"""DAW-oriented PySide6 player with an RPKX container inventory dock."""
from __future__ import annotations

import sys

import pyside6_daw_player as _daw
from pyside6_rpkx_browser import install_rpkx_dock


class DawPlayerWindow(_daw.DawPlayerWindow):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.rpkx_dock = install_rpkx_dock(self, self.peaks_path)


def main(argv: list[str] | None = None) -> int:
    # Both the direct-audio path and the no-argument drop launcher resolve this
    # module global at runtime, so one replacement covers both entry paths.
    _daw.DawPlayerWindow = DawPlayerWindow
    return _daw.main(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    raise SystemExit(main())
