"""
Embedded video preview and transport controls.

This widget owns the QMediaPlayer, the graphics scene that hosts the video
and crop overlay, and the transport buttons below. It exposes a small API
so the main window and the edit panels can drive playback, query the
current position, and read back the crop rectangle without poking at
internals.

The trickiest piece is keeping scene coordinates identical to source
pixel coordinates. That invariant is what lets the crop overlay produce
FFmpeg-ready values without any conversion step, and it's why we
explicitly resize the video item to the media's natural resolution every
time a new file loads.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QRectF, QSizeF, QUrl, Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
from PySide6.QtWidgets import (
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from src.core.edit_state import CropRect, EditState
from src.core.media_info import MediaInfo
from src.gui.crop_overlay import CropOverlay
from src.utils.time_utils import format_ms


class VideoPreview(QWidget):
    """Preview area + transport controls.

    Signals
    -------
    positionChanged(int):
        Current playback position in milliseconds. Fired each time the
        underlying QMediaPlayer reports a new position.
    durationChanged(int):
        Total length of the loaded media, in milliseconds.
    cropChanged(object):
        Emitted with a :class:`CropRect` (or ``None`` if the crop covers
        the full frame) whenever the user drags the overlay. The edit
        panels use this to update their spinboxes and the edit state.
    """

    positionChanged = Signal(int)
    durationChanged = Signal(int)
    cropChanged = Signal(object)  # CropRect | None

    def __init__(self, edit_state: EditState, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._edit_state = edit_state
        self._media: Optional[MediaInfo] = None

        # Build the graphics scene first so we can plug the video output
        # into it. The black background matches what professional NLEs
        # use and is easier on the eyes than the default light grey.
        self._scene = QGraphicsScene(self)
        self._scene.setBackgroundBrush(QBrush(QColor(10, 10, 10)))

        self._video_item = QGraphicsVideoItem()
        self._scene.addItem(self._video_item)

        # Crop overlay starts with a placeholder bounds; set_media will
        # resize it to match the loaded video's resolution.
        self._crop_overlay = CropOverlay(QRectF(0, 0, 100, 100))
        self._scene.addItem(self._crop_overlay)
        self._crop_overlay.signals.cropChanged.connect(self._on_overlay_crop_changed)

        # Initially hidden until a file is loaded, so we don't show an
        # empty blue rectangle over a black scene.
        self._crop_overlay.setVisible(False)

        self._view = QGraphicsView(self._scene)
        self._view.setRenderHint(self._view.renderHints())
        self._view.setFrameShape(QGraphicsView.NoFrame)
        self._view.setBackgroundBrush(QBrush(QColor(10, 10, 10)))
        # We manage fit/zoom ourselves in _apply_preview_transform, so
        # scrollbars would just add visual noise.
        self._view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        # Set the transformation anchor explicitly. AnchorViewCenter is
        # the Qt default today, but setting it here guarantees that any
        # call we make to view.rotate() or view.scale() pivots around
        # the view centre regardless of future Qt changes. This is what
        # keeps rotated and flipped content visible in the viewport
        # instead of sliding off to one side.
        self._view.setTransformationAnchor(QGraphicsView.AnchorViewCenter)

        # Media player, with a separate audio output. Qt 6 split these
        # so an app can, for example, mute one player while another keeps
        # playing audio; we don't need that today but the API expects the
        # split even when we'd rather write one line.
        self._player = QMediaPlayer(self)
        self._audio = QAudioOutput(self)
        self._player.setAudioOutput(self._audio)
        self._player.setVideoOutput(self._video_item)
        self._player.positionChanged.connect(self._on_player_position_changed)
        self._player.durationChanged.connect(self._on_player_duration_changed)
        self._player.playbackStateChanged.connect(self._on_player_state_changed)

        # --- Transport controls --------------------------------------
        # Using standard icons from the platform style (QStyle) gives us
        # reasonable defaults that match the host OS without shipping
        # custom icon files, which would bloat the distribution.
        style = self.style()

        self._btn_play = QPushButton()
        self._btn_play.setIcon(style.standardIcon(QStyle.SP_MediaPlay))
        self._btn_play.setToolTip("Play / Pause (Space)")
        self._btn_play.clicked.connect(self.toggle_play)

        self._btn_stop = QPushButton()
        self._btn_stop.setIcon(style.standardIcon(QStyle.SP_MediaStop))
        self._btn_stop.setToolTip("Stop")
        self._btn_stop.clicked.connect(self._player.stop)

        self._btn_frame_back = QPushButton()
        self._btn_frame_back.setIcon(style.standardIcon(QStyle.SP_MediaSeekBackward))
        self._btn_frame_back.setToolTip("Step one frame back (Left arrow)")
        self._btn_frame_back.clicked.connect(lambda: self.step_frames(-1))

        self._btn_frame_fwd = QPushButton()
        self._btn_frame_fwd.setIcon(style.standardIcon(QStyle.SP_MediaSeekForward))
        self._btn_frame_fwd.setToolTip("Step one frame forward (Right arrow)")
        self._btn_frame_fwd.clicked.connect(lambda: self.step_frames(1))

        # Seek slider in integer milliseconds. QSlider's int range of
        # ~2.1 billion comfortably covers any realistic video length.
        self._seek_slider = QSlider(Qt.Horizontal)
        self._seek_slider.setRange(0, 0)
        # sliderMoved fires only for user-driven changes; valueChanged
        # would also fire when we programmatically update the slider
        # during playback, which would cause a feedback loop.
        self._seek_slider.sliderMoved.connect(self._player.setPosition)

        self._time_label = QLabel("00:00:00.000 / 00:00:00.000")
        # Monospace-ish so the label width is stable as digits change.
        self._time_label.setStyleSheet("font-family: monospace;")

        # --- Layout --------------------------------------------------
        controls_row = QHBoxLayout()
        controls_row.addWidget(self._btn_play)
        controls_row.addWidget(self._btn_stop)
        controls_row.addWidget(self._btn_frame_back)
        controls_row.addWidget(self._btn_frame_fwd)
        controls_row.addWidget(self._seek_slider, 1)
        controls_row.addWidget(self._time_label)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._view, 1)
        layout.addLayout(controls_row)

        # Playback speed and audio are at nominal defaults.
        self._audio.setVolume(0.8)

        # Disable controls until a file is loaded so the user doesn't get
        # confused by pressing Play when there's nothing to play.
        self._set_controls_enabled(False)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load(self, media: MediaInfo) -> None:
        """Load a new video file into the preview.

        We stop any current playback before swapping the source, because
        QMediaPlayer can sometimes get into a bad state if ``setSource``
        is called while it's actively playing a different file.
        """
        self._player.stop()
        self._media = media

        # Set the video item's native size so scene coordinates = source
        # pixels. The GraphicsVideoItem respects this when it scales the
        # decoded frame for display.
        self._video_item.setSize(QSizeF(media.width, media.height))
        self._scene.setSceneRect(0, 0, media.width, media.height)

        # Reset the crop overlay to cover the full frame.
        self._crop_overlay.set_bounds(QRectF(0, 0, media.width, media.height))
        self._crop_overlay.setVisible(True)

        # Clear any leftover transform from a previous file.
        self._view.resetTransform()
        self._apply_preview_transform()

        self._player.setSource(QUrl.fromLocalFile(str(media.path)))
        self._set_controls_enabled(True)
        self._fit_view()

    def clear(self) -> None:
        """Unload any current video and disable the transport."""
        self._player.stop()
        self._player.setSource(QUrl())
        self._media = None
        self._crop_overlay.setVisible(False)
        self._set_controls_enabled(False)
        self._time_label.setText("00:00:00.000 / 00:00:00.000")

    def toggle_play(self) -> None:
        """Play if paused/stopped, pause if playing."""
        if self._player.playbackState() == QMediaPlayer.PlayingState:
            self._player.pause()
        else:
            self._player.play()

    def step_frames(self, frames: int) -> None:
        """Seek forward or backward by ``frames`` frames.

        ``QMediaPlayer`` has no native frame-step API on every backend, so
        we compute the millisecond delta from the media's frame rate and
        seek. It's accurate to within half a frame, which is close enough
        for the "rough positioning" role frame-stepping plays in this app
        (fine positioning uses the trim spinboxes).
        """
        if self._media is None or self._media.fps <= 0:
            return
        delta_ms = int(round(1000.0 * frames / self._media.fps))
        new_pos = max(0, min(self._player.duration(), self._player.position() + delta_ms))
        self._player.setPosition(new_pos)

    def position_ms(self) -> int:
        """Current playback position in milliseconds."""
        return int(self._player.position())

    def set_position_ms(self, ms: int) -> None:
        """Seek to the given position. Ignored when no media is loaded."""
        if self._media is None:
            return
        self._player.setPosition(max(0, min(self._player.duration(), ms)))

    # --- crop interface used by the crop panel ------------------------

    def set_crop_rect_from_source(self, rect: Optional[CropRect]) -> None:
        """Apply a crop rectangle (in source pixels) to the overlay.

        Called when the user edits the crop numerically in the side panel
        so the overlay reflects the new values.
        """
        if self._media is None:
            return
        if rect is None:
            self._crop_overlay.reset()
        else:
            self._crop_overlay.set_crop_rect(QRectF(rect.x, rect.y, rect.width, rect.height))

    def set_crop_aspect_ratio(self, ratio: Optional[float]) -> None:
        """Propagate an aspect-ratio lock from the crop panel."""
        self._crop_overlay.set_aspect_ratio(ratio)

    # --- transform interface used by the transform panel --------------

    def apply_transform(self, rotation: int, flip_h: bool, flip_v: bool) -> None:
        """Update the live preview transform for rotate/flip.

        We store nothing; the caller (the main window) updates the
        EditState, and we just paint what we're told. That keeps state
        ownership clear: the panel is the source of truth, the preview
        is a renderer.
        """
        self._edit_state.rotation = rotation
        self._edit_state.flip_horizontal = flip_h
        self._edit_state.flip_vertical = flip_v
        self._apply_preview_transform()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _set_controls_enabled(self, enabled: bool) -> None:
        for widget in (
            self._btn_play, self._btn_stop, self._btn_frame_back,
            self._btn_frame_fwd, self._seek_slider,
        ):
            widget.setEnabled(enabled)

    def _on_player_position_changed(self, position: int) -> None:
        # Avoid fighting the user when they are dragging the slider - in
        # that case ``sliderMoved`` is driving the player, and we would
        # just overwrite the value they're trying to set.
        if not self._seek_slider.isSliderDown():
            self._seek_slider.setValue(position)
        self._update_time_label()
        self.positionChanged.emit(position)

    def _on_player_duration_changed(self, duration: int) -> None:
        self._seek_slider.setRange(0, duration)
        self._update_time_label()
        self.durationChanged.emit(duration)

    def _on_player_state_changed(self, state) -> None:
        style = self.style()
        icon = (QStyle.SP_MediaPause
                if state == QMediaPlayer.PlayingState
                else QStyle.SP_MediaPlay)
        self._btn_play.setIcon(style.standardIcon(icon))

    def _update_time_label(self) -> None:
        pos = self._player.position()
        dur = self._player.duration()
        self._time_label.setText(f"{format_ms(pos)} / {format_ms(dur)}")

    def _on_overlay_crop_changed(self, rect: QRectF) -> None:
        """Translate an overlay change into a CropRect (or None for full frame).

        FFmpeg wants integer pixel coordinates, so we round first and then
        decide whether the rounded rectangle is the full frame. Using an
        exact integer comparison here is important: an earlier version of
        this code treated any rectangle within one pixel of full size as
        "no crop", which silently swallowed deliberate near-full crops
        (for example 1919x1079 on a 1920x1080 source) and meant the
        export came out uncropped even though the user could see the
        crop rectangle on the preview.
        """
        if self._media is None:
            return

        # Round to integers once and reuse - the overlay works in floats
        # for smooth dragging, but every downstream consumer (FFmpeg, the
        # crop-panel spinboxes, the EditState) uses integers.
        x = int(round(rect.x()))
        y = int(round(rect.y()))
        w = int(round(rect.width()))
        h = int(round(rect.height()))

        # Clamp for safety. The crop overlay is supposed to keep the
        # rectangle inside the video's bounds, but a float-to-int round
        # can push a corner one pixel over; clamping here means the
        # FFmpeg arguments are always valid even in those edge cases.
        x = max(0, min(x, self._media.width - 1))
        y = max(0, min(y, self._media.height - 1))
        w = max(1, min(w, self._media.width - x))
        h = max(1, min(h, self._media.height - y))

        # Only treat the rectangle as "no crop" when, after rounding, it
        # exactly covers the source frame. Anything smaller - even by one
        # pixel - is a real crop the user asked for and must be honoured.
        if x == 0 and y == 0 and w == self._media.width and h == self._media.height:
            self.cropChanged.emit(None)
            return

        self.cropChanged.emit(CropRect(x=x, y=y, width=w, height=h))

    def _apply_preview_transform(self) -> None:
        """Fit the scene to the viewport and apply rotation/flip on top.

        The previous implementation built a combined matrix with
        ``QTransform`` arithmetic and then called
        ``view.setTransform(...)`` directly. That has a subtle problem:
        ``setTransform`` replaces the matrix without going through Qt's
        transformation anchor, so a horizontal flip pivots around the
        scene origin rather than the viewport centre and the content
        ends up at negative scene coordinates - outside the visible
        viewport. The user then sees an unchanged black background and
        reasonably concludes that the flip did nothing.

        The fix is to rebuild the transform step by step using the
        convenience methods ``view.scale()`` and ``view.rotate()``.
        Those respect ``AnchorViewCenter`` (which we set explicitly in
        the constructor), so every step pivots around the viewport
        centre and the content stays visible through every rotation and
        flip. We also compute the fit scale ourselves because Qt's
        built-in ``fitInView`` is not rotation-aware: after a 90 or 270
        degree rotation the effective width and height swap, and a
        naive fit call would either under-fill the viewport or crop
        the rotated image.
        """
        if self._media is None:
            return

        scene = self._scene.sceneRect()
        viewport = self._view.viewport().size()
        if (
            scene.width() <= 0
            or scene.height() <= 0
            or viewport.width() <= 0
            or viewport.height() <= 0
        ):
            return

        # After a 90 or 270 degree rotation the effective dimensions of
        # the content swap: a 1920x1080 frame occupies a 1080x1920
        # footprint once stood on its side. Use that effective size when
        # computing the scale factor so the fit is correct in every
        # rotation state.
        rotation = self._edit_state.rotation % 360
        if rotation in (90, 270):
            effective_w = scene.height()
            effective_h = scene.width()
        else:
            effective_w = scene.width()
            effective_h = scene.height()
        scale = min(
            viewport.width() / effective_w,
            viewport.height() / effective_h,
        )

        # Rebuild the transform step by step. Each call below goes
        # through the transformation anchor we set in __init__, so the
        # content stays centred in the viewport. Order matters: scale
        # first so the final pixel size is right, then rotate, then
        # flip, so the flip is applied to the already-rotated image and
        # matches the FFmpeg filter chain (rotate then flip) exactly.
        self._view.resetTransform()
        self._view.scale(scale, scale)
        if rotation != 0:
            self._view.rotate(rotation)
        if self._edit_state.flip_horizontal:
            self._view.scale(-1, 1)
        if self._edit_state.flip_vertical:
            self._view.scale(1, -1)
        # Explicit centering is belt-and-braces - the anchor should take
        # care of it, but centerOn guarantees the scene centre lands at
        # the viewport centre even after a reset + rebuild.
        self._view.centerOn(scene.center())

    def _fit_view(self) -> None:
        """Alias retained for backward compatibility within this module.

        Every caller that used to call ``_fit_view`` now goes through
        the single implementation in ``_apply_preview_transform``. We
        keep this alias so the ``resizeEvent`` handler (and any future
        caller that still thinks of this as "just fit") reads naturally.
        """
        self._apply_preview_transform()

    def resizeEvent(self, event) -> None:
        # When the window resizes, re-fit so the video tracks the new
        # viewport dimensions instead of being cropped or underfilled.
        super().resizeEvent(event)
        self._fit_view()

    def keyPressEvent(self, event) -> None:
        # Simple keyboard shortcuts on the preview: Space toggles play,
        # Left/Right step one frame. These only work when the preview
        # actually has focus, which is the case after clicking on it.
        if event.key() == Qt.Key_Space:
            self.toggle_play()
            event.accept()
            return
        if event.key() == Qt.Key_Left:
            self.step_frames(-1)
            event.accept()
            return
        if event.key() == Qt.Key_Right:
            self.step_frames(1)
            event.accept()
            return
        super().keyPressEvent(event)
