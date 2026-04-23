"""
Application bootstrap.

This module sets up the :class:`QApplication`, attempts an initial FFmpeg
discovery, and hands control to the main window. It deliberately never
aborts the launch because of a missing FFmpeg: the whole point of the
Preferences menu is to let the user configure a path, and if the program
refused to start without one, the user would have no way to reach
Preferences in the first place. So instead of refusing to launch, we let
the main window open in a "degraded" state with a clearly-red FFmpeg
status indicator, disabled Open and Export actions, and a friendly
prompt suggesting the user set a path through Preferences. Once they do
that, the main window swaps in the new locator and everything lights up.
"""
from __future__ import annotations

import sys
from typing import List, Optional

from PySide6.QtWidgets import QApplication

from src import __version__
from src.gui.main_window import MainWindow
from src.utils.ffmpeg_locator import FFmpegLocator, FFmpegNotFound
from src.utils.settings import AppSettings


def main(argv: List[str]) -> int:
    """Start the application and return its exit code.

    Splitting this into a function (rather than putting the code under
    ``if __name__ == '__main__':``) makes it trivially callable from a
    launcher script, a test, or a packaged entry point.
    """
    app = QApplication(argv)
    app.setApplicationName("FFmpeg GUI")
    app.setApplicationVersion(__version__)
    # An organisation name matters on macOS and Windows because QSettings
    # uses it as a path component for preferences storage. Even if we don't
    # persist much today, setting it now avoids surprises later.
    app.setOrganizationName("FFmpeg GUI")

    # Settings must be constructed *after* QApplication because QSettings
    # reads the organisation and application names we just set; without
    # them, values would be stored against empty strings and effectively
    # lost between runs.
    settings = AppSettings()

    # Attempt initial discovery, but do not treat failure as fatal. A
    # missing FFmpeg is exactly the situation Preferences is designed to
    # fix, so the user must be able to reach the UI even when discovery
    # has failed. If discovery throws, we carry on with ``locator=None``
    # and let the main window present a friendly "please configure FFmpeg"
    # prompt once it is on screen.
    locator: Optional[FFmpegLocator]
    initial_error: Optional[FFmpegNotFound]
    try:
        locator = FFmpegLocator.discover(preferred_dir=settings.ffmpeg_path())
        initial_error = None
    except FFmpegNotFound as err:
        locator = None
        initial_error = err

    window = MainWindow(locator=locator, settings=settings, initial_error=initial_error)
    window.show()

    # app.exec() enters Qt's event loop and only returns when the user
    # closes the last window or we call app.quit(). Its return value is
    # conventionally the process exit code.
    return app.exec()


if __name__ == "__main__":  # pragma: no cover - convenience only
    raise SystemExit(main(sys.argv))
