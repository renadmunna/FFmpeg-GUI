"""
Application-wide persistent settings.

Qt provides :class:`QSettings` as a cross-platform key/value store that
picks the right backend for each operating system automatically: the
Windows registry under HKCU, a plist on macOS, and an INI file under
``~/.config`` on Linux. Using it means we don't have to hand-roll a
preferences file format, worry about file locking, or think about where
to put the file on each platform.

This module wraps ``QSettings`` with a small type-safe facade. The reason
to wrap it is simple: ``QSettings`` returns values typed as ``Any`` and
uses string keys, both of which make it easy to mistype a key or forget
to convert a value back to ``Path``. A facade lets the rest of the code
call ``settings.ffmpeg_path()`` and get an ``Optional[Path]`` back, with
the key string and the conversion logic living in exactly one place.

The ``QApplication.setOrganizationName`` and ``setApplicationName`` calls
in :mod:`src.app` determine where on disk these settings land, so you
must not instantiate :class:`AppSettings` before the ``QApplication`` is
constructed or the values will be stored against an empty org/app name
and effectively lost on the next run.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QSettings


class AppSettings:
    """Typed wrapper around the platform-native settings store.

    One instance can live for the entire application lifetime, but it is
    also cheap to reconstruct on demand - ``QSettings`` itself is a thin
    handle over the underlying backend, not a bulky object.
    """

    # Key strings are defined as class-level constants so the risk of a
    # typo breaking persistence silently drops to zero. Using a key that
    # doesn't exist yields the default value rather than an error, which
    # is exactly the kind of bug you notice only after shipping.
    _KEY_FFMPEG_PATH = "ffmpeg/custom_path"

    def __init__(self) -> None:
        # With no explicit arguments, QSettings picks up the organization
        # and application names we set on QApplication at startup. That
        # means the settings appear under a sensible path automatically
        # on every platform.
        self._settings = QSettings()

    # ------------------------------------------------------------------
    # FFmpeg path preference
    # ------------------------------------------------------------------
    def ffmpeg_path(self) -> Optional[Path]:
        """Return the user-configured FFmpeg directory, or ``None`` if unset.

        We store the raw string and convert to ``Path`` on read, so the
        on-disk format stays human-readable and editable. An empty string
        is treated the same as "not set" because that is what QSettings
        returns when you query a key that does not exist.
        """
        raw = self._settings.value(self._KEY_FFMPEG_PATH, "", type=str)
        return Path(raw) if raw else None

    def set_ffmpeg_path(self, path: Optional[Path]) -> None:
        """Persist or clear the FFmpeg directory preference.

        Passing ``None`` removes the key entirely, so the locator falls
        through to its defaults on the next run. We call ``sync()`` after
        the write to push the value to disk immediately; without it, the
        setting is only guaranteed to persist when the application exits
        cleanly, and we want the preference to survive even a hard crash.
        """
        if path is None:
            self._settings.remove(self._KEY_FFMPEG_PATH)
        else:
            self._settings.setValue(self._KEY_FFMPEG_PATH, str(path))
        self._settings.sync()

    # ------------------------------------------------------------------
    # Introspection helper (used by the About dialog)
    # ------------------------------------------------------------------
    def storage_location(self) -> str:
        """Return the path or registry key where settings are persisted.

        Shown in the About dialog so a curious user can find the file if
        they want to inspect or copy it (useful for migrating their
        preferences between machines).
        """
        return self._settings.fileName()
