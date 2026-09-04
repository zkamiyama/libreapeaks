"""PySide6 editor for the shared persistent demo cache configuration."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from demo_cache_config import (
    DemoCacheConfig,
    DemoConfigError,
    demo_config_path,
    load_demo_cache_config,
    save_demo_cache_config,
)
from reaper_config import discover_reaper_ini


_POLICY_ITEMS = (
    ("Beside source (default)", "sidecar"),
    ("peaks/ subdirectory", "subdir"),
    ("REAPER central cache", "reaper-central"),
    ("Follow REAPER.ini", "reaper-config"),
)


class CacheSettingsDialog(QDialog):
    """Edit cache placement once and share it across the desktop/web demos."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("libreapeaks cache settings")
        self.resize(720, 420)
        self.saved = False
        try:
            config = load_demo_cache_config()
            load_error = ""
        except DemoConfigError as exc:
            config = DemoCacheConfig()
            load_error = str(exc)

        self.policy = QComboBox(self)
        for label, value in _POLICY_ITEMS:
            self.policy.addItem(label, value)
        index = self.policy.findData(config.policy)
        self.policy.setCurrentIndex(max(0, index))

        self.cache_directory = QLineEdit(config.cache_directory, self)
        self.cache_directory.setPlaceholderText("Directory used by REAPER central cache")
        cache_row = self._path_row(
            self.cache_directory,
            "Choose central cache directory",
            directory=True,
        )

        self.reaper_ini = QLineEdit(config.reaper_ini, self)
        self.reaper_ini.setPlaceholderText("/path/to/reaper.ini")
        ini_row = self._path_row(self.reaper_ini, "Choose REAPER.ini", directory=False)

        self.auto_reaper_ini = QCheckBox("Auto-detect REAPER.ini", self)
        self.auto_reaper_ini.setChecked(config.auto_reaper_ini)
        self.auto_reaper_ini.toggled.connect(self._update_enabled)

        self.peak_rate = QSpinBox(self)
        self.peak_rate.setRange(0, 1_000_000)
        self.peak_rate.setSpecialValueText("Policy/default")
        self.peak_rate.setValue(config.peak_rate or 0)
        self.peak_rate.setToolTip(
            "0 uses peakcachegenrs when following REAPER.ini, otherwise 300"
        )

        self.verify = QCheckBox("Verify derived paths with installed REAPER", self)
        self.verify.setChecked(config.verify_with_reaper)

        self.reaper_executable = QLineEdit(config.reaper_executable, self)
        self.reaper_executable.setPlaceholderText("Optional REAPER executable")
        exe_row = self._path_row(
            self.reaper_executable,
            "Choose REAPER executable",
            directory=False,
        )

        self.summary = QLabel(self)
        self.summary.setWordWrap(True)
        self.summary.setText(
            load_error
            or f"Settings are shared by both demos and saved to {demo_config_path()}"
        )

        form = QFormLayout()
        form.addRow("Cache placement", self.policy)
        form.addRow("Central cache directory", cache_row)
        form.addRow("REAPER.ini", ini_row)
        form.addRow("", self.auto_reaper_ini)
        form.addRow("Fine peaks / second", self.peak_rate)
        form.addRow("", self.verify)
        form.addRow("REAPER executable", exe_row)

        self.detect_button = QPushButton("Detect REAPER.ini", self)
        self.detect_button.clicked.connect(self._detect_ini)
        self.cancel_button = QPushButton("Cancel", self)
        self.cancel_button.clicked.connect(self.reject)
        self.save_button = QPushButton("Save", self)
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self._save)

        buttons = QHBoxLayout()
        buttons.addWidget(self.detect_button)
        buttons.addStretch(1)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.save_button)

        layout = QVBoxLayout()
        layout.addWidget(self.summary)
        layout.addLayout(form)
        layout.addStretch(1)
        layout.addLayout(buttons)
        self.setLayout(layout)

        self.policy.currentIndexChanged.connect(self._update_enabled)
        self.verify.toggled.connect(self._update_enabled)
        self._update_enabled()

    def _path_row(self, edit: QLineEdit, title: str, *, directory: bool) -> QWidget:
        widget = QWidget(self)
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(edit, 1)
        button = QPushButton("Browse…", widget)

        def browse() -> None:
            initial = edit.text().strip() or str(Path.home())
            if directory:
                selected = QFileDialog.getExistingDirectory(self, title, initial)
            else:
                selected, _filter = QFileDialog.getOpenFileName(
                    self, title, initial, "All files (*)"
                )
            if selected:
                edit.setText(selected)

        button.clicked.connect(browse)
        layout.addWidget(button)
        edit._browse_button = button  # type: ignore[attr-defined]
        return widget

    def _update_enabled(self, *_args) -> None:
        policy = self.policy.currentData()
        central = policy == "reaper-central"
        follow = policy == "reaper-config"
        self.cache_directory.setEnabled(central)
        getattr(self.cache_directory, "_browse_button").setEnabled(central)
        self.reaper_ini.setEnabled(follow and not self.auto_reaper_ini.isChecked())
        getattr(self.reaper_ini, "_browse_button").setEnabled(
            follow and not self.auto_reaper_ini.isChecked()
        )
        self.auto_reaper_ini.setEnabled(follow)
        self.detect_button.setEnabled(follow)
        verify_enabled = central or follow
        self.verify.setEnabled(verify_enabled)
        self.reaper_executable.setEnabled(verify_enabled and self.verify.isChecked())
        getattr(self.reaper_executable, "_browse_button").setEnabled(
            verify_enabled and self.verify.isChecked()
        )

    def _detect_ini(self) -> None:
        executable = self.reaper_executable.text().strip() or None
        path = discover_reaper_ini(executable=executable)
        if path is None:
            self.summary.setText("No REAPER.ini was found on this system.")
            return
        self.reaper_ini.setText(str(path))
        self.auto_reaper_ini.setChecked(False)
        self.summary.setText(f"Detected {path}")

    def _save(self) -> None:
        policy = str(self.policy.currentData())
        cache_directory = self.cache_directory.text().strip()
        reaper_ini = self.reaper_ini.text().strip()
        if policy == "reaper-central" and not cache_directory:
            self.summary.setText("Choose a central cache directory before saving.")
            return
        if policy == "reaper-config" and not (
            reaper_ini or self.auto_reaper_ini.isChecked()
        ):
            self.summary.setText("Choose REAPER.ini or enable auto-detection.")
            return
        config = DemoCacheConfig(
            policy=policy,  # type: ignore[arg-type]
            cache_directory=cache_directory,
            reaper_ini=reaper_ini,
            auto_reaper_ini=self.auto_reaper_ini.isChecked(),
            verify_with_reaper=self.verify.isChecked(),
            reaper_executable=self.reaper_executable.text().strip(),
            peak_rate=self.peak_rate.value() or None,
        )
        try:
            path = save_demo_cache_config(config)
        except (OSError, DemoConfigError) as exc:
            self.summary.setText(f"Could not save settings: {exc}")
            return
        self.saved = True
        self.summary.setText(f"Saved {path}")
        self.accept()


def edit_cache_settings(parent=None) -> bool:
    dialog = CacheSettingsDialog(parent)
    return dialog.exec() == QDialog.DialogCode.Accepted and dialog.saved
