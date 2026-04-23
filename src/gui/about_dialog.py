"""
About dialog.

A friendly, self-documenting "what is this program" window. We made this
a proper :class:`QDialog` rather than a one-line ``QMessageBox.about``
call because the user asked for multiple sections - application version,
Python and Qt versions, operating system, FFmpeg status, system
requirements - and a message box becomes unreadable once you pack that
much text into it. A structured dialog with grouped sections and a
scrollable text block is much easier to skim.

The dialog pulls every piece of information from the place where it
actually lives: the application version from :mod:`src`, the Python and
Qt banners from their respective modules, the platform fields from
:mod:`platform`, and the FFmpeg version and path from the currently-
active :class:`FFmpegLocator`. That means the dialog can never drift
out of sync with what the rest of the app has configured; a refresh is
just "close and reopen".
"""
from __future__ import annotations

import platform
import sys
from typing import Optional

from PySide6 import __version__ as _pyside_version
from PySide6.QtCore import Qt, qVersion
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .. import __version__ as _app_version
from ..utils.ffmpeg_locator import FFmpegLocator
from ..utils.settings import AppSettings


class AboutDialog(QDialog):
    """Informational modal dialog reporting app, system, and FFmpeg state.

    The dialog is intentionally read-only. Anything the user might want
    to *change* (such as the FFmpeg path) lives in the Preferences dialog,
    so About stays a pure "here is the current state" view.
    """

    def __init__(
        self,
        locator: Optional[FFmpegLocator],
        settings: AppSettings,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("About FFmpeg GUI")
        self.setModal(True)
        self.setMinimumSize(560, 560)

        # --- title block --------------------------------------------------
        # Large friendly banner at the top so the user can tell at a glance
        # which application and version they are looking at. setTextFormat
        # stays on the default RichText so our tags render as formatting
        # rather than being escaped and shown literally.
        title = QLabel(
            f"<h2 style='margin-bottom: 4px;'>FFmpeg GUI</h2>"
            f"<div>Version {_html_escape(_app_version)}</div>"
            f"<div style='color: palette(mid); margin-top: 6px;'>"
            f"A lightweight, portable desktop video editor built on FFmpeg."
            f"</div>",
            self,
        )
        title.setTextFormat(Qt.RichText)
        title.setWordWrap(True)

        # --- body content -------------------------------------------------
        # We render every section into one rich-text block rather than a
        # dozen separate widgets. This is simpler to maintain and scrolls
        # as one unit, which is what the user expects when the content
        # outgrows the initial window size.
        body = QLabel(self._build_body_html(locator, settings), self)
        body.setTextFormat(Qt.RichText)
        body.setWordWrap(True)
        body.setTextInteractionFlags(
            # Allow the user to select and copy text (useful when reporting
            # a bug) and to follow URLs if we ever add any.
            Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard | Qt.LinksAccessibleByMouse
        )
        body.setAlignment(Qt.AlignTop | Qt.AlignLeft)

        # Wrap the body in a scroll area so the dialog stays a reasonable
        # size even on small screens. Without this, a long FFmpeg path or
        # an unusually verbose platform string could push the OK button
        # off the bottom of the window.
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll_host = QWidget()
        scroll_layout = QVBoxLayout(scroll_host)
        scroll_layout.setContentsMargins(0, 0, 0, 0)
        scroll_layout.addWidget(body)
        scroll_layout.addStretch(1)
        scroll.setWidget(scroll_host)

        # --- buttons ------------------------------------------------------
        buttons = QDialogButtonBox(QDialogButtonBox.Close, parent=self)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        # QDialogButtonBox.Close emits rejected(), so also wire it explicitly
        # for belt-and-braces clarity - some platforms bind Enter differently.
        buttons.button(QDialogButtonBox.Close).clicked.connect(self.accept)

        # --- assemble -----------------------------------------------------
        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(scroll, 1)
        layout.addWidget(buttons)

    # ------------------------------------------------------------------
    # Content assembly
    # ------------------------------------------------------------------
    def _build_body_html(
        self,
        locator: Optional[FFmpegLocator],
        settings: AppSettings,
    ) -> str:
        """Produce the long rich-text block shown in the scroll area.

        Split out into its own method so the dialog's constructor stays
        concerned with layout rather than string plumbing, and so the
        sections can be unit-tested individually in the future without
        instantiating a widget.
        """
        # Collect the raw facts first, escaping each piece that might
        # contain user- or system-supplied content (paths, version banners)
        # before interpolating into HTML.
        os_line = f"{platform.system()} {platform.release()} ({platform.version()})"
        python_line = f"{platform.python_version()} ({platform.python_implementation()})"
        qt_line = f"Qt {qVersion()} via PySide6 {_pyside_version}"

        if locator is not None:
            ffmpeg_version = locator.version()
            ffmpeg_path = str(locator.ffmpeg)
            ffprobe_path = str(locator.ffprobe)
            ffmpeg_status_html = (
                f"<span style='color: #27ae60;'>&#x25CF;</span> "
                f"<b>Connected:</b> {_html_escape(ffmpeg_version)}"
            )
            ffmpeg_paths_html = (
                f"<div>ffmpeg: <code>{_html_escape(ffmpeg_path)}</code></div>"
                f"<div>ffprobe: <code>{_html_escape(ffprobe_path)}</code></div>"
            )
        else:
            ffmpeg_status_html = (
                "<span style='color: #c0392b;'>&#x25CF;</span> "
                "<b>Not connected.</b> Configure a path in Preferences."
            )
            ffmpeg_paths_html = ""

        custom = settings.ffmpeg_path()
        custom_line = (
            f"<code>{_html_escape(str(custom))}</code>"
            if custom is not None
            else "<i>(not set - using default discovery order)</i>"
        )

        # Assemble the final HTML. Each section is introduced by a small
        # bold heading; we avoid real <h1>/<h2> tags here so the section
        # titles match the compact style of the rest of the dialog.
        return f"""
<p><b>FFmpeg status</b></p>
<p>{ffmpeg_status_html}</p>
{ffmpeg_paths_html}

<p><b>Application</b></p>
<table cellpadding='2'>
  <tr><td>Name</td><td>&nbsp;&nbsp;FFmpeg GUI</td></tr>
  <tr><td>Version</td><td>&nbsp;&nbsp;{_html_escape(_app_version)}</td></tr>
  <tr><td>Custom FFmpeg path</td><td>&nbsp;&nbsp;{custom_line}</td></tr>
  <tr><td>Settings stored at</td>
      <td>&nbsp;&nbsp;<code>{_html_escape(settings.storage_location())}</code></td></tr>
</table>

<p><b>Runtime environment</b></p>
<table cellpadding='2'>
  <tr><td>Operating system</td><td>&nbsp;&nbsp;{_html_escape(os_line)}</td></tr>
  <tr><td>Architecture</td><td>&nbsp;&nbsp;{_html_escape(platform.machine() or 'unknown')}</td></tr>
  <tr><td>Python</td><td>&nbsp;&nbsp;{_html_escape(python_line)}</td></tr>
  <tr><td>GUI toolkit</td><td>&nbsp;&nbsp;{_html_escape(qt_line)}</td></tr>
  <tr><td>Executable</td><td>&nbsp;&nbsp;<code>{_html_escape(sys.executable)}</code></td></tr>
</table>

<p><b>System requirements</b></p>
<ul>
  <li>Python 3.10 or newer (when running from source).</li>
  <li>FFmpeg 4.0 or newer, including the matching <code>ffprobe</code>.</li>
  <li>Roughly 150 MB of disk space when bundled with FFmpeg; about 80 MB
      without the bundled binaries.</li>
  <li>A GPU is not required; playback uses Qt's software or hardware
      backend depending on what is installed, and export is CPU-based.</li>
</ul>

<p><b>Third-party components</b></p>
<ul>
  <li><b>Qt / PySide6</b> - LGPL-licensed GUI toolkit, provides the widgets,
      media playback, and threading primitives.</li>
  <li><b>FFmpeg</b> - LGPL or GPL depending on build flavour, does all
      video and audio processing. FFmpeg GUI only wraps it; all credit for
      the decoding, encoding, and filtering work belongs there.</li>
</ul>

<p><b>Credits</b></p>
<p>FFmpeg GUI is an independent project and is not affiliated with the
FFmpeg project or the Qt Company. For the FFmpeg licence text and source
availability, see <a href='https://ffmpeg.org/legal.html'>ffmpeg.org/legal.html</a>.</p>
"""


def _html_escape(text: str) -> str:
    """Tiny ampersand/angle-bracket escaper for values inlined into HTML.

    We escape before interpolation rather than relying on Qt's rich-text
    parser to "do the right thing" because a path containing ``&`` or an
    angle bracket (rare but possible on Linux) would otherwise render as
    an entity or start a tag, respectively.
    """
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
