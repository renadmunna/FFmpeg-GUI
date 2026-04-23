"""Main application window.

This module is the "switchboard" of the application. Every edit panel we
built earlier is deliberately self-contained: the trim panel emits
``seekRequested`` but has no reference to the preview; the crop panel emits
``cropRectChanged`` but has no reference to the overlay. The main window is
where those signals get wired together into a working application.

The integration model is:

* One shared :class:`EditState` instance lives on the main window. Every
  panel receives a reference to it at construction time. When a panel
  mutates the state, it emits a signal; the main window's slot reads the
  new state and pushes it into the other widgets that need to know.
* The main window owns the preview, the panels, the menu bar, the status
  bar, and the drag-drop policy. It does not own any FFmpeg-related
  logic beyond command assembly.
* Opening a new file resets the per-file fields on the EditState (trim,
  crop, rotation, flips) and reconfigures every panel to the new source
  dimensions and duration. ``join_files`` is intentionally not reset so
  the join queue survives opening unrelated files in the single-edit tab.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QDragEnterEvent, QDropEvent, QKeySequence
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStatusBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..core.edit_state import CropRect, EditState
from ..core.ffmpeg_runner import (
    FFmpegJob,
    build_copy_command,
    build_edit_command,
    build_join_command,
)
from ..core.ffprobe import ProbeError, probe
from ..core.media_info import MediaInfo
from ..utils.ffmpeg_locator import FFmpegLocator, FFmpegNotFound
from ..utils.settings import AppSettings
from ..utils.time_utils import format_ms
from .about_dialog import AboutDialog
from .crop_panel import CropPanel
from .join_panel import JoinPanel
from .preferences_dialog import PreferencesDialog
from .preview import VideoPreview
from .transform_panel import TransformPanel
from .trim_panel import TrimPanel


# Accepted input extensions. We do not rely on the OS's MIME database
# because it is inconsistent across platforms and because FFmpeg can
# often read files whose extension is slightly wrong. Still, we list
# the common ones to filter drops and file-dialog selections so the
# user does not accidentally load a .txt file.
_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".wmv", ".flv", ".ts"}


class MainWindow(QMainWindow):
    """Top-level application window.

    Parameters
    ----------
    locator:
        A successfully-discovered :class:`FFmpegLocator`, or ``None`` if
        startup discovery failed. A ``None`` locator is not a fatal error:
        the window still opens, the Preferences menu stays reachable so
        the user can configure a path, and every FFmpeg-dependent action
        is disabled or guarded until a working locator is available. This
        is a deliberate departure from a harder "refuse to launch without
        FFmpeg" policy - if the app refused to open, the Preferences menu
        would be unreachable and the user would have no way to fix the
        situation through the UI.
    settings:
        The persistent :class:`AppSettings` facade. We hold a reference
        so the Preferences and About dialogs can read and write the
        same underlying store without having to reach for a global.
    initial_error:
        The :class:`FFmpegNotFound` exception raised by startup discovery,
        if any. We use its ``details`` attribute to show a friendly
        "no FFmpeg was found, would you like to configure a path?"
        prompt shortly after the window appears.
    """

    def __init__(
        self,
        locator: Optional[FFmpegLocator],
        settings: AppSettings,
        initial_error: Optional[FFmpegNotFound] = None,
    ) -> None:
        super().__init__()
        self._locator: Optional[FFmpegLocator] = locator
        self._settings = settings
        self._initial_error = initial_error
        self._state = EditState()
        self._media: Optional[MediaInfo] = None
        
        self._job: Optional[FFmpegJob] = None
        self._current_output_path: Optional[Path] = None

        # Action references captured during menu construction so
        # _update_locator_dependent_actions() can toggle them. Storing
        # these on self (rather than leaving them as local variables
        # inside _build_menu_bar) is the smallest change that turns
        # them into first-class state.
        self._act_open: Optional[QAction] = None
        self._act_open_multi: Optional[QAction] = None

        # Accepting drops at the window level means the user can drop a
        # file anywhere in the application (not just on the preview),
        # which matches how people usually try it the first time.
        self.setAcceptDrops(True)
        self.setWindowTitle("FFmpeg GUI")
        self.resize(1180, 760)

        # --- central layout ----------------------------------------------
        # The left side shows the preview; the right side shows the edit
        # tabs. A splitter lets the user drag the boundary so they can
        # give either panel more room as their workflow demands.
        self._preview = VideoPreview(self._state, self)
        self._trim_panel = TrimPanel(self._state, self)
        self._crop_panel = CropPanel(self._state, self)
        self._transform_panel = TransformPanel(self._state, self)
        self._join_panel = JoinPanel(self._state, self._locator, self)

        self._tabs = QTabWidget(self)
        self._tabs.addTab(self._trim_panel, "Trim")
        self._tabs.addTab(self._crop_panel, "Crop")
        self._tabs.addTab(self._transform_panel, "Rotate / Flip")
        self._tabs.addTab(self._join_panel, "Join")

        splitter = QSplitter(Qt.Horizontal, self)
        splitter.addWidget(self._preview)
        splitter.addWidget(self._tabs)
        # Give the preview roughly two-thirds of the width on first launch.
        # setStretchFactor is preferable to setSizes here because it still
        # does the right thing when the user resizes the window.
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 1)

        # Bottom export bar: output path + Export button. Keeping this
        # outside the tab widget means the export control stays visible
        # regardless of which edit tab the user has selected, which makes
        # the workflow feel more like "set parameters, click export" and
        # less like "hunt for the export button".
        self._output_edit = QLineEdit(self)
        self._output_edit.setPlaceholderText("Output file… (choose a location with Browse)")
        self._output_edit.setReadOnly(True)

        self._btn_browse = QPushButton("Browse…", self)
        self._btn_browse.clicked.connect(self._choose_output_path)

        self._btn_export = QPushButton("Export MP4", self)
        self._btn_export.setStyleSheet("QPushButton { font-weight: bold; padding: 6px 14px; }")
        self._btn_export.clicked.connect(self._on_export_clicked)
        # Export is disabled until a file is loaded, so we don't display a
        # button that can't do anything useful.
        self._btn_export.setEnabled(False)

        export_row = QHBoxLayout()
        export_row.addWidget(QLabel("Output:", self))
        export_row.addWidget(self._output_edit, 1)
        export_row.addWidget(self._btn_browse)
        export_row.addWidget(self._btn_export)

        # Bottom progress bar and inline status for export handling
        self._progress = QProgressBar(self)
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setVisible(False)

        self._btn_cancel_export = QPushButton("Cancel", self)
        self._btn_cancel_export.clicked.connect(self._on_cancel_export)
        self._btn_cancel_export.setVisible(False)

        progress_row = QHBoxLayout()
        progress_row.addWidget(self._progress, 1)
        progress_row.addWidget(self._btn_cancel_export)

        self._export_status_label = QLabel("", self)
        self._export_status_label.setWordWrap(True)
        self._export_status_label.setVisible(False)

        central = QWidget(self)
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(8, 8, 8, 8)
        central_layout.addWidget(splitter, 1)
        central_layout.addLayout(export_row)
        central_layout.addWidget(self._export_status_label)
        central_layout.addLayout(progress_row)
        self.setCentralWidget(central)

        # --- status bar ---------------------------------------------------
        # We build our own QStatusBar so we can hold a reference to the
        # permanent-metadata label. The default status bar created by
        # QMainWindow is fine for showMessage() but awkward for
        # addPermanentWidget() if you didn't construct it yourself.
        #
        # Layout: file info on the left (stretches), FFmpeg connection
        # indicator pinned to the right as a "permanent" widget so it is
        # always visible regardless of the file-info text length.
        self._status_label = QLabel("No file loaded.", self)
        self._ffmpeg_status_label = QLabel(self)
        self._ffmpeg_status_label.setTextFormat(Qt.RichText)
        # Giving the label a subtle frame makes it feel like a status
        # chip rather than drifting text; the padding stops the coloured
        # dot from hugging the window edge.
        self._ffmpeg_status_label.setStyleSheet(
            "QLabel { padding: 2px 8px; }"
        )

        status_bar = QStatusBar(self)
        status_bar.addWidget(self._status_label, 1)
        status_bar.addPermanentWidget(self._ffmpeg_status_label)
        self.setStatusBar(status_bar)

        # Populate the FFmpeg status indicator from the locator we were
        # given. Doing this here, after the status bar exists, keeps the
        # initialisation ordering straightforward: widgets first, content
        # second. _refresh_ffmpeg_status is also called later whenever
        # the user changes the path through Preferences.
        self._refresh_ffmpeg_status()

        # --- menu bar -----------------------------------------------------
        self._build_menu_bar()

        # --- signal wiring ------------------------------------------------
        self._connect_signals()

        # Sync the initial enabled/disabled state of every locator-dependent
        # action. This must come after _build_menu_bar (which populates
        # self._act_open and friends) but before the welcome prompt (so the
        # user sees the correct button states the instant the window paints).
        self._update_locator_dependent_actions()

        # If the startup discovery failed, schedule a friendly prompt that
        # directs the user to Preferences. QTimer.singleShot(0, ...) posts
        # the callback at the end of the current event-loop iteration, so
        # Qt finishes drawing the main window first and the dialog appears
        # layered on top - much nicer than an error popup that springs up
        # before the user has even seen the application.
        if self._locator is None:
            QTimer.singleShot(0, self._show_welcome_prompt)

    # ==================================================================
    # Menu construction
    # ==================================================================
    def _build_menu_bar(self) -> None:
        """Populate the menu bar with File, Edit, and Help menus.

        We keep rotation and flip actions in the Edit menu in addition to
        the Rotate / Flip tab so keyboard-oriented users can rotate without
        switching tabs. The tab widget and the menu both mutate the same
        EditState, so the two stay in sync automatically.
        """
        mb = self.menuBar()

        # --- File ---------------------------------------------------------
        file_menu = mb.addMenu("&File")

        # We keep references to these two actions on self because their
        # enabled state depends on whether a working FFmpegLocator is
        # available. _update_locator_dependent_actions() reaches in and
        # flips them whenever the locator changes.
        self._act_open = QAction("&Open video…", self)
        self._act_open.setShortcut(QKeySequence.Open)
        self._act_open.triggered.connect(self._on_open_file)
        file_menu.addAction(self._act_open)

        self._act_open_multi = QAction("Open multiple for &join…", self)
        self._act_open_multi.triggered.connect(self._on_open_multiple_for_join)
        file_menu.addAction(self._act_open_multi)

        file_menu.addSeparator()

        act_exit = QAction("E&xit", self)
        act_exit.setShortcut(QKeySequence.Quit)
        act_exit.triggered.connect(self.close)
        file_menu.addAction(act_exit)

        # --- Edit ---------------------------------------------------------
        # Every rotation and flip command, whether triggered from the
        # menu or from the Transform tab, ultimately flows through the
        # transform panel. That way the panel is the single source of
        # truth for rotation and flip state, and its visible UI (the
        # rotation label and the flip checkboxes) stays in sync with the
        # preview and the EditState without any extra wiring. If the
        # menu used to set state directly, the Transform tab's label
        # would still show the old degrees after a menu-driven rotation -
        # a subtle but confusing mismatch.
        edit_menu = mb.addMenu("&Edit")

        rotate_menu = edit_menu.addMenu("&Rotate")
        rotate_menu.addAction(self._make_action(
            "Rotate 90° clockwise",
            lambda: self._transform_panel.rotate_delta(90),
        ))
        rotate_menu.addAction(self._make_action(
            "Rotate 90° counter-clockwise",
            lambda: self._transform_panel.rotate_delta(-90),
        ))
        rotate_menu.addAction(self._make_action(
            "Rotate 180°",
            lambda: self._transform_panel.rotate_delta(180),
        ))
        rotate_menu.addSeparator()
        rotate_menu.addAction(self._make_action(
            "Reset rotation",
            self._transform_panel.reset_rotation,
        ))

        flip_menu = edit_menu.addMenu("&Flip")
        flip_menu.addAction(self._make_action(
            "Flip horizontal",
            self._transform_panel.toggle_flip_horizontal,
        ))
        flip_menu.addAction(self._make_action(
            "Flip vertical",
            self._transform_panel.toggle_flip_vertical,
        ))

        # --- Preferences --------------------------------------------------
        # Exposed as its own top-level menu rather than tucked inside File
        # or Edit, because a user who wants to change where the app finds
        # FFmpeg should not have to guess which menu that lives under.
        # Today the menu has one item; adding more later (default export
        # folder, preferred encoder, etc.) is a matter of appending rows
        # to the Preferences dialog without moving any menu entries.
        pref_menu = mb.addMenu("&Preferences")
        act_ffmpeg_path = QAction("&FFmpeg path…", self)
        act_ffmpeg_path.setStatusTip(
            "Set a custom folder for ffmpeg and ffprobe."
        )
        act_ffmpeg_path.triggered.connect(self._on_open_preferences)
        pref_menu.addAction(act_ffmpeg_path)

        # --- Help ---------------------------------------------------------
        help_menu = mb.addMenu("&Help")
        act_about = QAction("&About FFmpeg GUI", self)
        act_about.triggered.connect(self._show_about)
        help_menu.addAction(act_about)

    def _make_action(self, text: str, slot) -> QAction:
        """Tiny factory for menu actions whose only setup is a triggered slot.

        Keeps the menu-construction code above readable by collapsing three
        lines (create, connect, return) into one call per item.
        """
        action = QAction(text, self)
        action.triggered.connect(slot)
        return action

    # ==================================================================
    # Signal wiring
    # ==================================================================
    def _connect_signals(self) -> None:
        """Route every inter-panel signal through the main window.

        This is deliberately a single method so the full graph of panel
        interactions is visible in one place. Each line corresponds to one
        user-observable behavior; reading them top to bottom should describe
        the application's dataflow.
        """
        # Preview tells us about playback position and duration. The trim
        # panel uses position for "Set start/end from playhead" and uses
        # duration to configure the upper bound on its time fields.
        self._preview.positionChanged.connect(self._on_preview_position_changed)
        self._preview.durationChanged.connect(self._trim_panel.set_duration)

        # Preview also tells us when the user drags a crop on the overlay.
        # We push the new rectangle into the crop panel (so its numeric
        # spinboxes update) and into the shared state.
        self._preview.cropChanged.connect(self._on_preview_crop_changed)

        # Crop panel tells us when the user edits a spinbox. We push the
        # rectangle into the overlay (so it moves on screen) and into the
        # shared state.
        self._crop_panel.cropRectChanged.connect(self._on_crop_panel_rect_changed)
        self._crop_panel.cropResetRequested.connect(self._on_crop_reset)
        self._crop_panel.aspectRatioChanged.connect(self._preview.set_crop_aspect_ratio)

        # Trim panel: the main window supplies the current playhead when
        # asked, and routes seek requests into the preview.
        self._trim_panel.trimChanged.connect(self._on_trim_changed)
        self._trim_panel.seekRequested.connect(self._preview.set_position_ms)
        self._trim_panel.setStartRequested.connect(self._on_trim_set_start_requested)
        self._trim_panel.setEndRequested.connect(self._on_trim_set_end_requested)

        # Transform panel: rotation/flip changes update both the preview
        # (for immediate visual feedback) and the state (for export).
        self._transform_panel.transformChanged.connect(self._on_transform_changed)

        # Join panel: the main window handles the actual FFmpeg launch so
        # the join panel does not need to know about the export dialog or
        # the runner.
        self._join_panel.joinRequested.connect(self._on_join_requested)

    # ==================================================================
    # Drag-and-drop
    # ==================================================================
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        """Accept the drag only when the payload contains one or more files.

        Qt will call this when the mouse enters the window with a drag in
        progress. If we don't accept here, the subsequent drop is blocked.
        """
        if event.mimeData().hasUrls() and all(
            url.isLocalFile() for url in event.mimeData().urls()
        ):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        """Decide whether a drop targets single-edit mode or join mode.

        Rule: if the user is already on the Join tab OR they drop multiple
        files at once, treat it as a join add. Otherwise load the first
        file for single-file editing. This rule is conservative - dropping
        two files while on the Trim tab switches to Join rather than
        silently discarding one file.
        """
        urls = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        video_paths = [p for p in urls if Path(p).suffix.lower() in _VIDEO_EXTS]
        if not video_paths:
            QMessageBox.information(
                self,
                "Unsupported file",
                "Drop a video file (mp4, mov, mkv, avi, webm, m4v, wmv, flv, or ts).",
            )
            event.ignore()
            return

        # Opening a file requires ffprobe to read its metadata, so if we
        # have no locator we cannot proceed. _require_locator shows the
        # user a helpful "open Preferences?" prompt and returns False.
        # Ignoring the drop event lets Qt restore the drag's original
        # visual state (file bounces back rather than being accepted).
        if not self._require_locator():
            event.ignore()
            return

        on_join_tab = self._tabs.currentWidget() is self._join_panel
        if len(video_paths) > 1 or on_join_tab:
            # Switch to the Join tab so the user sees the effect of the drop.
            self._tabs.setCurrentWidget(self._join_panel)
            self._join_panel.add_paths(video_paths)
        else:
            self._load_file(Path(video_paths[0]))
        event.acceptProposedAction()

    # ==================================================================
    # File loading
    # ==================================================================
    def _on_open_file(self) -> None:
        if not self._require_locator():
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open video",
            "",
            "Video files (*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.wmv *.flv *.ts);;All files (*.*)",
        )
        if path:
            self._load_file(Path(path))

    def _on_open_multiple_for_join(self) -> None:
        if not self._require_locator():
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Open videos to join",
            "",
            "Video files (*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.wmv *.flv *.ts);;All files (*.*)",
        )
        if paths:
            self._tabs.setCurrentWidget(self._join_panel)
            self._join_panel.add_paths(paths)

    def _load_file(self, path: Path) -> None:
        """Probe the file, load it into the preview, and reset all panels.

        Any failure here is fatal to the load attempt but not to the app -
        we show a message and leave the previous state intact, so the user
        can drop another file without restarting. The defensive
        ``_locator is None`` check should be unreachable because every
        caller gates on _require_locator first, but keeping it here means
        a future code path that forgets that gate cannot crash the app.
        """
        if self._locator is None:
            self._require_locator()  # shows the prompt
            return
        try:
            media = probe(self._locator, path)
        except ProbeError as err:
            QMessageBox.warning(self, "Cannot open file", str(err))
            return

        self._media = media

        # Reset per-file state before configuring widgets, so the panels
        # pick up clean defaults. join_files is preserved by EditState.reset.
        self._state.reset()

        # Configure widgets for the new source. Note the order: we must set
        # the preview's source size *before* any crop rectangle is pushed
        # into it, because the overlay clamps against those bounds.
        self._preview.load(media)
        self._crop_panel.set_source_size(media.width, media.height)
        self._transform_panel.reset()
        # TrimPanel's set_duration is also triggered by the preview's
        # durationChanged signal, but setting it explicitly here means the
        # numeric fields are correct even before the media pipeline has
        # emitted durationChanged (a race that briefly shows 00:00:00.000).
        self._trim_panel.set_duration(media.duration_ms)

        # Update the status bar with the file's metadata.
        self._status_label.setText(
            f"{path.name}   |   {media.width}×{media.height}   |   "
            f"{format_ms(media.duration_ms)}   |   {media.fps:.2f} fps"
        )

        # Suggest an output path next to the input file with an _edited
        # suffix. The user can override this with Browse before exporting.
        suggested = path.with_name(f"{path.stem}_edited.mp4")
        self._output_edit.setText(str(suggested))

        # Re-evaluate locator-dependent actions. The Export button, in
        # particular, becomes enabled now that both a locator and a
        # loaded file are available. Centralising the check means this
        # line stays a single call regardless of how many conditions the
        # enabled-state rule picks up later.
        self._update_locator_dependent_actions()
        self.setWindowTitle(f"FFmpeg GUI - {path.name}")

    # ==================================================================
    # Preview-driven slots
    # ==================================================================
    def _on_preview_position_changed(self, position_ms: int) -> None:
        """Relay playback position where the trim panel needs it.

        TrimPanel exposes set_start_to / set_end_to which read the current
        playhead. We could have the trim panel reach into the preview
        directly, but keeping the reference one-way (main window knows
        the preview, trim panel knows the main window via signals) keeps
        the panels trivially replaceable.
        """
        # Intentionally a no-op body for now: TrimPanel requests the playhead
        # on demand via setStartRequested / setEndRequested, which we handle
        # by pulling self._preview.position_ms() at that moment. We keep the
        # connection wired so future UI elements (e.g. a playhead readout
        # on the trim panel) can attach here without extra plumbing.
        pass

    def _on_preview_crop_changed(self, crop: Optional[CropRect]) -> None:
        """User moved the visual crop overlay: update panel + state."""
        self._crop_panel.update_from_overlay(crop)
        self._state.crop = crop

    # ==================================================================
    # Panel-driven slots
    # ==================================================================
    def _on_crop_panel_rect_changed(self, crop: CropRect) -> None:
        """User typed new crop numbers: update overlay + state."""
        self._preview.set_crop_rect_from_source(crop)
        self._state.crop = crop

    def _on_crop_reset(self) -> None:
        """Reset button on the crop panel: clear both overlay and state."""
        self._preview.set_crop_rect_from_source(None)
        self._crop_panel.update_from_overlay(None)
        self._state.crop = None

    def _on_trim_changed(self, start_ms: int, end_ms: int) -> None:
        """TrimPanel reports a new start/end pair; write them into state.

        We allow ``end_ms == duration`` to represent "no trim end"; the
        command builder treats ``None`` specially to omit the -to flag, so
        we only set the state's trim_end_ms when the user actually trimmed.
        """
        duration = self._media.duration_ms if self._media else 0
        self._state.trim_start_ms = start_ms if start_ms > 0 else None
        self._state.trim_end_ms = end_ms if (duration and end_ms < duration) else None

    def _on_trim_set_start_requested(self) -> None:
        self._trim_panel.set_start_to(self._preview.position_ms())

    def _on_trim_set_end_requested(self) -> None:
        self._trim_panel.set_end_to(self._preview.position_ms())

    def _on_transform_changed(self, rotation: int, flip_h: bool, flip_v: bool) -> None:
        """Sync rotation/flip changes into the state and the live preview.

        This slot is the single integration point for every rotation or
        flip event in the application. Whether the user clicked a button
        on the Transform tab, toggled a checkbox there, or picked an
        item from the Edit menu, the request flowed through the
        transform panel and emitted ``transformChanged`` before landing
        here. That discipline is what keeps the panel's label and
        checkboxes authoritative - nothing else in the application
        mutates rotation or flip state directly any more.
        """
        self._state.rotation = rotation
        self._state.flip_horizontal = flip_h
        self._state.flip_vertical = flip_v
        self._preview.apply_transform(rotation, flip_h, flip_v)

    # ==================================================================
    # Export logic
    # ==================================================================
    def _choose_output_path(self) -> None:
        """Let the user pick a save location; force the .mp4 extension."""
        current = self._output_edit.text() or ""
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save exported video as…",
            current,
            "MP4 video (*.mp4)",
        )
        if not path:
            return
        if not path.lower().endswith(".mp4"):
            path += ".mp4"
        self._output_edit.setText(path)

    def _on_export_clicked(self) -> None:
        """Build and launch the FFmpeg export for the current edit state."""
        if self._media is None:
            return
        # Building the command needs self._locator.ffmpeg. If the user
        # somehow triggered the export button in a state where no locator
        # exists (e.g. shortcut key while the button was briefly enabled
        # between preference changes), _require_locator both guards against
        # the crash and invites them into Preferences.
        if not self._require_locator():
            return
        output_text = self._output_edit.text().strip()
        if not output_text:
            self._choose_output_path()
            output_text = self._output_edit.text().strip()
            if not output_text:
                return
        output_path = Path(output_text)

        # Decide whether we can stream-copy (fast, lossless) or need to
        # re-encode. Stream copy is only valid if the user made zero edits
        # on the current file; any crop/rotate/flip/trim requires filters
        # or re-muxing with precise timestamps.
        state_is_clean = (
            self._state.trim_start_ms is None
            and self._state.trim_end_ms is None
            and self._state.crop is None
            and self._state.rotation == 0
            and not self._state.flip_horizontal
            and not self._state.flip_vertical
        )
        if state_is_clean:
            command = build_copy_command(self._locator, self._media, output_path)
        else:
            command = build_edit_command(self._locator, self._media, self._state, output_path)

        # Duration the progress bar needs to fill. If the user trimmed,
        # FFmpeg will only write that many milliseconds, so using the raw
        # source duration would make the bar stop at, say, 12% for a 12-
        # second trim of a 100-second file.
        start_ms, end_ms = self._state.effective_trim(self._media.duration_ms)
        progress_duration_ms = max(1, end_ms - start_ms)

        self._run_export(command, progress_duration_ms, output_path)

    def _on_join_requested(self, input_paths: List[str], output_path: str) -> None:
        """Handle the join panel's export request end-to-end.

        The sum of input durations is what FFmpeg will emit as "time=" during
        a concat encode, so that's the duration we feed to the progress bar.
        We also collect the full :class:`MediaInfo` for each input because
        the join command builder needs each clip's resolution and frame
        rate to normalise them to a common target - without that step,
        joining clips of mismatched resolution would fail inside FFmpeg's
        concat filter. The locator guard at the top is defensive: the
        join panel itself disables its Export button when no locator is
        configured, so in practice this path runs only with a valid
        locator.
        """
        if not self._require_locator():
            return
        paths = [Path(p) for p in input_paths]
        media_infos: List[MediaInfo] = []
        total_duration_ms = 0
        for path in paths:
            try:
                info = probe(self._locator, path)
            except ProbeError as err:
                QMessageBox.warning(
                    self,
                    "Cannot read file for join",
                    f"{path.name}\n\n{err}",
                )
                return
            media_infos.append(info)
            total_duration_ms += info.duration_ms

        command = build_join_command(
            self._locator, paths, media_infos, Path(output_path)
        )
        self._run_export(command, max(1, total_duration_ms), Path(output_path))

    def _run_export(
        self,
        command: List[str],
        total_duration_ms: int,
        output_path: Path,
    ) -> None:
        """Launch the FFmpeg background job and display progress inline.

        All three code paths (single-file edit, single-file copy, join)
        funnel through this method so the final success/error UX is
        identical for all of them.
        """
        if self._job is not None and self._job.thread.isRunning():
            return

        self._current_output_path = output_path

        # Disable interactive UI elements during export to prevent state
        # mutations while FFmpeg is running.
        self._btn_export.setEnabled(False)
        self._btn_browse.setEnabled(False)
        self._tabs.setEnabled(False)
        
        self._progress.setValue(0)
        self._progress.setVisible(True)
        
        self._btn_cancel_export.setEnabled(True)
        self._btn_cancel_export.setVisible(True)
        
        self._export_status_label.setText(f"Exporting to {output_path.name}...")
        self._export_status_label.setStyleSheet("color: palette(text);")
        self._export_status_label.setVisible(True)

        # FFmpeg log strings are deliberately ignored in this setup, as we 
        # solely rely on the progress value to move the bar.
        self._job = FFmpegJob(command, total_duration_ms)
        self._job.worker.progress.connect(self._progress.setValue)
        self._job.worker.finished.connect(self._on_export_finished)
        self._job.start()

    def _on_export_finished(self, success: bool, message: str) -> None:
        """Clean up the export run and restore UI interactions."""
        # Restore control layout
        self._btn_browse.setEnabled(True)
        self._tabs.setEnabled(True)
        self._update_locator_dependent_actions()
        
        self._btn_cancel_export.setVisible(False)
        
        if success:
            self._progress.setValue(100)
            self._export_status_label.setText(f"Export complete: {self._current_output_path.name}")
            self._export_status_label.setStyleSheet("color: #27ae60; font-weight: bold;")
        else:
            if message == "Cancelled by user.":
                self._export_status_label.setText("Export cancelled.")
                self._export_status_label.setStyleSheet("color: #c0392b; font-weight: bold;")
                self._progress.setVisible(False)
            else:
                self._export_status_label.setText(f"Export failed: {message}")
                self._export_status_label.setStyleSheet("color: #c0392b; font-weight: bold;")
                self._progress.setVisible(False)
        
        self._job = None

    def _on_cancel_export(self) -> None:
        """User pressed the Cancel button during an active export run."""
        if self._job is not None:
            self._btn_cancel_export.setEnabled(False)
            self._export_status_label.setText("Cancelling the export - waiting for FFmpeg to stop…")
            self._job.cancel()

    # ==================================================================
    # Window events
    # ==================================================================
    def closeEvent(self, event) -> None:
        """Block the user from closing the application while an export is active."""
        if self._job is not None and self._job.thread.isRunning():
            answer = QMessageBox.question(
                self,
                "Cancel export?",
                "An export is currently running. Cancel it and exit?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer == QMessageBox.Yes:
                self._job.cancel()
                self._job.thread.wait(3000)
                event.accept()
            else:
                event.ignore()
        else:
            event.accept()

    # ==================================================================
    # Miscellaneous UI
    # ==================================================================
    def _show_about(self) -> None:
        """Open the detailed About dialog.

        The dialog reads live state from the current locator and settings,
        so simply constructing a fresh instance on every invocation gives
        us up-to-date content without any refresh plumbing. When the user
        changes the FFmpeg path and then re-opens About, the new values
        appear automatically.
        """
        dialog = AboutDialog(self._locator, self._settings, self)
        dialog.exec()

    # ==================================================================
    # Preferences
    # ==================================================================
    def _on_open_preferences(self) -> None:
        """Show the Preferences dialog and react to a successful save.

        We connect to ``preferencesSaved`` rather than reading the new
        value out of the dialog directly, so the dialog stays the single
        source of truth about how preferences are persisted. If we later
        add more preference fields, this wiring does not change.
        """
        dialog = PreferencesDialog(self._settings, self)
        dialog.preferencesSaved.connect(self._on_preferences_saved)
        dialog.exec()

    def _on_preferences_saved(self) -> None:
        """Rediscover FFmpeg with the new preference and refresh the UI.

        Three outcomes are possible:

        1. Discovery succeeds and finds FFmpeg. We swap in the new
           locator everywhere it is held, refresh the status chip, and
           enable the locator-dependent menu actions.
        2. Discovery succeeds but falls back to a different source than
           the one the user asked for (e.g. the custom path was empty and
           the bundled ``bin/`` folder had the binaries). We still swap
           in the new locator - it is a valid connection - and the status
           chip's tooltip will tell the user where the binaries came from.
        3. Discovery fails entirely. We keep whatever locator we had
           before (which may be ``None`` if the app launched in degraded
           mode), show a warning whose wording depends on whether there
           was a previous connection, and leave action states as they
           were. The user can reopen Preferences and try again.

        In every case we refresh the status chip so the user has visual
        confirmation that their action was processed.
        """
        preferred = self._settings.ffmpeg_path()
        try:
            new_locator = FFmpegLocator.discover(preferred_dir=preferred)
        except FFmpegNotFound as err:
            had_previous = self._locator is not None
            tail = (
                "The previous connection is still in use."
                if had_previous
                else "The application remains without an FFmpeg connection. "
                "Open Preferences again to try a different path."
            )
            QMessageBox.warning(
                self,
                "FFmpeg not found with new preference",
                "The application could not find FFmpeg after applying your "
                f"preference change. {tail}<br><br>"
                f"<pre>{err.details}</pre>",
            )
            self._refresh_ffmpeg_status()
            return

        # Success path: one call updates every holder of the locator and
        # every piece of UI that depends on it.
        self._set_locator(new_locator)

    # ==================================================================
    # Locator lifecycle
    # ==================================================================
    def _require_locator(self) -> bool:
        """Return True when a locator is available; otherwise prompt the user.

        This is the single guard used by every entry point that depends
        on FFmpeg (opening a file, exporting, joining). Centralising the
        "do we have a locator?" check means each caller reads as a single
        clean line - ``if not self._require_locator(): return`` - and the
        wording of the prompt stays identical whether the user triggered
        the action from the menu, a drag-drop, or a keyboard shortcut.

        When there is no locator, we ask whether the user wants to open
        the Preferences dialog right now. Saying Yes jumps straight to
        the place where the problem can be fixed; saying No lets the
        user continue in the degraded state without being pestered.
        After a successful configuration inside Preferences, the method
        returns True because ``self._locator`` has been updated by
        ``_set_locator``.
        """
        if self._locator is not None:
            return True

        answer = QMessageBox.question(
            self,
            "FFmpeg not configured",
            "This action needs FFmpeg, but no FFmpeg installation is "
            "currently configured.<br><br>"
            "Would you like to open Preferences and set a path now?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if answer == QMessageBox.Yes:
            # _on_open_preferences is modal, so control returns here only
            # after the dialog closes. By that point _set_locator may have
            # already run (if the user configured a working path), so the
            # freshly-updated self._locator tells us whether to proceed.
            self._on_open_preferences()
        return self._locator is not None

    def _set_locator(self, new_locator: FFmpegLocator) -> None:
        """Install a new locator and update every piece of UI that depends on it.

        This is the single success path used by both the startup flow
        (via __init__) and by the preferences-saved flow. Having exactly
        one method that "installs" a locator means future holders of the
        locator only need to be added here, and any locator-dependent UI
        update can be guaranteed to run alongside the assignment.
        """
        self._locator = new_locator
        # The join panel caches the locator so its probe calls use the
        # right binary. We update it through a public setter rather than
        # reaching into the attribute directly.
        self._join_panel.set_locator(new_locator)
        self._refresh_ffmpeg_status()
        self._update_locator_dependent_actions()

    def _update_locator_dependent_actions(self) -> None:
        """Enable or disable menu items based on whether FFmpeg is reachable.

        Menu items that cannot function without FFmpeg (Open video, Open
        multiple for join) are greyed out when ``self._locator`` is
        ``None``. This is honest UI: users can see at a glance which
        features are available right now instead of discovering the
        situation only when they click and get a dialog. The Preferences
        and About entries stay enabled always, because they are the
        exact places a user goes to fix or understand the problem.

        We also keep the Export button's enabled state in sync here even
        though it has two conditions (a loaded file *and* a locator).
        Re-evaluating both every time keeps the button truthful without
        a separate tracking flag.
        """
        has_locator = self._locator is not None

        # Guard each setEnabled call because _build_menu_bar may not have
        # run yet if __init__ is still executing. The attributes exist as
        # None placeholders (set at the top of __init__) until menu
        # construction assigns the real actions.
        if self._act_open is not None:
            self._act_open.setEnabled(has_locator)
        if self._act_open_multi is not None:
            self._act_open_multi.setEnabled(has_locator)

        # Export needs both a locator and a loaded media file. The same
        # method is the authoritative place to evaluate this combination.
        self._btn_export.setEnabled(has_locator and self._media is not None)

    def _show_welcome_prompt(self) -> None:
        """Explain the missing-FFmpeg situation once the window is visible.

        Called from a ``QTimer.singleShot(0, ...)`` scheduled in the
        constructor when startup discovery failed. Waiting a tick means
        the main window has already painting, so the dialog appears on
        top of the UI the user expects - not instead of it. The brief
        ``tried`` list gives some concrete context; for deeper detail
        the user can open Preferences, where the live test runs the same
        discovery call and shows the result interactively.
        """
        # Double-check the state here because the user could in theory
        # have dismissed the timer's delivery by moving the window or by
        # some other asynchronous event; belt-and-braces.
        if self._locator is not None:
            return

        details_block = ""
        if self._initial_error is not None:
            details_block = (
                "<br>We looked in these locations (in order):<br>"
                f"<pre>{self._initial_error.details}</pre>"
            )

        answer = QMessageBox.question(
            self,
            "Welcome - FFmpeg not configured",
            "<b>FFmpeg GUI could not find an FFmpeg installation.</b>"
            "<br><br>"
            "The application needs both <code>ffmpeg</code> and "
            "<code>ffprobe</code> to edit videos. You can set a custom "
            "path now through the Preferences menu, or continue and "
            "configure it later. Every FFmpeg-dependent action is "
            "disabled until a working path is available."
            f"{details_block}"
            "<br>Would you like to open Preferences now?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if answer == QMessageBox.Yes:
            self._on_open_preferences()

    # ==================================================================
    # FFmpeg status chip
    # ==================================================================
    def _refresh_ffmpeg_status(self) -> None:
        """Update the small status label in the bottom-right status bar.

        The label is a compact "chip" - coloured dot followed by a short
        version string - with a tooltip carrying the full path so power
        users can see exactly which binary is in use without opening the
        About dialog.

        We call the locator's ``version()`` method (which spawns
        ``ffmpeg -version`` with a timeout) on every refresh rather than
        caching the result. Refreshes happen only on startup and after
        a preferences change, so the cost is negligible and the value
        always matches the currently-configured binary.
        """
        if self._locator is None:
            self._ffmpeg_status_label.setText(
                "<span style='color: #c0392b;'>&#x25CF;</span> FFmpeg not connected"
            )
            self._ffmpeg_status_label.setToolTip(
                "No FFmpeg locator is available. Use Preferences → FFmpeg path "
                "to point the application at a working installation."
            )
            return

        version = self._locator.version()
        # The ``ffmpeg -version`` banner is long (includes a build
        # timestamp, compiler, and configuration flags). For the chip we
        # pull out just "ffmpeg version X.Y.Z" so it fits on one line of
        # the status bar regardless of screen width.
        short_version = _shorten_version(version)
        self._ffmpeg_status_label.setText(
            f"<span style='color: #27ae60;'>&#x25CF;</span> "
            f"{_html_escape(short_version)}"
        )
        self._ffmpeg_status_label.setToolTip(
            f"ffmpeg: {self._locator.ffmpeg}\n"
            f"ffprobe: {self._locator.ffprobe}\n\n"
            f"Full banner:\n{version}"
        )


def _shorten_version(banner: str) -> str:
    """Trim an FFmpeg ``-version`` banner down to its first identifying phrase.

    Typical banner:
        ``ffmpeg version 6.1.1 Copyright (c) 2000-2023 the FFmpeg developers``

    We keep everything up to (but not including) the word ``Copyright``,
    because after that the text is legal boilerplate rather than version
    information. Falling back to the raw banner keeps the function safe
    against unusual builds that format the banner differently.
    """
    if not banner:
        return "FFmpeg (version unknown)"
    head = banner.split("Copyright", 1)[0].strip()
    return head or banner


def _html_escape(text: str) -> str:
    """Minimal HTML escaping for QMessageBox.about rich text.

    QMessageBox.about treats its text as rich text whenever it contains
    HTML-looking tokens, so an FFmpeg version string that includes e.g.
    angle brackets would otherwise render incorrectly.
    """
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )