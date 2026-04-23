import sys
import os
import json
import subprocess
from pathlib import Path

from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QPushButton, QLabel, QFileDialog, 
                             QSlider, QTabWidget, QListWidget, QMessageBox,
                             QComboBox, QTimeEdit, QGroupBox, QProgressBar,
                             QFormLayout, QStackedLayout, QSpacerItem, QSizePolicy,
                             QScrollArea)
from PyQt6.QtCore import Qt, QUrl, QThread, pyqtSignal, QTime, QRect, QRectF, QPointF, QSettings
from PyQt6.QtGui import QAction, QDragEnterEvent, QDropEvent, QPainter, QColor, QPen, QPixmap, QCursor, QImage, QTransform
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput, QVideoSink, QVideoFrame


class FFmpegWorker(QThread):
    progress_updated = pyqtSignal(int)
    log_updated = pyqtSignal(str)
    finished = pyqtSignal(bool, str)

    def __init__(self, command, duration_ms=None):
        super().__init__()
        self.command = command
        self.duration_ms = duration_ms
        self.process = None
        self._is_cancelled = False

    def run(self):
        try:
            self.log_updated.emit(f"Executing: {' '.join(self.command)}\n")
            self.process = subprocess.Popen(
                self.command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )

            for line in self.process.stdout:
                if self._is_cancelled:
                    break
                self.log_updated.emit(line.strip())
                if "time=" in line and self.duration_ms:
                    try:
                        time_str = line.split("time=")[1].split(" ")[0]
                        h, m, s = time_str.split(":")
                        s, ms_part = s.split(".")
                        current_ms = (int(h) * 3600000) + (int(m) * 60000) + (int(s) * 1000) + int(ms_part) * 10
                        progress = int((current_ms / self.duration_ms) * 100)
                        self.progress_updated.emit(min(progress, 100))
                    except Exception:
                        pass 

            self.process.wait()
            
            if self._is_cancelled:
                self.finished.emit(False, "Process cancelled by user.")
            elif self.process.returncode == 0:
                self.progress_updated.emit(100)
                self.finished.emit(True, "Export completed successfully!")
            else:
                self.finished.emit(False, f"FFmpeg error. Return code: {self.process.returncode}")
                
        except Exception as e:
            self.finished.emit(False, str(e))

    def cancel(self):
        self._is_cancelled = True
        if self.process:
            self.process.terminate()


class TransformVideoWidget(QWidget):
    """A custom video widget that natively supports real-time rotation and flipping."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.video_sink = QVideoSink()
        self.video_sink.videoFrameChanged.connect(self.update_frame)
        self.current_image = QImage()
        self.rotation = 0
        self.flip_h = False
        self.flip_v = False
        self.setStyleSheet("background-color: #000000; border-radius: 8px;")

    def update_frame(self, frame):
        if frame.isValid():
            self.current_image = frame.toImage()
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#000000"))
        
        if self.current_image.isNull():
            return

        image = self.current_image
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        img_w, img_h = image.width(), image.height()
        
        # Calculate bounding box for the transformed image
        if self.rotation in [90, 270]:
            draw_w_bound, draw_h_bound = img_h, img_w
        else:
            draw_w_bound, draw_h_bound = img_w, img_h

        if draw_w_bound == 0 or draw_h_bound == 0: return

        # Calculate perfect fit scaling
        scale_w = self.width() / draw_w_bound
        scale_h = self.height() / draw_h_bound
        scale = min(scale_w, scale_h)

        draw_w = draw_w_bound * scale
        draw_h = draw_h_bound * scale

        # Center in the widget
        x = (self.width() - draw_w) / 2.0
        y = (self.height() - draw_h) / 2.0

        painter.translate(x + draw_w / 2.0, y + draw_h / 2.0)
        
        # Apply flip and rotation (Order matters: Flip -> Rotate)
        scale_x = -1.0 if self.flip_h else 1.0
        scale_y = -1.0 if self.flip_v else 1.0
        painter.scale(scale_x, scale_y)
        painter.rotate(self.rotation)

        # Draw scaled
        painter.drawImage(QRectF(-img_w * scale / 2.0, -img_h * scale / 2.0, 
                                 img_w * scale, img_h * scale), image)


class StaticCropEditor(QWidget):
    """A highly reliable crop editor that works on a static image snapshot."""
    def __init__(self):
        super().__init__()
        self.pixmap = None
        self.image_rect = QRect()
        self.norm_rect = QRectF(0.1, 0.1, 0.8, 0.8) # Normalized coords (0.0 to 1.0)
        
        self.active_handle = None
        self.drag_offset = QPointF()
        self.handle_size = 12
        self.aspect_ratio = None
        
        self.setMouseTracking(True)
        self.setStyleSheet("background-color: #000000; border-radius: 8px;")

    def set_image(self, pixmap, aspect_ratio=None):
        self.pixmap = pixmap
        self.aspect_ratio = aspect_ratio
        
        # Default box in the center
        w, h = 0.8, 0.8
        if aspect_ratio:
            if aspect_ratio > 1: h = w / aspect_ratio
            else: w = h * aspect_ratio
        self.norm_rect = QRectF((1.0 - w) / 2, (1.0 - h) / 2, w, h)
        self.update()

    def get_drawn_rect(self):
        if self.image_rect.isEmpty(): return QRect()
        x = self.image_rect.x() + self.norm_rect.x() * self.image_rect.width()
        y = self.image_rect.y() + self.norm_rect.y() * self.image_rect.height()
        w = self.norm_rect.width() * self.image_rect.width()
        h = self.norm_rect.height() * self.image_rect.height()
        return QRect(int(x), int(y), int(w), int(h))

    def get_handles(self):
        r = self.get_drawn_rect()
        if r.isEmpty(): return {}
        s = self.handle_size
        return {
            'tl': QRect(r.left() - s//2, r.top() - s//2, s, s),
            'tr': QRect(r.right() - s//2, r.top() - s//2, s, s),
            'bl': QRect(r.left() - s//2, r.bottom() - s//2, s, s),
            'br': QRect(r.right() - s//2, r.bottom() - s//2, s, s),
            't': QRect(r.center().x() - s//2, r.top() - s//2, s, s),
            'b': QRect(r.center().x() - s//2, r.bottom() - s//2, s, s),
            'l': QRect(r.left() - s//2, r.center().y() - s//2, s, s),
            'r': QRect(r.right() - s//2, r.center().y() - s//2, s, s),
            'center': r.adjusted(s, s, -s, -s) # Area to drag the whole box
        }

    def paintEvent(self, event):
        if not self.pixmap: return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # 1. Draw Image
        scaled = self.pixmap.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        x = (self.width() - scaled.width()) // 2
        y = (self.height() - scaled.height()) // 2
        self.image_rect = QRect(x, y, scaled.width(), scaled.height())
        painter.drawPixmap(x, y, scaled)

        r = self.get_drawn_rect()
        if r.isEmpty(): return

        # 2. Draw Dark Mask
        dark = QColor(0, 0, 0, 160)
        painter.fillRect(QRect(x, y, self.image_rect.width(), r.top() - y), dark)
        painter.fillRect(QRect(x, r.bottom() + 1, self.image_rect.width(), self.image_rect.bottom() - r.bottom()), dark)
        painter.fillRect(QRect(x, r.top(), r.left() - x, r.height()), dark)
        painter.fillRect(QRect(r.right() + 1, r.top(), self.image_rect.right() - r.right(), r.height()), dark)

        # 3. Draw Box
        painter.setPen(QPen(QColor(0, 255, 0), 2, Qt.PenStyle.DashLine))
        painter.drawRect(r)

        # 4. Draw Handles
        painter.setBrush(QColor(255, 255, 255))
        painter.setPen(QPen(QColor(0, 0, 0), 1))
        for name, rect in self.get_handles().items():
            if name == 'center': continue
            if self.aspect_ratio and name in ['t', 'b', 'l', 'r']: continue # Hide edge handles if ratio locked
            painter.drawRect(rect)

    def mousePressEvent(self, event):
        pos = event.pos()
        self.active_handle = None
        for name, rect in self.get_handles().items():
            if self.aspect_ratio and name in ['t', 'b', 'l', 'r']: continue
            if rect.contains(pos):
                self.active_handle = name
                # Store offset for smooth dragging of the whole box
                if name == 'center':
                    nx = (pos.x() - self.image_rect.x()) / self.image_rect.width()
                    ny = (pos.y() - self.image_rect.y()) / self.image_rect.height()
                    self.drag_offset = QPointF(nx - self.norm_rect.x(), ny - self.norm_rect.y())
                break

    def mouseMoveEvent(self, event):
        pos = event.pos()
        
        # Set Cursors
        cursor = Qt.CursorShape.ArrowCursor
        for name, rect in self.get_handles().items():
            if self.aspect_ratio and name in ['t', 'b', 'l', 'r']: continue
            if rect.contains(pos):
                if name in ['tl', 'br']: cursor = Qt.CursorShape.SizeFDiagCursor
                elif name in ['tr', 'bl']: cursor = Qt.CursorShape.SizeBDiagCursor
                elif name in ['t', 'b']: cursor = Qt.CursorShape.SizeVerCursor
                elif name in ['l', 'r']: cursor = Qt.CursorShape.SizeHorCursor
                elif name == 'center': cursor = Qt.CursorShape.SizeAllCursor
        self.setCursor(QCursor(cursor))

        if self.active_handle and event.buttons() & Qt.MouseButton.LeftButton:
            nx = (pos.x() - self.image_rect.x()) / self.image_rect.width()
            ny = (pos.y() - self.image_rect.y()) / self.image_rect.height()
            nx = max(0.0, min(1.0, nx))
            ny = max(0.0, min(1.0, ny))

            r = QRectF(self.norm_rect)

            if self.active_handle == 'center':
                new_x = nx - self.drag_offset.x()
                new_y = ny - self.drag_offset.y()
                new_x = max(0.0, min(1.0 - r.width(), new_x))
                new_y = max(0.0, min(1.0 - r.height(), new_y))
                r.moveTopLeft(QPointF(new_x, new_y))
            else:
                if self.active_handle in ['tl', 'l', 'bl']: r.setLeft(nx)
                if self.active_handle in ['tr', 'r', 'br']: r.setRight(nx)
                if self.active_handle in ['tl', 't', 'tr']: r.setTop(ny)
                if self.active_handle in ['bl', 'b', 'br']: r.setBottom(ny)

                # Enforce Minimum Size
                if r.width() < 0.05: r.setWidth(0.05)
                if r.height() < 0.05: r.setHeight(0.05)

                # Enforce Aspect Ratio
                if self.aspect_ratio:
                    img_ratio = self.image_rect.width() / self.image_rect.height()
                    target_norm_ratio = self.aspect_ratio / img_ratio
                    
                    new_h = r.width() / target_norm_ratio
                    if self.active_handle in ['tl', 'tr']: r.setTop(r.bottom() - new_h)
                    else: r.setBottom(r.top() + new_h)

            self.norm_rect = r
            self.update()

    def mouseReleaseEvent(self, event):
        self.active_handle = None


class FFmpegGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("FFmpeg GUI Wrapper")
        # Reduced default minimum size safely to prevent cutoff on smaller screens
        self.setMinimumSize(1000, 650)
        self.setAcceptDrops(True)

        self.settings = QSettings("FFmpegGUI", "Preferences")
        self.ffmpeg_path = self.settings.value("ffmpeg_path", "ffmpeg")
        self.ffprobe_path = self.settings.value("ffprobe_path", "ffprobe")

        self.current_video_path = None
        self.video_metadata = {}
        self.current_crop_pixmap_size = None
        self.worker = None

        self.init_ui()
        self.check_ffmpeg()

    def get_ffmpeg_version(self, path):
        try:
            res = subprocess.run([path, '-version'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            if res.returncode == 0: return res.stdout.split('\n')[0].split()[2]
        except: pass
        return None

    def check_ffmpeg(self):
        from shutil import which
        ffmpeg_found = which(self.ffmpeg_path) or os.path.exists(self.ffmpeg_path)
        ffprobe_found = which(self.ffprobe_path) or os.path.exists(self.ffprobe_path)
        
        if ffmpeg_found and ffprobe_found:
            version = self.get_ffmpeg_version(self.ffmpeg_path)
            if version:
                self.ffmpeg_status_label.setText(f"✅ FFmpeg Connected (v{version})")
                self.ffmpeg_status_label.setStyleSheet("color: #40a02b; padding: 0px 10px;")
            else:
                self.ffmpeg_status_label.setText("⚠️ FFmpeg found, but version check failed.")
                self.ffmpeg_status_label.setStyleSheet("color: #df8e1d; padding: 0px 10px;")
        else:
            self.ffmpeg_status_label.setText("❌ FFmpeg/FFprobe NOT found. Set path in Preferences.")
            self.ffmpeg_status_label.setStyleSheet("color: #d20f39; font-weight: bold; padding: 0px 10px;")

    def init_ui(self):
        menubar = self.menuBar()
        file_menu = menubar.addMenu("File")
        open_action = QAction("Open Video...", self)
        open_action.triggered.connect(self.open_file_dialog)
        file_menu.addAction(open_action)

        pref_menu = menubar.addMenu("Preferences")
        ffmpeg_path_action = QAction("Set FFmpeg 'bin' Folder...", self)
        ffmpeg_path_action.triggered.connect(self.set_ffmpeg_path)
        pref_menu.addAction(ffmpeg_path_action)

        help_menu = menubar.addMenu("Help")
        about_action = QAction("About...", self)
        about_action.triggered.connect(self.show_about_dialog)
        help_menu.addAction(about_action)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        main_layout = QHBoxLayout(central_widget)
        main_layout.setContentsMargins(15, 15, 15, 15)
        main_layout.setSpacing(20)

        # ==========================================
        # LEFT PANEL: Vast Video Canvas
        # ==========================================
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(15)
        
        self.player_container = QWidget()
        self.player_container.setObjectName("playerContainer")
        self.player_stacked_layout = QStackedLayout(self.player_container)
        
        self.video_widget = TransformVideoWidget()
        self.video_widget.setMinimumSize(400, 225)
        
        self.static_crop_editor = StaticCropEditor()
        
        self.player_stacked_layout.addWidget(self.video_widget)
        self.player_stacked_layout.addWidget(self.static_crop_editor)
        
        self.audio_output = QAudioOutput()
        self.player = QMediaPlayer()
        self.player.setAudioOutput(self.audio_output)
        self.player.setVideoOutput(self.video_widget.video_sink)
        self.player.positionChanged.connect(self.update_slider)
        self.player.durationChanged.connect(self.update_duration)
        self.player.playbackStateChanged.connect(self.update_play_pause_button)

        left_layout.addWidget(self.player_container, stretch=1)

        # Player Controls
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.sliderMoved.connect(self.set_position)
        left_layout.addWidget(self.slider)

        controls_layout = QHBoxLayout()
        
        self.btn_play_pause = QPushButton("▶ Play")
        self.btn_stop = QPushButton("⏹ Stop")
        self.btn_step_back = QPushButton("⏮ Frame")
        self.btn_step_fwd = QPushButton("Frame ⏭")
        
        # Apply modern specific styles to playback controls via objectName
        self.btn_play_pause.setObjectName("controlBtn")
        self.btn_stop.setObjectName("controlBtn")
        self.btn_step_back.setObjectName("controlBtn")
        self.btn_step_fwd.setObjectName("controlBtn")
        
        self.btn_play_pause.clicked.connect(self.toggle_play_pause)
        self.btn_stop.clicked.connect(self.player.stop)
        self.btn_step_back.clicked.connect(lambda: self.step_frame(-1))
        self.btn_step_fwd.clicked.connect(lambda: self.step_frame(1))

        controls_layout.addWidget(self.btn_play_pause)
        controls_layout.addWidget(self.btn_stop)
        controls_layout.addWidget(self.btn_step_back)
        controls_layout.addWidget(self.btn_step_fwd)
        
        self.lbl_time = QLabel("00:00:00.000 / 00:00:00.000")
        controls_layout.addStretch()
        controls_layout.addWidget(self.lbl_time)
        
        left_layout.addLayout(controls_layout)

        main_layout.addWidget(left_panel, stretch=7) # Takes up 70% of space

        # ==========================================
        # RIGHT PANEL: Sidebar Tools
        # ==========================================
        right_panel = QWidget()
        right_panel.setMinimumWidth(380)
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(15)

        self.meta_label = QLabel("<b>Metadata</b><br>No video loaded.<br>Drag and drop a file here.")
        self.meta_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.meta_label.setWordWrap(True)
        self.meta_label.setObjectName("metaLabel")
        right_layout.addWidget(self.meta_label)

        self.tabs = QTabWidget()
        right_layout.addWidget(self.tabs, stretch=1)

        self.init_edit_tab()
        self.init_join_tab()
        
        main_layout.addWidget(right_panel, stretch=3) # Takes up 30% of space

        # Status Bar
        self.ffmpeg_status_label = QLabel("Checking FFmpeg...")
        self.statusBar().addWidget(self.ffmpeg_status_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        self.statusBar().addPermanentWidget(self.progress_bar, 1)

    def init_edit_tab(self):
        edit_tab = QWidget()
        main_edit_layout = QVBoxLayout(edit_tab)
        main_edit_layout.setContentsMargins(0, 0, 0, 0)
        
        # Wrapped the Edit tools inside a ScrollArea to prevent crushing on small screens
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setStyleSheet("QScrollArea { border: none; background-color: transparent; } QWidget#scrollContent { background-color: transparent; }")
        
        scroll_content = QWidget()
        scroll_content.setObjectName("scrollContent")
        layout = QVBoxLayout(scroll_content)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(15)
        
        # Trim Group
        trim_group = QGroupBox("Cut / Trim")
        trim_layout = QFormLayout()
        self.time_start = QTimeEdit()
        self.time_start.setDisplayFormat("HH:mm:ss.zzz")
        btn_set_start = QPushButton("Set from Player")
        btn_set_start.clicked.connect(lambda: self.time_start.setTime(QTime.fromMSecsSinceStartOfDay(self.player.position())))
        self.time_end = QTimeEdit()
        self.time_end.setDisplayFormat("HH:mm:ss.zzz")
        btn_set_end = QPushButton("Set from Player")
        btn_set_end.clicked.connect(lambda: self.time_end.setTime(QTime.fromMSecsSinceStartOfDay(self.player.position())))
        
        trim_layout.addRow("Start:", self.time_start)
        trim_layout.addRow("", btn_set_start)
        trim_layout.addRow("End:", self.time_end)
        trim_layout.addRow("", btn_set_end)
        trim_group.setLayout(trim_layout)
        layout.addWidget(trim_group)

        # Crop Group
        crop_group = QGroupBox("Crop")
        crop_layout = QVBoxLayout()
        self.cb_crop_ratio = QComboBox()
        self.cb_crop_ratio.addItems(["Free", "1:1", "16:9", "4:3", "9:16"])
        self.btn_toggle_crop = QPushButton("Enable Visual Crop")
        self.btn_toggle_crop.setCheckable(True)
        self.btn_toggle_crop.toggled.connect(self.toggle_crop_mode)
        self.lbl_crop_vals = QLabel("Crop: None")
        self.btn_reset_crop = QPushButton("Reset Crop")
        self.btn_reset_crop.clicked.connect(self.reset_crop)
        self.actual_crop_vals = None
        
        crop_layout.addWidget(QLabel("Aspect Ratio:"))
        crop_layout.addWidget(self.cb_crop_ratio)
        crop_layout.addWidget(self.btn_toggle_crop)
        crop_layout.addWidget(self.lbl_crop_vals)
        crop_layout.addWidget(self.btn_reset_crop)
        crop_group.setLayout(crop_layout)
        layout.addWidget(crop_group)

        # Rotate & Flip Group
        rf_group = QGroupBox("Rotate & Flip")
        rf_layout = QVBoxLayout()
        self.cb_rotate = QComboBox()
        self.cb_rotate.addItems(["No Rotation", "90° Clockwise", "90° Counter-Clockwise", "180°"])
        self.cb_rotate.currentIndexChanged.connect(self.update_preview_transforms)
        self.cb_flip = QComboBox()
        self.cb_flip.addItems(["No Flip", "Horizontal Flip", "Vertical Flip", "Both"])
        self.cb_flip.currentIndexChanged.connect(self.update_preview_transforms)
        
        rf_layout.addWidget(QLabel("Rotate:"))
        rf_layout.addWidget(self.cb_rotate)
        rf_layout.addWidget(QLabel("Flip:"))
        rf_layout.addWidget(self.cb_flip)
        rf_group.setLayout(rf_layout)
        layout.addWidget(rf_group)

        layout.addSpacerItem(QSpacerItem(20, 20, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding))

        self.btn_export_edit = QPushButton("Export Edited MP4")
        self.btn_export_edit.setObjectName("exportBtn")
        self.btn_export_edit.setMinimumHeight(45)
        self.btn_export_edit.clicked.connect(self.export_edited_video)
        layout.addWidget(self.btn_export_edit)

        scroll_area.setWidget(scroll_content)
        main_edit_layout.addWidget(scroll_area)

        self.tabs.addTab(edit_tab, "Edit")

    def update_play_pause_button(self, state):
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self.btn_play_pause.setText("⏸ Pause")
        else:
            self.btn_play_pause.setText("▶ Play")

    def toggle_play_pause(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def update_preview_transforms(self):
        rot_idx = self.cb_rotate.currentIndex()
        if rot_idx == 0: self.video_widget.rotation = 0
        elif rot_idx == 1: self.video_widget.rotation = 90
        elif rot_idx == 2: self.video_widget.rotation = 270
        elif rot_idx == 3: self.video_widget.rotation = 180

        flip_idx = self.cb_flip.currentIndex()
        self.video_widget.flip_h = flip_idx in [1, 3]
        self.video_widget.flip_v = flip_idx in [2, 3]
        
        self.video_widget.update()
        self.reset_crop()

    def init_join_tab(self):
        join_tab = QWidget()
        layout = QVBoxLayout(join_tab)

        self.list_join = QListWidget()
        layout.addWidget(self.list_join)

        btn_layout = QVBoxLayout()
        btn_add = QPushButton("➕ Add Videos")
        btn_add.clicked.connect(self.add_join_videos)
        btn_remove = QPushButton("➖ Remove Selected")
        btn_remove.clicked.connect(lambda: self.list_join.takeItem(self.list_join.currentRow()))
        
        move_layout = QHBoxLayout()
        btn_up = QPushButton("▲ Move Up")
        btn_up.clicked.connect(self.move_item_up)
        btn_down = QPushButton("▼ Move Down")
        btn_down.clicked.connect(self.move_item_down)
        move_layout.addWidget(btn_up)
        move_layout.addWidget(btn_down)

        btn_layout.addWidget(btn_add)
        btn_layout.addWidget(btn_remove)
        btn_layout.addLayout(move_layout)
        layout.addLayout(btn_layout)
        
        layout.addSpacerItem(QSpacerItem(20, 20, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding))

        self.btn_export_join = QPushButton("Join & Export to MP4")
        self.btn_export_join.setObjectName("exportBtn")
        self.btn_export_join.setMinimumHeight(45)
        self.btn_export_join.clicked.connect(self.export_joined_video)
        layout.addWidget(self.btn_export_join)

        self.tabs.addTab(join_tab, "Join")

    def show_about_dialog(self):
        about_text = (
            "<h2>FFmpeg GUI Wrapper</h2>"
            "<p><b>Version:</b> 1.0.0</p>"
            "<p>A lightweight, portable video editing application built with Python and PyQt6. "
            "It acts as a wrapper around FFmpeg, allowing users to crop, trim, rotate, flip, and join videos.</p>"
            "<h3>System Requirements:</h3>"
            "<ul>"
            "<li><b>OS:</b> Windows, macOS, or Linux</li>"
            "<li><b>Dependencies:</b> Python 3.8+ (if running from source), FFmpeg, and FFprobe.</li>"
            "</ul>"
            "<hr>"
            "<p><i>Note: The application requires FFmpeg to process media files. All final exports are saved as MP4.</i></p>"
        )
        QMessageBox.about(self, "About FFmpeg GUI", about_text)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls(): event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        urls = event.mimeData().urls()
        if urls:
            if self.tabs.currentIndex() == 0: self.load_video(urls[0].toLocalFile())
            else:
                for url in urls: self.list_join.addItem(url.toLocalFile())

    def set_ffmpeg_path(self):
        dir_path = QFileDialog.getExistingDirectory(self, "Select FFmpeg 'bin' Folder")
        if dir_path:
            ffmpeg_name = "ffmpeg.exe" if os.name == 'nt' else "ffmpeg"
            ffprobe_name = "ffprobe.exe" if os.name == 'nt' else "ffprobe"
            
            ffmpeg_path = os.path.join(dir_path, ffmpeg_name)
            ffprobe_path = os.path.join(dir_path, ffprobe_name)
            
            if os.path.exists(ffmpeg_path):
                self.ffmpeg_path = ffmpeg_path
                self.settings.setValue("ffmpeg_path", ffmpeg_path)
                
                if os.path.exists(ffprobe_path):
                    self.ffprobe_path = ffprobe_path
                    self.settings.setValue("ffprobe_path", ffprobe_path)
                else:
                    self.ffprobe_path = "ffprobe"
                    self.settings.setValue("ffprobe_path", "ffprobe")
                    
                QMessageBox.information(self, "Preferences", "FFmpeg bin folder linked successfully!")
                self.check_ffmpeg()
            else:
                QMessageBox.critical(self, "Error", f"Could not find '{ffmpeg_name}' in the selected folder.")

    def open_file_dialog(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Open Video", "", "Video Files (*.mp4 *.mkv *.avi *.mov *.flv *.wmv)")
        if file_path: self.load_video(file_path)

    def load_video(self, file_path):
        self.current_video_path = file_path
        self.player.setSource(QUrl.fromLocalFile(file_path))
        self.player.pause()
        self.fetch_metadata(file_path)
        self.reset_crop()

    def fetch_metadata(self, file_path):
        try:
            cmd = [self.ffprobe_path, '-v', 'quiet', '-print_format', 'json', '-show_format', '-show_streams', file_path]
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            data = json.loads(result.stdout)
            
            video_stream = next((s for s in data['streams'] if s['codec_type'] == 'video'), None)
            if video_stream:
                w = int(video_stream.get('width', 0))
                h = int(video_stream.get('height', 0))
                duration = float(data.get('format', {}).get('duration', 0))
                
                fps_str = video_stream.get('r_frame_rate', '0/1')
                num, den = map(int, fps_str.split('/'))
                fps = num / den if den != 0 else 0

                self.video_metadata = {'width': w, 'height': h, 'duration': duration, 'fps': fps}
                meta_text = f"<b>Filename:</b> {os.path.basename(file_path)}<br><br><b>Resolution:</b> {w}x{h}<br><b>Duration:</b> {duration:.2f} s<br><b>FPS:</b> {fps:.2f}"
                self.meta_label.setText(meta_text)

                self.time_start.setTime(QTime(0, 0, 0, 0))
                self.time_end.setTime(QTime.fromMSecsSinceStartOfDay(int(duration * 1000)))
        except Exception as e:
            self.meta_label.setText(f"Error reading metadata:<br>{str(e)}")

    def update_slider(self, position):
        self.slider.setValue(position)
        self.update_time_label()

    def update_duration(self, duration):
        self.slider.setRange(0, duration)
        self.update_time_label()

    def set_position(self, position):
        self.player.setPosition(position)

    def update_time_label(self):
        curr = QTime.fromMSecsSinceStartOfDay(self.player.position()).toString("HH:mm:ss.zzz")
        tot = QTime.fromMSecsSinceStartOfDay(self.player.duration()).toString("HH:mm:ss.zzz")
        self.lbl_time.setText(f"{curr} / {tot}")

    def step_frame(self, direction):
        if not self.video_metadata.get('fps'): return
        ms_per_frame = int(1000 / self.video_metadata['fps'])
        self.player.setPosition(self.player.position() + (direction * ms_per_frame))

    # --- Frame Extraction for Crop ---
    def extract_current_frame(self):
        if not self.current_video_path: return None
        self.statusBar().showMessage("Capturing frame for crop editor...", 3000)
        
        temp_dir = os.path.join(str(Path.home()), ".ffmpeg_gui_temp")
        os.makedirs(temp_dir, exist_ok=True)
        temp_file = os.path.join(temp_dir, "snap.jpg")
        
        pos_sec = self.player.position() / 1000.0
        cmd = [self.ffmpeg_path, '-y', '-ss', f"{pos_sec:.3f}", '-i', self.current_video_path, '-vframes', '1', '-q:v', '2', temp_file]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        
        if os.path.exists(temp_file):
            pix = QPixmap(temp_file)
            try: os.remove(temp_file)
            except: pass
            
            transform = QTransform()
            rot_idx = self.cb_rotate.currentIndex()
            if rot_idx == 1: transform.rotate(90)
            elif rot_idx == 2: transform.rotate(270)
            elif rot_idx == 3: transform.rotate(180)
            
            if rot_idx > 0:
                pix = pix.transformed(transform, Qt.TransformationMode.SmoothTransformation)
                
            flip_idx = self.cb_flip.currentIndex()
            flip_h = flip_idx in [1, 3]
            flip_v = flip_idx in [2, 3]
            if flip_h or flip_v:
                pix = pix.transformed(QTransform().scale(-1.0 if flip_h else 1.0, -1.0 if flip_v else 1.0))
                
            return pix
        return None

    # --- Crop Controls ---
    def toggle_crop_mode(self, active):
        self.cb_rotate.setEnabled(not active)
        self.cb_flip.setEnabled(not active)
        
        if active:
            if not self.current_video_path:
                self.btn_toggle_crop.setChecked(False)
                return QMessageBox.warning(self, "Error", "No video loaded.")

            self.player.pause()
            pixmap = self.extract_current_frame()
            if not pixmap:
                self.btn_toggle_crop.setChecked(False)
                return QMessageBox.warning(self, "Error", "Could not extract frame for cropping.")

            self.current_crop_pixmap_size = pixmap.size()
            ratio_str = self.cb_crop_ratio.currentText()
            aspect_ratio = None
            if ratio_str != "Free":
                num, den = map(float, ratio_str.split(':'))
                aspect_ratio = num / den

            self.static_crop_editor.set_image(pixmap, aspect_ratio)
            self.player_stacked_layout.setCurrentIndex(1)
            self.btn_toggle_crop.setText("Finish Cropping")
            self.slider.setEnabled(False)
            self.btn_play_pause.setEnabled(False)
        else:
            self.player_stacked_layout.setCurrentIndex(0)
            self.btn_toggle_crop.setText("Enable Visual Crop")
            self.slider.setEnabled(True)
            self.btn_play_pause.setEnabled(True)

            if hasattr(self, 'current_crop_pixmap_size') and self.current_crop_pixmap_size:
                nr = self.static_crop_editor.norm_rect
                v_w = self.current_crop_pixmap_size.width()
                v_h = self.current_crop_pixmap_size.height()
                
                orig_x = int(nr.x() * v_w)
                orig_y = int(nr.y() * v_h)
                orig_w = int(nr.width() * v_w)
                orig_h = int(nr.height() * v_h)
                
                self.actual_crop_vals = (orig_w, orig_h, orig_x, orig_y)
                self.lbl_crop_vals.setText(f"Crop: {orig_w}x{orig_h} at ({orig_x},{orig_y})")

    def reset_crop(self):
        self.actual_crop_vals = None
        self.current_crop_pixmap_size = None
        self.lbl_crop_vals.setText("Crop: None")
        self.btn_toggle_crop.setChecked(False)
        if self.player_stacked_layout.currentIndex() == 1:
            self.player_stacked_layout.setCurrentIndex(0)

    # --- Join & Export Controls ---
    def add_join_videos(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Add Videos", "", "Video Files (*.mp4 *.mkv *.avi *.mov *.flv *.wmv)")
        for file in files: self.list_join.addItem(file)

    def move_item_up(self):
        row = self.list_join.currentRow()
        if row > 0: self.list_join.insertItem(row - 1, self.list_join.takeItem(row)); self.list_join.setCurrentRow(row - 1)

    def move_item_down(self):
        row = self.list_join.currentRow()
        if row < self.list_join.count() - 1: self.list_join.insertItem(row + 1, self.list_join.takeItem(row)); self.list_join.setCurrentRow(row + 1)

    def export_edited_video(self):
        if not self.current_video_path: return QMessageBox.warning(self, "Error", "No video loaded.")
        
        start_ms = self.time_start.time().msecsSinceStartOfDay()
        end_ms = self.time_end.time().msecsSinceStartOfDay()
        if start_ms >= end_ms: return QMessageBox.warning(self, "Error", "End time must be after the start time.")
        
        p = Path(self.current_video_path)
        default_out_path = str(p.with_name(f"{p.stem} edited.mp4"))
        out_path, _ = QFileDialog.getSaveFileName(self, "Save Video", default_out_path, "MP4 Video (*.mp4)")
        if not out_path: return

        filters = []
        rot = self.cb_rotate.currentIndex()
        if rot == 1: filters.append("transpose=1")
        elif rot == 2: filters.append("transpose=2")
        elif rot == 3: filters.append("transpose=2,transpose=2")

        flip = self.cb_flip.currentIndex()
        if flip == 1: filters.append("hflip")
        elif flip == 2: filters.append("vflip")
        elif flip == 3: filters.append("hflip,vflip")

        if self.actual_crop_vals:
            w, h, x, y = self.actual_crop_vals
            if w > 0 and h > 0: filters.append(f"crop={w}:{h}:{x}:{y}")

        filters.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")
        start_str = self.time_start.time().toString("HH:mm:ss.zzz")
        end_str = self.time_end.time().toString("HH:mm:ss.zzz")

        cmd = [self.ffmpeg_path, '-y', '-ss', start_str, '-to', end_str, '-i', self.current_video_path]
        if filters: cmd.extend(['-vf', ','.join(filters)])
        cmd.extend(['-c:v', 'libx264', '-preset', 'fast', '-c:a', 'aac', out_path])

        self.run_ffmpeg(cmd, end_ms - start_ms)

    def export_joined_video(self):
        count = self.list_join.count()
        if count < 2: return QMessageBox.warning(self, "Error", "Add at least two videos to join.")
        
        first_video_path = self.list_join.item(0).text()
        p = Path(first_video_path)
        default_out_path = str(p.with_name(f"{p.stem} joined.mp4"))
        
        out_path, _ = QFileDialog.getSaveFileName(self, "Save Joined Video", default_out_path, "MP4 Video (*.mp4)")
        if not out_path: return

        self.statusBar().showMessage("Analyzing videos for joining...", 3000)
        QApplication.processEvents()

        max_w, max_h, max_fps, total_duration_ms = 0, 0, 30, 0

        for i in range(count):
            vid_path = self.list_join.item(i).text()
            try:
                res = subprocess.run([self.ffprobe_path, '-v', 'quiet', '-print_format', 'json', '-show_format', '-show_streams', vid_path], stdout=subprocess.PIPE, universal_newlines=True, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                data = json.loads(res.stdout)
                total_duration_ms += int(float(data.get('format', {}).get('duration', 0)) * 1000)
                v_data = next(s for s in data['streams'] if s['codec_type'] == 'video')
                max_w, max_h = max(max_w, int(v_data['width'])), max(max_h, int(v_data['height']))
                
                fps_str = v_data.get('r_frame_rate', '30/1')
                num, den = map(int, fps_str.split('/'))
                max_fps = max(max_fps, num / den if den != 0 else 30)
            except: pass

        if max_w == 0 or max_h == 0: max_w, max_h = 1920, 1080
        w, h, fps = max_w - (max_w % 2), max_h - (max_h % 2), max_fps

        cmd = [self.ffmpeg_path, '-y']
        for i in range(count): cmd.extend(['-i', self.list_join.item(i).text()])

        filter_complex = ""
        for i in range(count): 
            filter_complex += f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p[v{i}]; "
            filter_complex += f"[{i}:a]aresample=48000,aformat=sample_rates=48000:channel_layouts=stereo[a{i}]; "
            
        concat_str = ""
        for i in range(count): concat_str += f"[v{i}][a{i}]"
        concat_str += f"concat=n={count}:v=1:a=1[outv][outa]"

        cmd.extend(['-filter_complex', filter_complex + concat_str, '-map', '[outv]', '-map', '[outa]', '-c:v', 'libx264', '-preset', 'fast', '-c:a', 'aac', out_path])
        self.run_ffmpeg(cmd, total_duration_ms if total_duration_ms > 0 else None)

    def run_ffmpeg(self, cmd, duration_ms):
        self.btn_export_edit.setEnabled(False)
        self.btn_export_join.setEnabled(False)
        self.progress_bar.setValue(0)
        self.worker = FFmpegWorker(cmd, duration_ms)
        self.worker.progress_updated.connect(self.progress_bar.setValue)
        self.worker.finished.connect(self.on_ffmpeg_finished)
        self.worker.start()

    def on_ffmpeg_finished(self, success, message):
        self.btn_export_edit.setEnabled(True)
        self.btn_export_join.setEnabled(True)
        if success: QMessageBox.information(self, "Success", "Export completed successfully!")
        else: QMessageBox.critical(self, "Error", f"Export failed:\n{message}")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    
    # Fully modernized Light Theme Stylesheet
    app.setStyleSheet("""
        QMainWindow, QWidget {
            background-color: #f4f5f7;
            color: #2e3440;
            font-family: "Segoe UI", -apple-system, BlinkMacSystemFont, Arial, sans-serif;
            font-size: 13px;
        }
        
        QGroupBox {
            border: 1px solid #ccd0da;
            border-radius: 8px;
            margin-top: 24px;
            padding-top: 15px;
            background-color: #ffffff;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            subcontrol-position: top left;
            left: 10px;
            top: 0px;
            color: #1e66f5;
            font-weight: bold;
            background-color: transparent;
            padding: 0 5px;
        }
        
        QPushButton {
            background-color: #ffffff;
            border: 1px solid #ccd0da;
            border-radius: 6px;
            padding: 8px 14px;
            font-weight: bold;
            color: #4c4f69;
        }
        QPushButton:hover { background-color: #e6e9ef; border-color: #bcc0cc; }
        QPushButton:pressed, QPushButton:checked { background-color: #1e66f5; color: #ffffff; border-color: #1e66f5; }
        
        /* Modern Media Control Buttons */
        QPushButton#controlBtn {
            background-color: transparent;
            border: none;
            font-size: 14px;
            padding: 8px 12px;
            color: #1e66f5;
            font-weight: bold;
        }
        QPushButton#controlBtn:hover { background-color: #e6e9ef; border-radius: 6px; }
        QPushButton#controlBtn:pressed { background-color: #dce0e8; }
        
        QPushButton#exportBtn {
            background-color: #40a02b;
            color: #ffffff;
            font-size: 14px;
            padding: 12px;
            border: none;
            border-radius: 8px;
        }
        QPushButton#exportBtn:hover { background-color: #317f22; }
        
        QSlider::groove:horizontal {
            border-radius: 4px;
            height: 8px;
            background: #e6e9ef;
        }
        QSlider::handle:horizontal {
            background: #1e66f5;
            width: 16px;
            height: 16px;
            margin: -4px 0;
            border-radius: 8px;
        }
        QSlider::handle:horizontal:hover {
            background: #114fda;
        }
        QSlider::sub-page:horizontal {
            background: #1e66f5;
            border-radius: 4px;
        }
        
        QTabWidget::pane { border: 1px solid #ccd0da; border-radius: 8px; background: #ffffff; }
        QTabBar::tab {
            background: #eff1f5;
            border: 1px solid #ccd0da;
            border-bottom: none;
            padding: 10px 20px;
            border-top-left-radius: 8px;
            border-top-right-radius: 8px;
            margin-right: 4px;
            font-size: 14px;
            font-weight: 600;
            color: #4c4f69;
        }
        QTabBar::tab:selected { background: #ffffff; color: #1e66f5; border-bottom: 2px solid #ffffff; }
        QTabBar::tab:hover:!selected { background: #e6e9ef; }
        
        QListWidget {
            background-color: #ffffff;
            border: 1px solid #ccd0da;
            border-radius: 6px;
            padding: 5px;
            color: #4c4f69;
        }
        
        QLabel#metaLabel {
            background-color: #ffffff;
            padding: 15px;
            border-radius: 8px;
            border: 1px solid #ccd0da;
        }
        
        QWidget#playerContainer {
            background-color: #000000;
            border: 1px solid #ccd0da;
            border-radius: 8px;
        }
        
        QComboBox, QTimeEdit {
            background-color: #ffffff;
            border: 1px solid #ccd0da;
            border-radius: 6px;
            padding: 6px 25px 6px 10px;
            color: #4c4f69;
        }
        QComboBox::drop-down { border: none; width: 20px; }
        
        /* Modernized Up/Down Buttons for QTimeEdit */
        QTimeEdit::up-button {
            subcontrol-origin: border;
            subcontrol-position: top right;
            width: 24px;
            border-left: 1px solid #ccd0da;
            border-bottom: 1px solid #ccd0da;
            border-top-right-radius: 6px;
            background-color: #eff1f5;
        }
        QTimeEdit::down-button {
            subcontrol-origin: border;
            subcontrol-position: bottom right;
            width: 24px;
            border-left: 1px solid #ccd0da;
            border-top: none;
            border-bottom-right-radius: 6px;
            background-color: #eff1f5;
        }
        QTimeEdit::up-button:hover, QTimeEdit::down-button:hover { background-color: #dce0e8; }
        QTimeEdit::up-button:pressed, QTimeEdit::down-button:pressed { background-color: #ccd0da; }
        QTimeEdit::up-arrow { width: 10px; height: 10px; }
        QTimeEdit::down-arrow { width: 10px; height: 10px; }
        
        QScrollArea { border: none; background-color: transparent; }
        QWidget#scrollContent { background-color: transparent; }

        /* Sleek Modern Scrollbars */
        QScrollBar:vertical {
            border: none;
            background-color: transparent;
            width: 8px;
            margin: 0px;
        }
        QScrollBar::handle:vertical {
            background-color: #bcc0cc;
            border-radius: 4px;
            min-height: 20px;
        }
        QScrollBar::handle:vertical:hover { background-color: #9ca0b0; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background-color: transparent; }

        QScrollBar:horizontal {
            border: none;
            background-color: transparent;
            height: 8px;
            margin: 0px;
        }
        QScrollBar::handle:horizontal {
            background-color: #bcc0cc;
            border-radius: 4px;
            min-width: 20px;
        }
        QScrollBar::handle:horizontal:hover { background-color: #9ca0b0; }
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0px; }
        QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background-color: transparent; }

        /* Export Progress Bar */
        QProgressBar {
            background-color: #e6e9ef;
            border: none;
            border-radius: 4px;
            text-align: center;
            color: transparent; 
            height: 8px;
            max-height: 8px;
        }
        QProgressBar::chunk {
            background-color: #1e66f5;
            border-radius: 4px;
        }
    """)
    window = FFmpegGUI()
    window.show()
    sys.exit(app.exec())