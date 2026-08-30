"""Minimal CPU-rendered PySide6 example.

Usage:
  python pyside6_viewer.py /path/to/audio.wav.reapeaks
"""
import sys
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QApplication, QLabel
import reapeaks

path = sys.argv[1]
rp = reapeaks.ReaPeaks.open(path)
width, height = 1200, max(180, 160 * rp.channels)
# .reapeaks itself only provides an upper-bound frame length. For a production
# app, use the exact decoded media frame count from the audio source.
levels = rp.levels()
end_frame = levels[0][0] * levels[0][1]
raw = rp.render_rgba(width, height, 0, end_frame,
                     background=(20, 20, 20, 255),
                     waveform=(220, 230, 240, 255))
img = QImage(raw, width, height, width * 4, QImage.Format_RGBA8888).copy()
app = QApplication(sys.argv)
label = QLabel()
label.setPixmap(QPixmap.fromImage(img))
label.show()
sys.exit(app.exec())
