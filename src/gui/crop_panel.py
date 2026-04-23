"""
Crop panel.

This panel owns the UI side of cropping: an aspect-ratio combo box, four
spinboxes for numeric X/Y/width/height input, and a "Reset" button. The
visible crop rectangle is drawn by :class:`src.gui.crop_overlay.CropOverlay`
inside the preview, and the two components communicate via signals wired
in the main window. Keeping them decoupled means the overlay doesn't know
about aspect-ratio presets and the panel doesn't know about mouse events,
which makes each piece easier to reason about in isolation.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from src.core.edit_state import CropRect, EditState


# Presets shown in the aspect-ratio combo. The value is width/height as a
# float, or ``None`` for "Free" which unlocks the aspect constraint.
# Order matters - "Free" sits first because it's the default and the most
# permissive choice.
_ASPECT_PRESETS = [
    ("Free (no lock)", None),
    ("1:1 (square)", 1.0),
    ("4:3 (classic TV)", 4 / 3),
    ("16:9 (widescreen)", 16 / 9),
    ("9:16 (vertical)", 9 / 16),
]


class CropPanel(QWidget):
    """Numeric crop controls and aspect-ratio picker.

    Signals
    -------
    cropRectChanged(object):
        Emitted when the spinboxes produce a new rectangle. The main
        window propagates this to the preview's overlay.
    cropResetRequested():
        Emitted when the user clicks Reset. Handled by the main window
        so both the edit state and the overlay snap back together.
    aspectRatioChanged(object):
        Emitted with ``None`` for unlock or a float for a locked ratio.
    """

    cropRectChanged = Signal(object)    # CropRect
    cropResetRequested = Signal()
    aspectRatioChanged = Signal(object)  # float | None

    def __init__(self, edit_state: EditState, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._edit_state = edit_state
        self._source_width = 0
        self._source_height = 0
        # Guard flag so we don't emit cropRectChanged while programmatically
        # populating the spinboxes in response to an external change.
        self._suppress_emit = False

        # -- Aspect ratio picker --------------------------------------
        self._aspect_combo = QComboBox()
        for label, value in _ASPECT_PRESETS:
            self._aspect_combo.addItem(label, value)
        self._aspect_combo.currentIndexChanged.connect(self._on_aspect_changed)

        # -- Spinboxes ------------------------------------------------
        # Maxima are set when a file loads; starting with a generous
        # upper bound avoids Qt clipping values before we know the
        # source dimensions.
        self._x_spin = _make_spin("px")
        self._y_spin = _make_spin("px")
        self._w_spin = _make_spin("px")
        self._h_spin = _make_spin("px")

        for spin in (self._x_spin, self._y_spin, self._w_spin, self._h_spin):
            spin.valueChanged.connect(self._on_spin_changed)

        # -- Reset ----------------------------------------------------
        btn_reset = QPushButton("Reset crop")
        btn_reset.setToolTip("Clear the crop selection so the full frame is kept.")
        btn_reset.clicked.connect(self.cropResetRequested)

        # -- Info label showing current crop dims on screen -----------
        self._info_label = QLabel("No file loaded.")
        self._info_label.setStyleSheet("color: #666; font-size: 11px;")

        # -- Layout ---------------------------------------------------
        aspect_row = QHBoxLayout()
        aspect_row.addWidget(QLabel("Aspect ratio:"))
        aspect_row.addWidget(self._aspect_combo, 1)

        form = QFormLayout()
        form.addRow("X:", self._x_spin)
        form.addRow("Y:", self._y_spin)
        form.addRow("Width:", self._w_spin)
        form.addRow("Height:", self._h_spin)

        group = QGroupBox("Crop")
        group_layout = QVBoxLayout(group)
        group_layout.addLayout(aspect_row)
        group_layout.addLayout(form)
        group_layout.addWidget(btn_reset)
        group_layout.addWidget(self._info_label)
        group_layout.addStretch(1)

        outer = QVBoxLayout(self)
        outer.addWidget(group)
        outer.addStretch(1)

    # ------------------------------------------------------------------
    # Called by the main window
    # ------------------------------------------------------------------

    def set_source_size(self, width: int, height: int) -> None:
        """Set the spinbox bounds to match the loaded file's dimensions."""
        self._source_width = max(width, 0)
        self._source_height = max(height, 0)
        self._suppress_emit = True
        try:
            self._x_spin.setMaximum(max(0, width - 1))
            self._y_spin.setMaximum(max(0, height - 1))
            self._w_spin.setRange(1, max(1, width))
            self._h_spin.setRange(1, max(1, height))
            # Default to the whole frame.
            self._x_spin.setValue(0)
            self._y_spin.setValue(0)
            self._w_spin.setValue(width)
            self._h_spin.setValue(height)
        finally:
            self._suppress_emit = False
        self._refresh_info()

    def update_from_overlay(self, rect: Optional[CropRect]) -> None:
        """Refresh spinboxes to match a rectangle set by the overlay.

        ``rect`` of ``None`` means "full frame" which we display as x=0,
        y=0, width=source_width, height=source_height.
        """
        self._suppress_emit = True
        try:
            if rect is None:
                self._x_spin.setValue(0)
                self._y_spin.setValue(0)
                self._w_spin.setValue(self._source_width)
                self._h_spin.setValue(self._source_height)
            else:
                self._x_spin.setValue(rect.x)
                self._y_spin.setValue(rect.y)
                self._w_spin.setValue(rect.width)
                self._h_spin.setValue(rect.height)
        finally:
            self._suppress_emit = False
        self._refresh_info()

    # ------------------------------------------------------------------
    # Internal handlers
    # ------------------------------------------------------------------

    def _on_aspect_changed(self, _index: int) -> None:
        ratio = self._aspect_combo.currentData()
        self.aspectRatioChanged.emit(ratio)

    def _on_spin_changed(self, _value: int) -> None:
        if self._suppress_emit:
            return
        # Clamp width/height so x+width doesn't exceed source_width, etc.
        # We do this here rather than via QSpinBox maximums because the
        # valid maximum for width depends on the current X value.
        x = self._x_spin.value()
        y = self._y_spin.value()
        max_w = max(1, self._source_width - x)
        max_h = max(1, self._source_height - y)
        w = min(self._w_spin.value(), max_w)
        h = min(self._h_spin.value(), max_h)
        if w != self._w_spin.value():
            self._suppress_emit = True
            self._w_spin.setValue(w)
            self._suppress_emit = False
        if h != self._h_spin.value():
            self._suppress_emit = True
            self._h_spin.setValue(h)
            self._suppress_emit = False

        self.cropRectChanged.emit(CropRect(x=x, y=y, width=w, height=h))
        self._refresh_info()

    def _refresh_info(self) -> None:
        if self._source_width == 0 or self._source_height == 0:
            self._info_label.setText("No file loaded.")
            return
        w, h = self._w_spin.value(), self._h_spin.value()
        # Show the current crop size alongside the source size, so the
        # user sees at a glance how much they're discarding.
        self._info_label.setText(
            f"Crop: {w}x{h} of {self._source_width}x{self._source_height}"
        )


def _make_spin(suffix: str) -> QSpinBox:
    """Factory for a QSpinBox with consistent formatting across fields."""
    spin = QSpinBox()
    spin.setRange(0, 100000)   # generous ceiling; real max set in set_source_size
    spin.setSuffix(f" {suffix}")
    spin.setSingleStep(1)
    # Step by 10 pixels when the user holds Shift, which Qt does by default
    # via PageUp/PageDown but not Shift. We don't customise this further
    # to keep behaviour predictable.
    return spin
