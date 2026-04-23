"""
Locate FFmpeg and FFprobe executables.

There are two deployment scenarios we care about. The first is a developer
running from source on a machine where FFmpeg is installed system-wide; in
that case we should simply use whatever ``ffmpeg`` resolves to on PATH.
The second is a portable distribution where we ship FFmpeg binaries inside
the application folder so the end user doesn't need to install anything.
The portable case must win when both are present, because that's the whole
point of bundling: deterministic behaviour regardless of the host system.

The :class:`FFmpegLocator` below implements exactly that preference order,
and raises :class:`FFmpegNotFound` with a human-readable breakdown if
neither source works.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


class FFmpegNotFound(RuntimeError):
    """Raised when neither a bundled nor a PATH FFmpeg can be located.

    The ``details`` attribute carries a multi-line string describing every
    place we looked. The UI layer uses it to produce a helpful error
    dialog rather than forcing the user to read a traceback.
    """

    def __init__(self, details: str) -> None:
        super().__init__("FFmpeg/FFprobe not found")
        self.details = details


@dataclass(frozen=True)
class FFmpegLocator:
    """Holds paths to the two binaries we invoke during normal operation.

    We resolve both executables once, at startup, so every later call knows
    exactly which ffmpeg it is talking to. ``frozen=True`` makes instances
    hashable and read-only, which is what we want for a configuration value
    that should never mutate after discovery.
    """

    ffmpeg: Path
    ffprobe: Path

    @classmethod
    def discover(cls, preferred_dir: Optional[Path] = None) -> "FFmpegLocator":
        """Find FFmpeg and FFprobe, preferring an explicit user setting.

        Search order, in decreasing priority:

        1. The ``preferred_dir`` argument, when supplied. This is the
           folder the user has set through the Preferences dialog; if
           they went to the trouble of configuring it, we honour it
           before anything else. If it fails, we fall through to the
           remaining locations rather than refusing to start - otherwise
           a stale preference from a deleted folder would brick the app
           on the next launch.
        2. ``<app>/bin`` next to the running script or frozen executable.
           This is where a portable distribution places its private copy.
        3. The directory set by the ``FFMPEG_GUI_BIN`` environment variable,
           for users who want to point the app at a custom install without
           using the Preferences dialog (e.g. from a build script).
        4. Whatever ``ffmpeg``/``ffprobe`` resolves to on the system PATH.

        The first entry that yields working, executable binaries wins.
        """
        tried: List[str] = []

        # The "app root" differs between running from source and running from
        # a PyInstaller bundle. ``sys.frozen`` is set by PyInstaller; in that
        # case the binaries are alongside the launcher. Otherwise we walk up
        # from this file to the project root.
        if getattr(sys, "frozen", False):
            app_root = Path(sys.executable).resolve().parent
        else:
            app_root = Path(__file__).resolve().parents[2]

        # Build the ordered candidate list. We label each entry so the
        # FFmpegNotFound error - if we raise one - tells the user exactly
        # which directory corresponded to which configuration source. A
        # bare list of paths in an error dialog is much less useful.
        candidates: List[tuple] = []
        if preferred_dir is not None:
            candidates.append(("user preference", Path(preferred_dir)))
        candidates.append(("bundled bin/", app_root / "bin"))

        env_override = os.environ.get("FFMPEG_GUI_BIN")
        if env_override:
            candidates.append(("FFMPEG_GUI_BIN env var", Path(env_override)))

        for label, directory in candidates:
            ffmpeg = _find_in_directory(directory, "ffmpeg")
            ffprobe = _find_in_directory(directory, "ffprobe")
            tried.append(
                f"  - {label}: {directory}  "
                f"(ffmpeg={bool(ffmpeg)}, ffprobe={bool(ffprobe)})"
            )
            if ffmpeg and ffprobe:
                return cls(ffmpeg=ffmpeg, ffprobe=ffprobe)

        # Fall back to PATH. shutil.which handles Windows' ``.exe`` extension
        # and OS-specific executable lookup rules for us.
        path_ffmpeg = shutil.which("ffmpeg")
        path_ffprobe = shutil.which("ffprobe")
        tried.append(
            f"  - system PATH  (ffmpeg={bool(path_ffmpeg)}, ffprobe={bool(path_ffprobe)})"
        )
        if path_ffmpeg and path_ffprobe:
            return cls(ffmpeg=Path(path_ffmpeg), ffprobe=Path(path_ffprobe))

        raise FFmpegNotFound("\n".join(tried))

    def version(self) -> str:
        """Return the first line of ``ffmpeg -version`` for display in About.

        We don't care about the full banner, just a short identifier. Running
        with a 2-second timeout guards against a corrupt binary that hangs.
        """
        try:
            completed = subprocess.run(
                [str(self.ffmpeg), "-version"],
                capture_output=True,
                text=True,
                timeout=2,
                # Hide the console window that would otherwise flash on Windows.
                creationflags=_subprocess_no_window_flags(),
            )
            first_line = completed.stdout.splitlines()[0] if completed.stdout else ""
            return first_line or "unknown"
        except (OSError, subprocess.TimeoutExpired):
            return "unknown"


def _find_in_directory(directory: Path, name: str) -> Optional[Path]:
    """Return an executable matching ``name`` inside ``directory``, or None.

    On Windows we must also consider the ``.exe`` extension. We deliberately
    do *not* follow symlinks into other directories or descend recursively;
    the bundled ``bin`` folder is flat by design.
    """
    if not directory.is_dir():
        return None

    # Order matters: prefer the extension-less name on Unix, then ``.exe`` on
    # Windows. os.access with X_OK confirms the file is actually executable,
    # which matters more than existence alone.
    for candidate_name in (name, f"{name}.exe"):
        candidate = directory / candidate_name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _subprocess_no_window_flags() -> int:
    """Return the Popen ``creationflags`` value that hides console windows.

    On Windows, every subprocess we launch would briefly flash a console
    window if we didn't pass CREATE_NO_WINDOW. On non-Windows platforms this
    flag doesn't exist, so we return 0 and subprocess ignores it.
    """
    if sys.platform == "win32":
        # 0x08000000 is CREATE_NO_WINDOW. We avoid importing from subprocess
        # by name because the constant is only defined on Windows builds.
        return 0x08000000
    return 0
