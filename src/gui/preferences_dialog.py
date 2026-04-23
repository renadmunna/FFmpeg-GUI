"""
Preferences dialog.

Right now the only preference we persist is the folder that contains
the user's chosen ``ffmpeg`` and ``ffprobe`` binaries. Exposing this as
a dedicated dialog - rather than a simple file-picker - gives us room
to grow later (default export folder, preferred encoder preset, and so
on) without rebuilding the UI, and it lets us surround the single path
field with the contextual information that makes it usable: an
explanation of what the path should contain, an immediate indication of
whether the chosen path actually works, and a "Clear" button that
reverts to the automatic discovery behaviour.

The dialog does not modify the running locator directly. It saves the
new preference to :class:`AppSettings` and emits ``preferencesSaved``;
the main window listens for that signal and is responsible for
rediscovering FFmpeg and refreshing any dependent UI. Keeping the
dialog passive means it can be reused from anywhere without knowing
about the application's internal wiring.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..utils.ffmpeg_locator import FFmpegLocator, FFmpegNotFound
from ..utils.settings import AppSettings


class PreferencesDialog(QDialog):
    """Modal preferences window, today only hosting the FFmpeg Path field.

    Signals
    -------
    preferencesSaved():
        Emitted after the user clicks Save and the new value has been
        persisted. Connect this on the main window to rediscover FFmpeg
        and refresh the status indicator.
    """

    preferencesSaved = Signal()

    def __init__(self, settings: AppSettings, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Preferences")
        self.setModal(True)
        self.setMinimumWidth(560)

        self._settings = settings

        # --- FFmpeg path row ---------------------------------------------
        # The input field is editable so advanced users can paste a path
        # without opening the file dialog. The Browse button drives the
        # common case, and the Clear button makes it obvious that the
        # preference can be reverted rather than only overwritten.
        self._path_edit = QLineEdit(self)
        current = self._settings.ffmpeg_path()
        if current is not None:
            self._path_edit.setText(str(current))
        self._path_edit.setPlaceholderText(
            "(empty = use bundled bin/, FFMPEG_GUI_BIN, or system PATH)"
        )
        # Live validation: whenever the text changes we re-test the path
        # so the status label underneath stays accurate. textChanged fires
        # for every keystroke and for programmatic setText() calls, so the
        # initial load also triggers it automatically.
        self._path_edit.textChanged.connect(self._update_test_status)

        btn_browse = QPushButton("Browse…", self)
        btn_browse.clicked.connect(self._on_browse)

        btn_clear = QPushButton("Clear", self)
        btn_clear.setToolTip("Use the default discovery order instead of a custom path.")
        btn_clear.clicked.connect(lambda: self._path_edit.setText(""))

        path_row = QHBoxLayout()
        path_row.addWidget(self._path_edit, 1)
        path_row.addWidget(btn_browse)
        path_row.addWidget(btn_clear)

        # A small explanatory label sits under the path row. It describes
        # what the folder should contain and why, so the user does not have
        # to refer to the README to configure this correctly.
        self._help_label = QLabel(
            "Pick the folder that contains <code>ffmpeg</code> and "
            "<code>ffprobe</code>. Leaving this blank tells the app to "
            "fall back to its usual discovery order: the bundled "
            "<code>bin/</code> folder, the <code>FFMPEG_GUI_BIN</code> "
            "environment variable, and finally your system PATH.",
            self,
        )
        self._help_label.setWordWrap(True)
        self._help_label.setStyleSheet("QLabel { color: palette(mid); }")

        # Live test status: re-runs discovery whenever the field changes
        # and reports success or failure in a single line. This gives the
        # user immediate feedback without needing a separate Test button.
        self._test_label = QLabel("", self)
        self._test_label.setWordWrap(True)

        # --- form layout --------------------------------------------------
        form = QFormLayout()
        form.addRow(QLabel("<b>FFmpeg path</b>", self))
        form.addRow("Binary folder:", _wrap_layout(path_row))
        form.addRow("", self._help_label)
        form.addRow("Status:", self._test_label)

        # --- buttons ------------------------------------------------------
        button_box = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel,
            parent=self,
        )
        button_box.accepted.connect(self._on_save)
        button_box.rejected.connect(self.reject)

        # --- assemble -----------------------------------------------------
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addStretch(1)
        layout.addWidget(button_box)

        # Prime the status label with the initial value.
        self._update_test_status()

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------
    def _on_browse(self) -> None:
        """Open a folder picker and populate the path field with the choice.

        We use getExistingDirectory rather than getOpenFileName because the
        locator's mental model is "the folder that contains the binaries",
        not "the ffmpeg executable itself". Keeping the UI consistent with
        that mental model avoids ambiguity about what to pick when the
        folder also contains other FFmpeg tools (ffplay, ffmpeg-docs, etc.).
        """
        current = self._path_edit.text().strip()
        start_dir = current if current and Path(current).is_dir() else str(Path.home())
        chosen = QFileDialog.getExistingDirectory(
            self,
            "Select folder containing ffmpeg and ffprobe",
            start_dir,
        )
        if chosen:
            self._path_edit.setText(chosen)

    def _on_save(self) -> None:
        """Persist the new value and notify interested parties.

        If the user saved an obviously broken path (non-empty and not a
        directory), we give them a last-chance confirmation prompt. This
        is friendlier than silent acceptance and less paternalistic than
        refusing to close: sometimes a user knows they are about to create
        the folder and wants the preference saved in advance.
        """
        raw = self._path_edit.text().strip()
        if raw:
            candidate = Path(raw)
            if not candidate.is_dir():
                answer = QMessageBox.question(
                    self,
                    "Path does not exist",
                    f"The folder <b>{raw}</b> does not exist yet.<br><br>"
                    "Save this preference anyway? The application will fall "
                    "back to its usual discovery order until you create it.",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if answer != QMessageBox.Yes:
                    return
            self._settings.set_ffmpeg_path(candidate)
        else:
            # Empty field means "remove the override", which is different
            # from saving an empty string. set_ffmpeg_path(None) calls
            # QSettings.remove() so the key vanishes cleanly.
            self._settings.set_ffmpeg_path(None)

        self.preferencesSaved.emit()
        self.accept()

    def _update_test_status(self) -> None:
        """Re-run discovery with the current field value and update the label.

        We call the same FFmpegLocator.discover we use at startup, passing
        the current text as the preferred directory. That means the status
        line reflects the real outcome the user will see when they click
        Save - not a separate check that might diverge from the real code
        path.
        """
        raw = self._path_edit.text().strip()
        preferred = Path(raw) if raw else None
        try:
            locator = FFmpegLocator.discover(preferred_dir=preferred)
        except FFmpegNotFound:
            self._test_label.setText(
                "<span style='color: #c0392b;'>&#x25CF;</span> "
                "FFmpeg not found at the specified path or on any fallback."
            )
            return

        # If the user set a custom path, check whether the binaries we
        # actually found live inside that path. If they don't, discovery
        # silently fell through to a default, and the user deserves to
        # know so they can fix their configuration.
        if preferred is not None:
            try:
                locator.ffmpeg.relative_to(preferred)
                location_note = f"found at <code>{locator.ffmpeg}</code>"
            except ValueError:
                location_note = (
                    f"<b>not found at your custom path</b> - "
                    f"falling back to <code>{locator.ffmpeg}</code>"
                )
        else:
            location_note = f"found at <code>{locator.ffmpeg}</code>"

        self._test_label.setText(
            f"<span style='color: #27ae60;'>&#x25CF;</span> "
            f"<b>{_html_escape(locator.version())}</b><br>"
            f"{location_note}"
        )


def _wrap_layout(layout) -> QWidget:
    """Wrap a raw layout in a container widget so QFormLayout can host it.

    QFormLayout.addRow accepts widgets or strings, not layouts, so to put
    a three-widget row (field + two buttons) next to a label we need a
    thin container. A default-constructed QWidget with that layout is the
    smallest way to express it and carries no visible chrome.
    """
    container = QWidget()
    container.setLayout(layout)
    layout.setContentsMargins(0, 0, 0, 0)
    return container


def _html_escape(text: str) -> str:
    """Escape angle brackets and ampersands for Qt's rich-text labels.

    Rich-text rendering kicks in automatically whenever a label string
    looks like HTML, so a version banner containing ``<`` would otherwise
    be interpreted as an incomplete tag and swallowed.
    """
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
