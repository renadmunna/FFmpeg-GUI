"""
FFmpeg GUI - entry point.

Run this file from the project root to launch the application:

    python run.py

The actual application code lives under the ``src`` package. This thin wrapper
exists so we have one predictable, top-level script to point users and
packaging tools at, without committing to a particular import layout inside
``src``.
"""
from __future__ import annotations

import sys
from pathlib import Path

# When frozen by PyInstaller, sys.path is set up automatically.
# When running from source, we insert the project root so ``import src...``
# resolves without forcing the user to set PYTHONPATH manually.
_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.app import main  # noqa: E402  (import after sys.path tweak is intentional)


if __name__ == "__main__":
    # ``main`` returns an integer exit code; propagate it so shell scripts and
    # CI runners can tell whether the app quit cleanly or crashed.
    raise SystemExit(main(sys.argv))
