"""
Transform panel: rotation and flip controls.

Of all the edit panels this is the simplest because it has no continuous
state - just four mutually-exclusive rotation buttons and two flip
toggles. We still give each button a dedicated method rather than a
shared handler so the code reads straight top to bottom without casing.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.core.edit_state import EditState


class TransformPanel(QWidget):
    """Rotate 0/90/180/270 and flip horizontal/vertical.

    Signals
    -------
    transformChanged(int, bool, bool):
        (rotation_degrees, flip_h, flip_v). The main window forwards
        this to the preview so the user sees the change immediately,
        and writes the values into :class:`EditState` for the next
        export.
    """

    transformChanged = Signal(int, bool, bool)

    def __init__(self, edit_state: EditState, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._edit_state = edit_state

        # -- Rotation buttons -----------------------------------------
        # We implement rotation as three buttons that *apply* a rotation
        # delta, rather than four buttons that *set* an absolute angle.
        # That matches how users think about it: "rotate right" is a
        # verb, not a state.
        btn_rot_ccw = QPushButton("⟲ 90° left")
        btn_rot_ccw.setToolTip("Rotate 90 degrees counter-clockwise.")
        btn_rot_ccw.clicked.connect(lambda: self._rotate_by(-90))

        btn_rot_cw = QPushButton("⟳ 90° right")
        btn_rot_cw.setToolTip("Rotate 90 degrees clockwise.")
        btn_rot_cw.clicked.connect(lambda: self._rotate_by(90))

        btn_rot_180 = QPushButton("⤢ 180°")
        btn_rot_180.setToolTip("Rotate 180 degrees.")
        btn_rot_180.clicked.connect(lambda: self._rotate_by(180))

        btn_rot_reset = QPushButton("Reset rotation")
        btn_rot_reset.clicked.connect(self._reset_rotation)

        rot_row = QHBoxLayout()
        rot_row.addWidget(btn_rot_ccw)
        rot_row.addWidget(btn_rot_cw)
        rot_row.addWidget(btn_rot_180)

        self._rotation_label = QLabel("Current rotation: 0°")
        self._rotation_label.setStyleSheet("color: #666; font-size: 11px;")

        rot_group = QGroupBox("Rotate")
        rot_layout = QVBoxLayout(rot_group)
        rot_layout.addLayout(rot_row)
        rot_layout.addWidget(btn_rot_reset)
        rot_layout.addWidget(self._rotation_label)

        # -- Flip checkboxes ------------------------------------------
        self._flip_h = QCheckBox("Flip horizontal (mirror left-right)")
        self._flip_h.toggled.connect(self._on_flip_toggled)

        self._flip_v = QCheckBox("Flip vertical (mirror top-bottom)")
        self._flip_v.toggled.connect(self._on_flip_toggled)

        flip_group = QGroupBox("Flip")
        flip_layout = QVBoxLayout(flip_group)
        flip_layout.addWidget(self._flip_h)
        flip_layout.addWidget(self._flip_v)

        # -- Outer layout ---------------------------------------------
        outer = QVBoxLayout(self)
        outer.addWidget(rot_group)
        outer.addWidget(flip_group)
        outer.addStretch(1)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Clear all transforms back to defaults (called on new file load)."""
        self._edit_state.rotation = 0
        self._edit_state.flip_horizontal = False
        self._edit_state.flip_vertical = False
        # Block the toggled signals so we don't fire three transformChanged
        # events for a single logical reset.
        for cb in (self._flip_h, self._flip_v):
            cb.blockSignals(True)
            cb.setChecked(False)
            cb.blockSignals(False)
        self._rotation_label.setText("Current rotation: 0°")
        self.transformChanged.emit(0, False, False)

    def rotate_delta(self, delta_degrees: int) -> None:
        """Apply a relative rotation from an external caller.

        Exposed so the main window's Edit menu can rotate without
        duplicating the bookkeeping: the panel updates its own label,
        mutates the shared :class:`EditState`, and emits
        ``transformChanged`` exactly once, which the main window's slot
        picks up to refresh the preview. Keeping this path as the only
        way to change rotation guarantees the panel's UI never drifts
        out of sync with the actual rotation state.
        """
        self._rotate_by(delta_degrees)

    def reset_rotation(self) -> None:
        """Set rotation back to zero (flip state is left alone).

        The distinction from :meth:`reset` is intentional: the Edit
        menu's "Reset rotation" item should only touch rotation, not
        clear flips the user may have separately enabled.
        """
        self._reset_rotation()

    def toggle_flip_horizontal(self) -> None:
        """Flip the horizontal-flip checkbox programmatically.

        Toggling the checkbox (rather than mutating the edit state
        directly) makes the checkbox the single source of truth for
        the flip UI. The checkbox's ``toggled`` signal fires, which
        triggers ``_on_flip_toggled``, which in turn emits
        ``transformChanged`` - so this one call keeps the UI, the
        state, and the preview in agreement.
        """
        self._flip_h.setChecked(not self._flip_h.isChecked())

    def toggle_flip_vertical(self) -> None:
        """Flip the vertical-flip checkbox programmatically. See above."""
        self._flip_v.setChecked(not self._flip_v.isChecked())

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _rotate_by(self, delta_degrees: int) -> None:
        # Normalise into [0, 360) so we always store one of 0, 90, 180, 270.
        new_rotation = (self._edit_state.rotation + delta_degrees) % 360
        self._edit_state.rotation = new_rotation
        self._rotation_label.setText(f"Current rotation: {new_rotation}°")
        self._emit()

    def _reset_rotation(self) -> None:
        self._edit_state.rotation = 0
        self._rotation_label.setText("Current rotation: 0°")
        self._emit()

    def _on_flip_toggled(self, _checked: bool) -> None:
        self._edit_state.flip_horizontal = self._flip_h.isChecked()
        self._edit_state.flip_vertical = self._flip_v.isChecked()
        self._emit()

    def _emit(self) -> None:
        self.transformChanged.emit(
            self._edit_state.rotation,
            self._edit_state.flip_horizontal,
            self._edit_state.flip_vertical,
        )
