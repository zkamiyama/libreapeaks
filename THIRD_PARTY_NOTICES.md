# Third-party notices

The optional `strict-wdl` feature uses Cockos WDL as a Git submodule.
WDL source files retain their original permissive license notices. The relevant
WDL FFT code is also based on DJBFFT (Copyright 1999 D. J. Bernstein), as noted
in WDL's own source headers.

`strict-wdl` deliberately builds WDL's FFT with `WDL_FFT_REALSIZE=8`, matching
the double-precision FFT buffer observed in REAPER 7.79 x86_64 Linux.
