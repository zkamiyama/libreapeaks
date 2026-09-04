"""Reference PySide6 player with an RPKX container inventory dock."""
from __future__ import annotations

import sys

import pyside6_player as _base
from pyside6_rpkx_browser import install_rpkx_dock


class PlayerWindow(_base.PlayerWindow):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.rpkx_dock = install_rpkx_dock(self, self.peaks_path)


def main(argv: list[str] | None = None) -> int:
    _base.PlayerWindow = PlayerWindow
    return _base.main(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    raise SystemExit(main())
