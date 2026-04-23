"""
Immutable container for the video metadata we care about.

FFprobe can return a huge JSON blob with dozens of fields per stream, but
the UI only needs a handful: resolution, duration, frame rate, and a few
stream flags. Pulling those into a plain dataclass up front means the rest
of the app works with a stable, documented interface instead of dictionary
keys that could be missing or misspelled.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MediaInfo:
    """Summary of a single video file, built by :mod:`src.core.ffprobe`.

    Attributes
    ----------
    path:
        Absolute path to the source file. Kept as a :class:`~pathlib.Path`
        so callers can join, rename, or inspect it without re-parsing
        strings.
    width, height:
        Frame dimensions in pixels. Both zero means we couldn't determine
        the resolution, which callers should treat as an error condition
        rather than silently continuing.
    duration_ms:
        Total length in milliseconds. Stored as int for exact arithmetic
        (see the note at the top of :mod:`src.utils.time_utils`).
    fps:
        Frame rate in frames per second. Float because real-world values
        include 23.976, 29.97, etc., which don't round cleanly.
    has_audio:
        True when FFprobe reports at least one audio stream. We use this
        to decide whether to include ``-an`` or map audio during export.
    video_codec, audio_codec:
        Codec short names (``h264``, ``aac``, etc.) or empty string when
        the corresponding stream is absent. Used by the join logic to
        decide between stream-copy and re-encode paths.
    """

    path: Path
    width: int
    height: int
    duration_ms: int
    fps: float
    has_audio: bool
    video_codec: str
    audio_codec: str

    @property
    def resolution_str(self) -> str:
        """Human-friendly ``WxH`` for status bar display."""
        return f"{self.width}x{self.height}"

    @property
    def filename(self) -> str:
        """Just the file's basename, without the directory prefix."""
        return self.path.name
