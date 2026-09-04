# PySide / Web DAW GUI parity

The parity entry points keep the existing reference demos intact while adding a
shared RPKX inventory and the DAW-oriented controls to the browser demo.

## PySide

Reference player + RPKX inventory dock:

```bash
python examples/pyside6_player_with_rpkx.py /path/to/audio.wav
```

DAW player + RPKX inventory dock (also supports the existing no-argument drop
launcher):

```bash
python examples/pyside6_daw_player_with_rpkx.py
python examples/pyside6_daw_player_with_rpkx.py /path/to/audio.wav
```

## Web

The enhanced server can start empty or with an initial file:

```bash
python examples/web_player/daw_server.py
python examples/web_player/daw_server.py /path/to/audio.wav
```

Open `http://127.0.0.1:8765/`. The browser can then open or drag/drop another
local media file. Upload and full waveform + spectral + spectrogram + loudness
cache preparation run without blocking the HTTP UI; the progress overlay polls
the session state and reloads when the replacement service is ready.

## Display parity

The Web DAW page exposes the same display concepts as `pyside6_daw_player.py`:

| PySide DAW control | Web DAW control |
| --- | --- |
| Waveform / spectral peaks / spectrogram / loudness view | `View` selector |
| Peak zoom | `Peak zoom` (-24…+24 dB) |
| Analysis opacity | `Opacity` |
| Spectral full-spectrum / every-octave | `Range` |
| Spectral low/high Hz | `Low Hz` / `High Hz` |
| Spectral reverse / fade noise | matching checkboxes |
| Loudness LUFS-M / LUFS-S | `Measure` |
| Loudness graph+peaks / colored peaks | `Style` |
| Loudness low/high/offset/transition | matching LU controls |
| Spectrogram heatmap | `Heatmap` |
| Spectrogram intensity/gain | `Intensity / gain dB` |
| Spectrogram floor/ceiling | matching dB controls |
| Spectrogram contrast | `Contrast` |
| Spectrogram log/linear frequency | `Frequency` |
| Horizontal wheel zoom / Ctrl+wheel vertical zoom / drag pan / seek | existing web interaction + DAW overlay |
| Extreme-zoom source PCM | retained for waveform mode |

## RPKX inventory

Both parity entry points show the same opaque RPKX metadata:

- container flags and source stamp;
- chunk index;
- namespace UUID/bytes;
- FourCC kind;
- payload version and flags;
- payload byte count; and
- a short hex/ASCII payload preview.

No application-specific meaning is assigned to RPKX payload bytes.
