"""Modal export-progress dialog.

This is the visible face of a running FFmpeg job. It does three jobs:

1. Show a progress bar that advances from 0 to 100 percent based on the
   ``progress`` signal the worker emits as it parses FFmpeg's stderr.
2. Stream FFmpeg's full log output into a scrolling text area so when an
   export fails the user (or a support person) can diagnose why.
3. Offer a Cancel button that asks the worker to terminate its subprocess,
   then blocks closing until the thread has actually stopped.

Only one export should run at a time in the application. We enforce that
structurally by making this dialog modal: while it is open, the main window
does not receive events, so the user cannot start a second export that would
race for the same output file.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..core.ffmpeg_runner import FFmpegJob


class ExportDialog(QDialog):
    """Modal dialog that runs a single FFmpeg job to completion.

    Parameters
    ----------
    command:
        The already-built FFmpeg argument list. We do not build commands here
        - the caller knows whether this is an edit, a copy, or a join.
    total_duration_ms:
        Expected output duration in milliseconds. Used to convert FFmpeg's
        "time=" reports into a 0-100 percentage. For a trim the caller passes
        the trimmed duration; for a join it passes the summed input durations.
    output_path:
        Destination file. We display it in the dialog and open the parent
        directory on success if the user clicks Show-in-folder (future use).
    parent:
        Normal Qt parent, used for modal positioning.
    """

    def __init__(
        self,
        command: List[str],
        total_duration_ms: int,
        output_path: Path,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Exporting…")
        # modal=app so clicks on the main window are deferred until we close,
        # preventing accidental second exports.
        self.setModal(True)
        self.setMinimumWidth(520)

        self._output_path = output_path
        self._succeeded: bool = False
        self._final_message: str = ""

        # --- widgets ------------------------------------------------------
        self._label = QLabel(f"Writing {output_path.name}…", self)
        self._label.setWordWrap(True)

        self._progress = QProgressBar(self)
        self._progress.setRange(0, 100)
        self._progress.setValue(0)

        self._log = QTextEdit(self)
        self._log.setReadOnly(True)
        # Fixed-width font makes FFmpeg's columnar progress lines easier to
        # read and keeps the box from reflowing every time a new line lands.
        self._log.setStyleSheet("QTextEdit { font-family: monospace; font-size: 10pt; }")
        self._log.setMinimumHeight(180)

        self._buttons = QDialogButtonBox(self)
        self._cancel_button = QPushButton("Cancel", self)
        self._cancel_button.clicked.connect(self._on_cancel_clicked)
        self._buttons.addButton(self._cancel_button, QDialogButtonBox.RejectRole)

        layout = QVBoxLayout(self)
        layout.addWidget(self._label)
        layout.addWidget(self._progress)
        layout.addWidget(QLabel("FFmpeg log:", self))
        layout.addWidget(self._log, 1)
        layout.addWidget(self._buttons)

        # --- job wiring ---------------------------------------------------
        # We hold a strong reference to the job for its entire lifetime so
        # Python's garbage collector does not reap it mid-run. The job
        # itself keeps the worker + thread alive through Qt parent-child
        # relationships, so as long as the dialog holds _job the thread is
        # safe.
        self._job = FFmpegJob(command, total_duration_ms)
        self._job.worker.progress.connect(self._progress.setValue)
        self._job.worker.log.connect(self._append_log)
        self._job.worker.finished.connect(self._on_finished)
        self._job.start()

    # ------------------------------------------------------------------
    # Result accessors (the caller reads these after exec() returns)
    # ------------------------------------------------------------------
    def succeeded(self) -> bool:
        return self._succeeded

    def final_message(self) -> str:
        return self._final_message

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------
    def _append_log(self, line: str) -> None:
        """Append a log line and keep the view scrolled to the bottom.

        Using append() + ensureCursorVisible() is cheaper than setText()
        with the whole history every time a line lands, which matters when
        FFmpeg emits many lines per second on long encodes.
        """
        self._log.append(line)
        cursor = self._log.textCursor()
        cursor.movePosition(cursor.End)
        self._log.setTextCursor(cursor)

    def _on_cancel_clicked(self) -> None:
        """User clicked Cancel: ask the worker to stop.

        We do NOT close the dialog here. Closing is deferred until the
        worker's finished signal fires, because on some platforms (Windows
        in particular) closing the dialog while the subprocess is still
        being torn down can leave a zombie ffmpeg.exe running.
        """
        if self._job is None:
            return
        self._cancel_button.setEnabled(False)
        self._cancel_button.setText("Cancelling…")
        self._label.setText("Cancelling the export - waiting for FFmpeg to stop…")
        self._job.cancel()

    def _on_finished(self, success: bool, message: str) -> None:
        """Worker reported its final state. Switch the dialog into a result view."""
        self._succeeded = success
        self._final_message = message

        if success:
            self._progress.setValue(100)
            self._label.setText(f"Export complete: {self._output_path.name}")
        else:
            self._label.setText(f"Export failed: {message or 'see log for details.'}")

        # Swap the Cancel button for a Close button so the user can dismiss
        # the dialog and read the final log. Rebuilding the QDialogButtonBox
        # is simpler than juggling enabled/text on a single button.
        self._buttons.removeButton(self._cancel_button)
        self._cancel_button.deleteLater()
        close_button = QPushButton("Close", self)
        close_button.setDefault(True)
        close_button.clicked.connect(self.accept if success else self.reject)
        self._buttons.addButton(close_button, QDialogButtonBox.AcceptRole)

    # ------------------------------------------------------------------
    # Close-event interception
    # ------------------------------------------------------------------
    def closeEvent(self, event) -> None:  # noqa: N802 (Qt naming convention)
        """Block the user from closing while the job is still running.

        If the worker thread is still active we either cancel first (and
        wait) or refuse the close entirely - either way we never let the
        dialog vanish while a subprocess is orphaned behind it.
        """
        if self._job is not None and self._job.thread.isRunning():
            # Ask the user whether to cancel; if they decline, veto the close.
            answer = QMessageBox.question(
                self,
                "Cancel export?",
                "The export is still running. Cancel it and close this window?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            self._job.cancel()
            # Give the thread a moment to tear down cleanly. 3 s is enough
            # for FFmpeg to flush on any reasonable filesystem.
            self._job.thread.wait(3000)
        event.accept()
