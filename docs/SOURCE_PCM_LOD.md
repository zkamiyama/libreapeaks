# Source PCM LOD for exact high-zoom waveforms

`.reapeaks` remains the persistent low/medium-resolution waveform pyramid.
The reference WebGL2 and PySide6 players now switch to a bounded source-PCM
window when the finest cache record becomes visibly wider than a pixel. At
sample scale they draw the real decoded samples, not enlarged peak-cache
records.

## Why a second data path is required

A waveform cache record contains the maximum and minimum of a source interval.
For a 48 kHz source and a 300 peaks/s fine layer, one record summarizes about
160 sample frames. Their order and all non-extreme samples are irreversibly
lost. No renderer can recover exact sample points from that pair.

This two-path model is also visible in existing DAW interfaces and source:

- REAPER's SDK gives `PCM_source` separate `GetSamples` and `GetPeakInfo`
  methods. Its peak-resolution recommendation says to switch to the high-
  resolution source at 1.5 pixels per peak
  ([sample/peak methods](https://github.com/justinfrankel/reaper-sdk/blob/main/sdk/reaper_plugin.h#L616-L617),
  [resolution recommendation](https://github.com/justinfrankel/reaper-sdk/blob/main/sdk/reaper_plugin.h#L1066)).
- Ardour's `AudioSource::read_peaks_with_fpp` labels the higher-resolution
  branch `UPSAMPLE`, reads the raw source in chunks of at most 4096 samples,
  and generates visual peaks on demand
  ([`audiosource.cc`, lines 757-779](https://github.com/Ardour/ardour/blob/ab29bbbe64050732f3f71145a99b607942d094f6/libs/ardour/audiosource.cc#L757-L779)).
- REAPER's own release notes separately refer to a “sample-level
  crosses-and-lines” view, later sinc interpolation for sample-level peaks, and
  more accurate drawing when highly zoomed. The 6.x notes also call out
  improved zoomed-in peak performance “especially on compressed media,” which
  is exactly where bounded decode/cache policy matters
  ([2.x notes](https://www.reaper.fm/download-old.php?ver=2x),
  [5.x notes](https://www.reaper.fm/download-old.php?ver=5x),
  [6.x notes](https://www.reaper.fm/download-old.php?ver=6x)).

The 1.5 value is a documented SDK recommendation, not a claim about the exact
private threshold in every REAPER release. libreapeaks uses it as a sensible
default and adds hysteresis.

## Implemented LOD state

| View density | Data | Display record |
|---|---|---|
| broad to fine | native/derived `.reapeaks` mipmap | stored max/min |
| finest cache record >= 1.5 px | source window | exact on-demand max/min bucket |
| <= 1 decoded frame/px | source window | one exact sample frame |
| >= 3 px/decoded frame | source window | connected line plus circular sample point |

The source path exits only below 1.1 px per finest peak. The 1.5/1.1 split
prevents repeated cache/source toggles at a threshold during trackpad zoom.

For the intermediate source-envelope state, division is the next power of two
at or above frames-per-pixel, capped at the finest cache division so source LOD
never becomes coarser than the cache it replaces. This is not another
persistent cache. The max/min records are created from the transient PCM page
and immediately sent to the renderer. At exact sample scale division is one,
so the response is a direct float32 sample slice with no min/max reduction.

## Bounded window algorithm

For each viewport the planner:

1. adds two source buckets of guard data on each side;
2. aligns the request to a reusable page boundary;
3. caps display records at 4096 (or the runtime GPU texture limit);
4. checks decoded bytes before any file read or FFmpeg process starts;
5. requests only the latest page needed by the UI.

Default budgets are:

| Limit | Default | Purpose |
|---|---:|---|
| target raw page | 1 MiB | reusable pan/zoom prefetch granularity |
| one raw window hard limit | 16 MiB | bound one read/decode and response |
| raw-window LRU | 64 MiB | keep nearby decoded pages, evict by bytes |
| retained LRU entries | 1024 | also bound zero-byte/EOF metadata entries |
| pending distinct windows | 64 | reject a stale-request flood before decode |
| concurrent reader loads | 2 | bound direct-reader memory and I/O concurrency |
| GPU display records | 4096 | bound texture height and draw work |

Raw float32 memory is:

```text
bytes = frames * channels * 4
```

For example, 1 MiB holds 131,072 stereo frames (2.73 seconds at 48 kHz) or
32,768 eight-channel frames (0.68 seconds at 48 kHz). Exact sample view needs
only roughly the canvas width plus guards, so its normal payload is much
smaller than the byte limit.

The LRU is byte-bounded and has an independent entry-count bound. Identical
concurrent HTTP requests share one in-flight decode, even when retained-cache
capacity is zero. Distinct pending windows and active reader calls also have
hard limits; a same-thread recursive request for its own in-flight key fails
instead of deadlocking. A reader failure wakes every coalesced waiter and does
not poison the key, so a later request can retry. The result distinguishes
`decoded`, `cache-hit`, and `coalesced`. PySide6 additionally permits only one
decoder task at a time; rapid pan/zoom replaces the requested target instead of
queuing FFmpeg processes. The browser debounces source requests for 45 ms. The
server serializes FFmpeg window decodes per source and drops superseded waiters
as a final concurrency and backlog bound. Until a requested source page is
ready, both UIs keep drawing
`.reapeaks` so interaction does not block.

The decoded cache page and the GPU display window are deliberately decoupled.
For example, a sample view may return only 4096 display frames while retaining
a larger 1 MiB decoded neighborhood. Nearby pans then slice the same raw page
and avoid both a new FFmpeg process and a larger GPU texture. Half-page overlap
keeps reuse predictable without rounding an adversarial viewport to three
fixed pages.

## Speed/memory trade-off

There is no way to make arbitrary access into every compressed codec both free
and stateless: a decoder must seek to a codec/container access point and decode
preroll. The useful choices are:

| Strategy | RAM | First/random seek | Disk | This implementation |
|---|---:|---:|---:|---|
| complete decoded PCM in memory | file-duration sized | fastest | none | deliberately avoided |
| decoded float WAV / OS page cache | bounded application RAM after preparation | fast after preparation | file-duration sized | reused with `--playback-decoder ffmpeg` |
| bounded range decode + byte LRU | fixed | codec-dependent miss; instant hit | none | default |
| persistent in-process decoder | fixed | good for nearby sequential access | none | possible future backend |

The default is the bounded range/LRU point: a miss pays decoder startup and
preroll, while nearby views hit a reclaimable fixed-size cache. Direct WAV reads
also benefit from the operating system's page cache without making the
application own a complete PCM allocation. For a workload dominated by
repeated random sample editing, the disk-backed float-WAV option trades startup
time and disk space for consistently cheap later seeks while still keeping RAM
bounded.

### Reusing a playback engine's PCM

A player normally has *some* decoded PCM in RAM, but it is usually a short
sequential ring/read-ahead buffer around the playhead, not the entire file.
Waveform viewports can be stopped, can jump arbitrarily, and can inspect a
region far from playback. The playback buffer may also be device-rate,
resampled, time-stretched, mixed, or post-DSP; those values are not necessarily
the exact source-file samples this LOD promises.

In a DAW with an accessible source-rate block cache, sharing it is better than
starting a second decoder. `CallbackPcmWindowReader` adapts such a cache:

```python
reader = CallbackPcmWindowReader(
    playback_engine.read_source_f32le,
    sample_rate=source_rate,
    channels=source_channels,
    total_frames=source_frames,
    backend="shared-playback-cache",
)
service = SourcePcmService(reader, cache_bytes=0)
```

The callback is synchronous; `PcmWindowLoader` invokes it on its worker rather
than the Qt GUI thread, and other callers must provide the same isolation. A
host should return a cached source block when present and synchronously decode
only a miss. Setting the libreapeaks LRU to zero avoids double-caching; a small
nonzero LRU is useful when the playback ring evicts display regions
aggressively. The callback must use the same pre-resampling source timeline,
sample rate, channel layout, and decoder delay/padding convention as the
`.reapeaks` cache.

If the host callback has its own cache diagnostics, it can return
`PcmWindowReadResult(window, cache_disposition="cache-hit", reader_ran=False)`
instead of a bare window. This keeps `rangeDecoded` and the browser decode
counter reserved for real host misses even when the libreapeaks LRU is
disabled.

The reference clients cannot random-access this optimization through their
stock playback backends: browser `HTMLAudioElement` and Qt `QMediaPlayer` do
not expose an arbitrary frame lookup into their internal decoded ring buffers.
Qt 6.8 and newer can emit the sequential decoded buffers that pass the
playhead through `QAudioBufferOutput`, explicitly including visualization as a
use case
([Qt `QMediaPlayer::audioBufferOutput`](https://doc.qt.io/qt-6/qmediaplayer.html#audioBufferOutput-prop)).
A host can opportunistically insert those buffers into the shared source cache,
but unplayed or already-evicted viewport ranges still need the callback's
random-access miss path. The reference source-window service therefore remains
separate and works on older Qt versions too.

## Source readers

### PCM16 and float32 RIFF/WAVE

The direct reader validates RIFF structure and audio geometry once, maps all
`data` chunks into one logical audio stream, and seeks directly to the requested
frame range. PCM16 is converted only for that range; float32 bytes can be used
directly. The complete media file is never copied into application memory.

The current direct scope is PCM16 and IEEE float32 RIFF/WAVE, including their
WAVE_FORMAT_EXTENSIBLE equivalents. Other WAV encodings, RF64/WAVE64, and
compressed formats use FFmpeg.

`libsndfile` is a viable future in-process backend because `sf_seek` is defined
in multichannel audio frames rather than container bytes
([libsndfile seek API](https://libsndfile.github.io/libsndfile/api.html#seek)).
It is not added as a required dependency by this implementation.

### Compressed and other media

Each cache miss runs a bounded FFmpeg decode for the requested timeline window:

```text
seek up to 2 s before target
  -> demux/decode codec preroll
  -> atrim by decoded sample count
  -> emit only requested interleaved f32le frames
```

FFmpeg documents that input `-ss` seeks to the closest earlier seek point and
that the default accurate-seek path decodes and discards the extra segment
([FFmpeg `-ss` and `-accurate_seek`](https://ffmpeg.org/ffmpeg.html#Main-options)).
The additional two-second lead handles decoder/container preroll and avoids a
short-FLAC boundary failure observed during implementation. `atrim` uses sample
counts for the final boundary; stdout is therefore still restricted to the
requested frames. FFmpeg runs with one thread, a timeout, one selected audio
stream, and no video/subtitle/data output. An output `-fs` limit equal to the
requested interleaved float32 byte count independently bounds captured stdout;
the returned payload is then checked for whole frames and for never exceeding
the requested frame count.

This is sample-position accurate on the decoded FFmpeg timeline. For lossy
formats it does not mean recovery of samples that existed before lossy
encoding, and another decoder implementation can differ at codec delay/padding
edges.

## Why the browser does not decode the complete source

Web Audio's `decodeAudioData` accepts encoded file data in an `ArrayBuffer` and
decodes that file data as a unit
([Web Audio 1.1](https://www.w3.org/TR/webaudio-1.1/#dom-baseaudiocontext-decodeaudiodata)).
An arbitrary HTTP byte range from MP3/FLAC is not generally a self-contained
decodable audio file, and decoding a multi-hour asset into an `AudioBuffer`
would defeat the memory bound. The browser therefore receives small f32 source
windows from the local server and uploads only their display records.

WebCodecs `AudioDecoder` is a plausible future browser-local backend, but its
interface consumes codec-specific `EncodedAudioChunk` objects rather than
providing a media-container demuxer or arbitrary file-frame seek
([WebCodecs](https://www.w3.org/TR/webcodecs/)). A cross-format implementation
would still need container indexing, HTTP byte-range mapping, codec preroll,
delay/padding reconciliation, and runtime codec-support fallback. Keeping that
complexity behind the local source-window service makes the current reference
path deterministic across the browser and PySide6 clients.

Qt's `QAudioDecoder` exposes sequential `bufferReady`/`read` decoding but no
sample-frame random-seek operation in its public API
([Qt `QAudioDecoder`](https://doc.qt.io/qt-6/qaudiodecoder.html)). The PySide6
player consequently uses the same random-access service as the web server,
while its playback `QMediaPlayer` remains independent.

## API and tuning

The web server advertises source availability and limits in `/api/meta` and
serves little-endian float32 display records from:

```text
GET /api/pcm-window?first=FRAME&count=FRAMES&division=DIVISION
```

Response headers describe the actual first frame, frame count, division,
record count, channels, component count, mode, backend, cache hit, and resident
cache bytes. They also expose a range-event id, raw decoded interval, reader
time, cache disposition, and the semantic `X-Pcm-Range-Reader-Ran` header. The
older/debug-friendly `X-Pcm-Range-Decode-Ran` spelling is retained as an alias.
A one-component response is exact samples; two components are `max,min`.

### Debug events and draw-plan API

Every successful `SourcePcmService.display_window()` result carries a
`PcmRangeEvent`. Its `reader_ran` flag answers whether this access actually ran
a range read/decode; cache hits and requests coalesced onto another in-flight
decode remain distinguishable. WebGL2 dispatches the same information as a
`libreapeaks:pcm-range` `CustomEvent` on `#analysisGl`. The browser diagnostics
line shows and briefly highlights the latest real decode, and the local server
logs one `PCM_RANGE_DECODE` record per actual reader run. PySide6 exposes
`PcmWindowLoader.rangeAccess` and `rangeDecoded` Qt signals and shows real
decodes in both its diagnostics row and status bar.

CPU and texture GUI clients can share the placement math:

```python
window = service.display_window(first, count, division)
draw = plan_pcm_draw(window, view_start, view_end, width_px)

# Texture path
record0 = draw.record0
records_across = draw.records_across

# CPU path (no point/segment array is allocated by the plan)
values = pcm_display_values(window)
for local in range(draw.visible_record_count):
    x = draw.x_for_local_record(local)
    sample = values[draw.sample_offset(local, channel)]
```

`draw_lines` and `draw_points` provide the sample-line/dot decision, with the
default dot threshold at 3 px per frame. `x_origin_px`/`x_step_px` support an
even tighter inner loop. JavaScript exports the mirrored `planPcmDraw()` helper,
which the WebGL2 shader path uses directly.

Both players accept:

```text
--no-source-pcm
--pcm-decoder auto|wav|ffmpeg
--pcm-cache-mib MIB
--pcm-window-mib MIB
--pcm-page-mib MIB
```

`--pcm-cache-mib 0` disables retained decoded pages while preserving bounded
window decoding. Lower the limits for many simultaneous players; raise the LRU
when repeatedly scrubbing compressed media around a larger neighborhood.

If `--playback-decoder ffmpeg` is selected, the existing playback preparation
already creates a temporary float WAV on disk. The source LOD reuses that
seekable file rather than decoding the original compressed source again. With
the default native playback mode, source LOD reads/decodes bounded windows from
the original media.

The current example playback preparation briefly materializes its bounded
full decode before writing that temporary WAV (subject to
`--max-decode-bytes`). That is an optional playback compatibility path, not the
default source-LOD policy. A production disk-cache path should stream FFmpeg
output directly into a finalized RF64/WAVE64 or chunked store when very long
media must never have a full-file RAM peak.

## Failure and semantic boundaries

- Missing FFmpeg, a failed seek, or an unsupported source disables/falls back
  to `.reapeaks`; it does not prevent playback or broad waveform display.
- The path displays decoded source-file samples. It does not yet apply a DAW
  take's resampling, reverse, stretch markers, fades, item gain, or plugin DSP.
- Full-file cache generation still requires a sequential full decode because
  every source frame contributes to `.reapeaks`. That operation is separate
  from interactive source-window LOD.
- The cache does not carry an exact source-frame count. Players prefer media
  metadata/exact WAV geometry and clamp a short final decode at EOF.

## Verification

Regression tests cover direct split-chunk WAV range reads, EOF clamping,
float32 zero-copy sample payloads, exact on-demand extrema, LOD hysteresis and
memory limits, zero-capacity concurrent decode coalescing, failed-waiter wakeup,
same-key reentrancy, pending/concurrent request limits, byte and item LRU
eviction, malformed reader output, NaN/infinity samples, structured and random
malformed WAV corpora, structured range-decode events, HTTP query floods,
response-header consistency, line/dot draw geometry, and FFmpeg timeout/output
bounds. FFmpeg windows are compared with the same decoded PCM timeline;
lossless FLAC windows are compared at several nontrivial offsets. Python and
JavaScript each run thousands of randomized LOD coverage/budget cases, and a
cross-runtime parity gate compares their plans exactly. Real Mesa/PySide6 and
Chrome/WebGL2 workflows exercise the R32F sample texture, shaders, and visible
range-decode notifications.
