# C ABI overview

Public declarations live in `include/reapeaks.h`.

The ABI uses opaque `RpkHandle*` objects and caller-independent `RpkBuffer`
allocations. Any returned `RpkBuffer` must be released with `rpk_buffer_free`.

Main groups:

- file: `rpk_open`, `rpk_close`, `rpk_wave_encoding`;
- zoom: `rpk_level_count`, `rpk_get_level_info`, `rpk_plan_view`;
- tiled GUI: `rpk_tile_count`, `rpk_tile_texture_rgba8`;
- spectral GUI: `rpk_spectral_tile_texture_rgba8`;
- CPU image: `rpk_render_rgba8[_scaled]`;
- writer: `rpk_generate_pcm16`, `rpk_generate_f32`.

`rpk_generate_f32(..., large_range=1)` emits RPKL. Use `large_range=0` for an
integer-media-style RPKN cache after decoding 24/32-bit PCM or FLAC into float.

The spectral writer is most REAPER-compatible when the library is built with
`--features strict-wdl`.
