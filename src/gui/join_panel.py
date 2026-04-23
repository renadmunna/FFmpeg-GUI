"""Join/merge panel.

Lets the user queue two or more video files and export a single concatenated
MP4. We decouple this tab completely from the single-file editing tabs: it
doesn't share the preview, the trim/crop/transform panels, or the edit
state's filter-chain fields. Its only contact with the rest of the app is
that it reads ``edit_state.join_files`` as its model so opening a new
single-file project does not wipe the user's join queue.

The concat filter we emit requires every input to contain one video stream
and one audio stream. Before accepting a file we probe it and reject any
input that lacks audio; without this guard FFmpeg would fail mid-export
with a cryptic "stream specifier matches no streams" error and the user
would lose the progress dialog state with no useful feedback.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core.edit_state import EditState
from ..core.ffprobe import ProbeError, probe
from ..utils.ffmpeg_locator import FFmpegLocator


# An explicit stylesheet for the queue. Relying on the OS theme was
# producing a near-black selection colour on some systems and an
# essentially invisible hover state on others, so we set readable
# colours here once and let Qt apply them consistently across
# platforms. The selection colour is a Windows/macOS-style blue that
# works well against both light and dark row backgrounds, the text is
# white so contrast stays high, and the hover state is a very low-alpha
# blue overlay that hints at interactivity without competing with the
# selection colour when the user is scanning the list.
_LIST_STYLESHEET = """
QListWidget {
    outline: 0;
    border: 1px solid palette(mid);
    border-radius: 2px;
}
QListWidget::item {
    padding: 6px 8px;
}
QListWidget::item:hover:!selected {
    background-color: rgba(120, 160, 210, 45);
}
QListWidget::item:selected,
QListWidget::item:selected:!active {
    background-color: #3478c6;
    color: white;
}
"""


class _DeselectableListWidget(QListWidget):
    """QListWidget that lets the user click empty space to deselect.

    Qt's ``SingleSelection`` mode enforces "exactly one selected" once
    any item has been clicked: there is no built-in gesture to return
    to the zero-selected state. For this list that is the wrong
    behaviour - the user may want to take their selection off a file
    they just added, or simply scan the list without any row being
    visually called out.

    The fix is to intercept mouse-press events, check whether the click
    landed on any item (``itemAt`` returns ``None`` for clicks below
    the last row, inside the spacing between rows, or into any other
    empty region), and clear the selection before handing the event on
    to Qt's normal handling. Clicks that land on an item fall straight
    through to the base class, so single-selection behaviour is
    preserved for the click-on-item case.
    """

    def mousePressEvent(self, event) -> None:  # noqa: N802  (Qt naming)
        # itemAt accepts local widget coordinates; event.position() is
        # the Qt 6 API that returns a QPointF so we convert to QPoint
        # for itemAt. Using event.pos() also works but is deprecated
        # in Qt 6 documentation.
        pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
        if self.itemAt(pos) is None:
            self.clearSelection()
        super().mousePressEvent(event)


class JoinPanel(QWidget):
    """List-based queue of clips with Add/Remove/Up/Down/Join controls.

    Signals
    -------
    joinRequested(list[str], str):
        Emitted when the user clicks Export. Arguments are the ordered list
        of input paths and the chosen output path. The main window handles
        the actual FFmpeg launch so the join tab stays independent of the
        job-runner/ progress-dialog machinery.
    """

    joinRequested = Signal(list, str)

    def __init__(
        self,
        edit_state: EditState,
        locator: Optional[FFmpegLocator],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._edit_state = edit_state
        # The locator is optional because the main window may still be in
        # "degraded" startup mode with no FFmpeg configured. Storing it
        # as Optional and gating the buttons on its presence keeps this
        # tab consistent with the rest of the UI: actions that cannot
        # work without FFmpeg should be visibly unavailable rather than
        # triggering errors after a click.
        self._locator: Optional[FFmpegLocator] = locator

        # --- list widget holds the current queue --------------------------
        # We use a small QListWidget subclass that supports deselecting
        # by clicking on empty space. Base QListWidget in SingleSelection
        # mode will not let the user return to zero-selected once any
        # row has been clicked, which is confusing here where the user
        # often wants to "just scan" the queue without a row visually
        # claimed by a selection highlight. The accompanying stylesheet
        # gives the hover and selection states consistent, readable
        # colours - the OS defaults were producing a near-black
        # selection on some themes and an essentially invisible hover
        # on others.
        self._list = _DeselectableListWidget(self)
        self._list.setSelectionMode(QListWidget.SingleSelection)
        self._list.setAlternatingRowColors(True)
        self._list.setStyleSheet(_LIST_STYLESHEET)
        # Repopulate from shared state in case the list was previously built
        # and we're re-entering this tab after a settings dialog etc.
        for path in self._edit_state.join_files:
            self._list.addItem(_make_item(path))

        # --- buttons ------------------------------------------------------
        # The two actions that actually spawn ffprobe/ffmpeg - Add (which
        # probes each file before accepting it) and Join (which runs the
        # concat encode) - are kept on self so set_locator() can enable
        # or disable them when the connection state changes. The other
        # buttons (Remove, Move Up/Down, Clear) only touch the in-memory
        # list and stay usable regardless of FFmpeg availability.
        self._btn_add = QPushButton("Add files…", self)
        self._btn_add.clicked.connect(self._on_add)

        btn_remove = QPushButton("Remove", self)
        btn_remove.clicked.connect(self._on_remove)

        btn_up = QPushButton("Move up", self)
        btn_up.clicked.connect(lambda: self._move_selected(-1))

        btn_down = QPushButton("Move down", self)
        btn_down.clicked.connect(lambda: self._move_selected(1))

        btn_clear = QPushButton("Clear", self)
        btn_clear.clicked.connect(self._on_clear)

        self._btn_join = QPushButton("Join && export MP4…", self)
        # Prominent export action gets a bolder style so the eye lands on it
        # over the utility buttons above.
        self._btn_join.setStyleSheet("QPushButton { font-weight: bold; }")
        self._btn_join.clicked.connect(self._on_join)

        # --- layout -------------------------------------------------------
        btn_row = QHBoxLayout()
        btn_row.addWidget(self._btn_add)
        btn_row.addWidget(btn_remove)
        btn_row.addWidget(btn_up)
        btn_row.addWidget(btn_down)
        btn_row.addWidget(btn_clear)
        btn_row.addStretch(1)

        self._status = QLabel(self)
        self._status.setWordWrap(True)
        self._update_status()

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Clips to join (in order):", self))
        layout.addWidget(self._list, 1)
        layout.addLayout(btn_row)
        layout.addWidget(self._status)
        layout.addWidget(self._btn_join)

        # Prime the locator-dependent button states with whatever we were
        # constructed with. After this, every change goes through
        # set_locator() so the state stays in sync.
        self._update_locator_dependent_buttons()

    # ------------------------------------------------------------------
    # Public helpers used by the main window
    # ------------------------------------------------------------------
    def set_locator(self, locator: Optional[FFmpegLocator]) -> None:
        """Swap in a new locator and refresh the dependent button states.

        The main window calls this whenever the user changes the FFmpeg
        path through Preferences. Exposing a named setter - rather than
        letting the main window poke ``self._locator`` directly - keeps
        the "what needs to happen when the locator changes" logic in one
        place, which is the same principle the main window follows with
        its own ``_set_locator`` method.
        """
        self._locator = locator
        self._update_locator_dependent_buttons()

    def _update_locator_dependent_buttons(self) -> None:
        """Enable or disable the buttons that need a working FFmpeg.

        Add files needs ``ffprobe`` to validate each candidate file;
        Join & export needs ``ffmpeg`` to run the concat encode. Remove,
        Move Up/Down, and Clear only touch the in-memory list, so they
        stay enabled always - that way the user can still tidy a stale
        queue even while FFmpeg is not configured.
        """
        has_locator = self._locator is not None
        self._btn_add.setEnabled(has_locator)
        self._btn_join.setEnabled(has_locator)

    def add_paths(self, paths: List[str]) -> None:
        """Add one or more files to the queue, probing each first.

        Called both from the Add-files button and from the main window's
        drag-drop handler when the user drops multiple files onto the
        window while the Join tab is active. The Add button is already
        disabled when no locator is configured, and the main window's
        drop handler gates on a locator before ever calling us, but we
        keep a defensive check here so a future caller that forgets the
        gate fails loudly (with a helpful message) rather than crashing
        inside ffprobe.
        """
        if self._locator is None:
            # This branch is defence-in-depth: current callers do not hit
            # it, but keeping it documented means future refactors cannot
            # break the invariant silently.
            return
        accepted = 0
        for path in paths:
            if self._try_add_one(path):
                accepted += 1
        if accepted:
            self._sync_state_from_list()
            self._update_status()

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------
    def _on_add(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Add video files to join",
            "",
            "Video files (*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.wmv *.flv *.ts);;All files (*.*)",
        )
        if paths:
            self.add_paths(paths)

    def _on_remove(self) -> None:
        row = self._list.currentRow()
        if row >= 0:
            self._list.takeItem(row)
            self._sync_state_from_list()
            self._update_status()

    def _on_clear(self) -> None:
        self._list.clear()
        self._sync_state_from_list()
        self._update_status()

    def _move_selected(self, delta: int) -> None:
        """Move the current row up (delta=-1) or down (delta=+1).

        Implemented by taking the item out and reinserting it at the new
        index; QListWidget has no "swap rows" primitive. We clamp the target
        index so pressing Move-Up on row 0 is a no-op instead of wrapping.
        """
        row = self._list.currentRow()
        if row < 0:
            return
        new_row = row + delta
        if new_row < 0 or new_row >= self._list.count():
            return
        item = self._list.takeItem(row)
        self._list.insertItem(new_row, item)
        self._list.setCurrentRow(new_row)
        self._sync_state_from_list()

    def _on_join(self) -> None:
        """Validate the queue then emit joinRequested for the main window."""
        paths = self._current_paths()
        if len(paths) < 2:
            QMessageBox.information(
                self,
                "Not enough clips",
                "Add at least two clips to join them into one output video.",
            )
            return

        # Pre-fill the save dialog with the first file's location and name 
        # using the " joined" suffix.
        first_path = Path(paths[0])
        default_out = str(first_path.with_name(f"{first_path.stem} joined.mp4"))

        output_path, _ = QFileDialog.getSaveFileName(
            self,
            "Save joined video as…",
            default_out,
            "MP4 video (*.mp4)",
        )
        if not output_path:
            return
        # Force .mp4 extension even if the user typed something else - every
        # output in this app is MP4, and saving a .mov file that's really
        # an MP4 would confuse downstream tools.
        if not output_path.lower().endswith(".mp4"):
            output_path += ".mp4"

        self.joinRequested.emit(paths, output_path)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _try_add_one(self, path: str) -> bool:
        """Probe a candidate file and append it to the list if it is usable.

        Returns True if the file was added. Shows a blocking error message
        and returns False otherwise. We do this synchronously because ffprobe
        returns in milliseconds for local files and a queued/asynchronous
        validation flow would hurt clarity more than it would help latency.
        """
        try:
            media = probe(self._locator, Path(path))
        except ProbeError as err:
            QMessageBox.warning(
                self,
                "Cannot read file",
                f"{Path(path).name}\n\n{err}",
            )
            return False

        if not media.has_audio:
            # Rather than silently inserting a synthetic silent track, we
            # reject and explain. If we silently repaired it, the user
            # would get an output whose audio behavior differed from what
            # they expect, with no UI indication why.
            QMessageBox.warning(
                self,
                "Audio required",
                f"{Path(path).name} has no audio track.\n\n"
                "The join operation concatenates video and audio together, "
                "so every clip must contain both. Add audio to this file "
                "first (for example with an external tool) and try again.",
            )
            return False

        self._list.addItem(_make_item(path))
        return True

    def _current_paths(self) -> List[str]:
        return [self._list.item(i).data(Qt.UserRole) for i in range(self._list.count())]

    def _sync_state_from_list(self) -> None:
        """Mirror the list widget's contents into the shared EditState.

        The main window may persist EditState between panel switches, so
        keeping the model in sync on every mutation means we never have to
        remember to "flush" at export time.
        """
        self._edit_state.join_files = self._current_paths()

    def _update_status(self) -> None:
        count = self._list.count()
        if count == 0:
            self._status.setText("Queue is empty.")
        elif count == 1:
            self._status.setText("1 clip queued - add at least one more to join.")
        else:
            self._status.setText(f"{count} clips queued and ready to join.")


def _make_item(path: str) -> QListWidgetItem:
    """Build a display item that shows the basename but carries the full path.

    Storing the full path in Qt.UserRole means we can show a clean filename
    in the UI (important for long paths) while still having the canonical
    path available when we collect the export arguments.
    """
    item = QListWidgetItem(Path(path).name)
    item.setData(Qt.UserRole, path)
    # Tooltip shows the full path so the user can disambiguate files with
    # the same basename (e.g. two "clip.mp4" files from different folders).
    item.setToolTip(path)
    return item