# User guide: REAPER RPKX protection reference extension

> **Experimental reference implementation, not part of the libreapeaks public API.**
>
> This extension is currently pinned to **REAPER 7.79**. It intentionally refuses
> to load in other REAPER versions rather than applying an unvalidated host
> integration.

This guide is for someone who wants to install and use a prebuilt extension. If
you want to study, modify, or build the integration, use [`README.md`](README.md)
and [`DESIGN.md`](DESIGN.md) instead.

## What it does

The extension transparently wraps supported REAPER media sources so ordinary
peak-cache regeneration can replace the standard REAPEAKS region without losing
an existing RPKX suffix.

There is **no settings panel and no normal user action to run**. Once the
extension is loaded, use REAPER normally. The integration is designed to take
part in ordinary import, peak rebuild, selected-item rebuild, display-profile,
offline/online, and related peak-cache workflows automatically.

The guarantee is about preserving an **already existing RPKX payload**. A newly
recorded, glued, or rendered media file has no pre-existing RPKX payload to
preserve at creation time. If RPKX is later attached to that media's cache,
subsequent ordinary regeneration is the relevant preservation workflow.

## Supported prebuilt targets

Release packages, when published, use these target names:

| Release asset | Host used for validation | Extension inside the archive |
|---|---|---|
| `libreapeaks-reaper-rpkx-example-windows-x86_64.zip` | Windows x86_64 | `reaper_rpkx.dll` |
| `libreapeaks-reaper-rpkx-example-linux-x86_64.zip` | Linux x86_64 | `reaper_rpkx.so` |
| `libreapeaks-reaper-rpkx-example-macos-arm64.zip` | macOS arm64 | `reaper_rpkx.dylib` |

The current real-host evidence is for REAPER 7.79 on those CI targets. In
particular, the macOS package is **arm64**, not an Intel-macOS compatibility
claim.

If a release does not contain a package for your platform, do not substitute a
binary built for another architecture. Build the reference extension from source
instead.

## Install a release build

1. Confirm that REAPER reports version **7.79**.
2. Open the repository's GitHub Release and download the archive matching your
   operating system and CPU architecture.
3. If the release contains `SHA256SUMS.txt`, verify the downloaded archive
   against it before installing.
4. In REAPER, choose the command that opens the **REAPER resource path**
   (`Options` > `Show REAPER resource path in explorer/finder...` in the standard
   REAPER menu layout).
5. Quit REAPER before changing extension files.
6. Open the `UserPlugins` directory inside the REAPER resource path. Create it if
   it does not already exist.
7. Extract the downloaded archive and copy **only the normal extension binary**
   into `UserPlugins`:

   ```text
   Windows: reaper_rpkx.dll
   macOS:   reaper_rpkx.dylib
   Linux:   reaper_rpkx.so
   ```

8. Start REAPER again.

The release archive also contains this user guide and the relevant license/
third-party notice files. The diagnostic fault-injection binary used by CI is
**not** a release asset and must not be installed.

## What successful loading looks like

Successful loading is intentionally quiet: there is no toolbar button, menu
command, or settings UI to enable. The extension registers itself internally as:

```text
libreapeaks RPKX protection (experimental)
```

If the REAPER version is not 7.79, the extension refuses to load and writes a
message to the REAPER console explaining that the experimental plugin is pinned
to REAPER 7.79.

For a functional check, use a disposable project/cache that already contains a
known RPKX suffix, perform an ordinary REAPER peak rebuild, and verify with the
RPKX-aware application that created or reads that suffix that its payload is
still present. Do not use irreplaceable cache data as the first installation
check.

## Normal use

After installation, no extra step is required. Continue using REAPER's ordinary
peak workflows. The reference integration has real-host coverage for operations
including:

- importing media and generating a missing cache;
- rebuilding all peaks;
- rebuilding peaks for selected items;
- stale media/source regeneration;
- waveform, spectral, spectrogram, and loudness cache profiles;
- offline/online transitions and reverse-source rebuilds;
- long PCM16 streaming;
- created-media lifecycles such as Glue and Render followed by an RPKX-bearing
  rebuild.

The validated real-host PCM paths include PCM16 WAVE and float32 WAVE. The source
wrapper recognizes additional REAPER source types, but this example is not an
exhaustive compatibility claim for every codec/container variant or third-party
`PCM_source` wrapper.

## Safety behavior

The example prefers refusing an unsafe update to silently destroying trailing
application data. In particular it uses source-stamp validation, guarded reads,
a crash-recovery journal, exact suffix checks, and safe rejection of unsupported
or unwrapped source pointers.

If REAPER prints a `libreapeaks:` error in its console, treat that as a failed
preserving operation and investigate before retrying on important data. Do not
replace the example with a build that disables the preservation or crash-safety
checks merely to make the error disappear.

## macOS note

The CI-produced macOS reference binary is not currently documented as a
Developer ID notarized distribution. macOS security policy may therefore require
an explicit user approval for a downloaded third-party extension. Do not disable
system-wide security protections. If your system will not load the downloaded
binary, either approve it through the normal macOS security UI when offered or
build the example locally from source.

## Uninstall

1. Quit REAPER.
2. Remove `reaper_rpkx.dll`, `reaper_rpkx.dylib`, or `reaper_rpkx.so` from the
   REAPER resource path's `UserPlugins` directory.
3. Start REAPER again.

Uninstalling the extension does not intentionally rewrite existing media or
cache files. It simply removes this reference integration from future REAPER
source/peak operations.

## Building instead of downloading

If no prebuilt asset is available for your system, see the build instructions in
[`README.md`](README.md). The implementation and its real-host test harness are
kept under `examples/reaper_rpkx_extension/` specifically so applications can
study or adapt the integration without treating it as part of the libreapeaks
library API.

For the exact safety and testing contract, see:

- [`DESIGN.md`](DESIGN.md)
- [`TESTING.md`](TESTING.md)
