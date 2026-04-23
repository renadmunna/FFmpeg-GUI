"""
Trim panel.

The trim UI has three cooperating pieces: the existing seek slider in the
preview, a pair of "Set start / Set end" buttons that snap to the current
playhead position, and two time-edit fields that accept ``HH:MM:SS.mmm``
strings for exact millisecond input. We deliberately do not build a
custom two-handle range slider here - it would duplicate functionality
the preview's seek slider already provides, and during early testing
"scrub to the right spot and press a button" felt more natural than
"drag a tiny handle on a thin bar."
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.core.edit_state import EditState
from src.utils.time_utils import format_ms, parse_time


class TrimPanel(QWidget):
    """Trim controls: set start/end, type exact times, see selected duration.

    Signals
    -------
    trimChanged(int, int):
        Emitted whenever the effective trim range changes. Both values
        are concrete milliseconds (never None), so the main window can
        overlay markers on the preview seek bar without resolving
        defaults itself.
    seekRequested(int):
        Emitted when the user presses "Go to start" or "Go to end" so
        the main window can move the preview playhead.
    setStartRequested():
        Emitted when the user clicks "Set from playhead" next to Start.
        The main window, which knows the current playhead position,
        responds by calling :meth:`set_start_to`. Routing via a signal
        keeps this panel ignorant of the preview widget.
    setEndRequested():
        Same idea, for the end.
    """

    # Signals must be declared as class-level attributes *during* class
    # creation for PySide6's metaclass to wire them up. Declaring them
    # below, after the class body, silently does nothing.
    trimChanged = Signal(int, int)
    seekRequested = Signal(int)
    setStartRequested = Signal()
    setEndRequested = Signal()

    def __init__(self, edit_state: EditState, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._edit_state = edit_state
        self._duration_ms = 0

        # -- Start section --------------------------------------------
        self._start_edit = QLineEdit("00:00:00.000")
        self._start_edit.setToolTip(
            "Start of the kept region. Format: HH:MM:SS.mmm (milliseconds optional)."
        )
        self._start_edit.editingFinished.connect(self._on_start_edit_finished)

        btn_set_start = QPushButton("Set from playhead")
        btn_set_start.setToolTip("Use the preview's current time as the trim start.")
        btn_set_start.clicked.connect(self.setStartRequested)

        btn_goto_start = QPushButton("Go to")
        btn_goto_start.setToolTip("Seek the preview to the trim start.")
        btn_goto_start.clicked.connect(
            lambda: self.seekRequested.emit(self._edit_state.trim_start_ms or 0)
        )

        start_row = QHBoxLayout()
        start_row.addWidget(self._start_edit, 1)
        start_row.addWidget(btn_set_start)
        start_row.addWidget(btn_goto_start)

        # -- End section ----------------------------------------------
        self._end_edit = QLineEdit("00:00:00.000")
        self._end_edit.setToolTip(
            "End of the kept region. Format: HH:MM:SS.mmm (milliseconds optional)."
        )
        self._end_edit.editingFinished.connect(self._on_end_edit_finished)

        btn_set_end = QPushButton("Set from playhead")
        btn_set_end.setToolTip("Use the preview's current time as the trim end.")
        btn_set_end.clicked.connect(self.setEndRequested)

        btn_goto_end = QPushButton("Go to")
        btn_goto_end.setToolTip("Seek the preview to the trim end.")
        btn_goto_end.clicked.connect(
            lambda: self.seekRequested.emit(
                self._edit_state.trim_end_ms
                if self._edit_state.trim_end_ms is not None
                else self._duration_ms
            )
        )

        end_row = QHBoxLayout()
        end_row.addWidget(self._end_edit, 1)
        end_row.addWidget(btn_set_end)
        end_row.addWidget(btn_goto_end)

        # -- Duration readout & reset ---------------------------------
        self._duration_label = QLabel("Selection: 00:00:00.000")
        # A bold, slightly larger font makes this the thing the eye lands
        # on: "how long will my output be?" is the question users check
        # most often while adjusting trims.
        self._duration_label.setStyleSheet("font-weight: 600; font-size: 14px;")

        btn_reset = QPushButton("Reset trim")
        btn_reset.setToolTip("Clear trim points and keep the whole file.")
        btn_reset.clicked.connect(self._on_reset)

        # -- Layout ---------------------------------------------------
        form = QFormLayout()
        form.addRow("Start:", _wrap_in_widget(start_row))
        form.addRow("End:", _wrap_in_widget(end_row))

        hint = QLabel(
            "Tip: use the Left and Right arrow keys on the preview to step "
            "one frame at a time, then press Set from playhead for frame-"
            "accurate trim points."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #666; font-size: 11px;")

        group = QGroupBox("Trim")
        group_layout = QVBoxLayout(group)
        group_layout.addLayout(form)
        group_layout.addWidget(self._duration_label)
        group_layout.addWidget(btn_reset)
        group_layout.addWidget(hint)
        group_layout.addStretch(1)

        outer = QVBoxLayout(self)
        outer.addWidget(group)
        outer.addStretch(1)

    # ------------------------------------------------------------------
    # Called by the main window when a new file loads
    # ------------------------------------------------------------------

    def set_duration(self, duration_ms: int) -> None:
        """Inform the panel of the currently loaded media's length."""
        self._duration_ms = max(duration_ms, 0)
        self._refresh_display()

    def _refresh_display(self) -> None:
        start, end = self._edit_state.effective_trim(self._duration_ms)
        # Block signals while we set text programmatically so we don't
        # re-enter _on_*_edit_finished and cause spurious trimChanged
        # emissions during plain refreshes.
        self._start_edit.blockSignals(True)
        self._end_edit.blockSignals(True)
        self._start_edit.setText(format_ms(start))
        self._end_edit.setText(format_ms(end))
        self._start_edit.blockSignals(False)
        self._end_edit.blockSignals(False)
        self._duration_label.setText(f"Selection: {format_ms(max(0, end - start))}")
        self.trimChanged.emit(start, end)

    # ------------------------------------------------------------------
    # Edit handlers
    # ------------------------------------------------------------------

    def _on_start_edit_finished(self) -> None:
        parsed = parse_time(self._start_edit.text())
        if parsed is None:
            # Revert to the stored value on unparseable input rather than
            # silently accepting something wrong. No modal dialog because
            # that would be overkill for a typo.
            self._refresh_display()
            return
        parsed = max(0, min(self._duration_ms, parsed))
        # Enforce start < end by nudging end if needed. We'd rather keep
        # both values sensible than reject the user's edit outright.
        end = self._edit_state.trim_end_ms
        if end is not None and parsed >= end:
            parsed = max(0, end - 1)
        self._edit_state.trim_start_ms = parsed if parsed > 0 else None
        self._refresh_display()

    def _on_end_edit_finished(self) -> None:
        parsed = parse_time(self._end_edit.text())
        if parsed is None:
            self._refresh_display()
            return
        parsed = max(0, min(self._duration_ms, parsed))
        start = self._edit_state.trim_start_ms or 0
        if parsed <= start:
            parsed = min(self._duration_ms, start + 1)
        # Store None when the end matches the media's duration so
        # "no trim" round-trips cleanly through EditState.
        self._edit_state.trim_end_ms = None if parsed >= self._duration_ms else parsed
        self._refresh_display()

    def _on_reset(self) -> None:
        self._edit_state.trim_start_ms = None
        self._edit_state.trim_end_ms = None
        self._refresh_display()

    # ------------------------------------------------------------------
    # Called by the main window to snap start/end to the playhead
    # ------------------------------------------------------------------

    def set_start_to(self, position_ms: int) -> None:
        position_ms = max(0, min(self._duration_ms, position_ms))
        end = self._edit_state.trim_end_ms
        if end is not None and position_ms >= end:
            position_ms = max(0, end - 1)
        self._edit_state.trim_start_ms = position_ms if position_ms > 0 else None
        self._refresh_display()

    def set_end_to(self, position_ms: int) -> None:
        position_ms = max(0, min(self._duration_ms, position_ms))
        start = self._edit_state.trim_start_ms or 0
        if position_ms <= start:
            position_ms = min(self._duration_ms, start + 1)
        self._edit_state.trim_end_ms = (
            None if position_ms >= self._duration_ms else position_ms
        )
        self._refresh_display()


def _wrap_in_widget(layout) -> QWidget:
    """Turn a QLayout into a QWidget, required by QFormLayout's addRow.

    QFormLayout expects a widget on the right-hand side; passing a bare
    layout works in some Qt versions but is officially undocumented and
    has broken before. Wrapping is a one-line safety measure.
    """
    widget = QWidget()
    layout.setContentsMargins(0, 0, 0, 0)
    widget.setLayout(layout)
    return widget
