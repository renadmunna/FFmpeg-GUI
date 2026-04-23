"""Portable-build helper around PyInstaller.

The manual PyInstaller command that packages this application has one
gotcha that trips up almost every user the first time: the ``--add-data``
flag uses ``:`` as its source/destination separator on Linux and macOS
but ``;`` on Windows. Forgetting this produces a build that lacks the
bundled ``bin/`` folder, and the user only discovers the problem when the
packaged binary opens and immediately complains that it cannot find
FFmpeg.

This script wraps the command so you can type ``python build_portable.py``
without worrying about the separator. It also prints an actionable
message if PyInstaller is not installed, because the default
``ModuleNotFoundError`` traceback is noisier than it needs to be.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


APP_NAME = "FFmpegGUI"
ENTRY_POINT = "run.py"


def main() -> int:
    """Entry point. Returns 0 on success, non-zero on any failure."""
    project_root = Path(__file__).resolve().parent

    # Verify PyInstaller is importable so we can show a friendlier message
    # than the default ModuleNotFoundError when it is missing.
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print(
            "PyInstaller is not installed in the current environment.\n"
            "Install it with:\n\n"
            "    pip install -r requirements-dev.txt\n",
            file=sys.stderr,
        )
        return 2

    # Pick the right data-spec separator for the current platform. The
    # PyInstaller source documents this explicitly: colon on Unix,
    # semicolon on Windows. ``os.pathsep`` gives exactly the right value.
    data_sep = os.pathsep

    bin_dir = project_root / "bin"
    if not bin_dir.exists() or not any(bin_dir.iterdir()):
        print(
            "WARNING: The bin/ folder is missing or empty. Dropping in\n"
            "ffmpeg and ffprobe binaries before packaging is strongly\n"
            "recommended; otherwise your portable build will rely on the\n"
            "user's system PATH at runtime.\n",
            file=sys.stderr,
        )

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",      # overwrite previous dist/ without asking
        "--clean",          # wipe PyInstaller's own cache for a deterministic build
        "--name", APP_NAME,
        "--windowed",       # no console window on Windows / macOS
        "--add-data", f"bin{data_sep}bin",
        ENTRY_POINT,
    ]

    print("Running:", " ".join(command))
    result = subprocess.run(command, cwd=project_root)
    if result.returncode != 0:
        print(f"\nPyInstaller failed with exit code {result.returncode}.", file=sys.stderr)
        return result.returncode

    dist_path = project_root / "dist" / APP_NAME
    print(
        f"\nBuild succeeded.\n"
        f"Portable app folder: {dist_path}\n"
        f"Entry point: {dist_path / (APP_NAME + ('.exe' if sys.platform == 'win32' else ''))}\n"
    )

    # shutil is imported only so a later extension can zip the dist folder;
    # keeping the import above documents intent without forcing it now.
    _ = shutil
    return 0


if __name__ == "__main__":
    sys.exit(main())
