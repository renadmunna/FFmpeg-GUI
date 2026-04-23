"""
Thin wrapper around ``ffprobe`` for media inspection.

We only ever call ffprobe from the *main thread* in practice, because it's
a short operation (a few hundred milliseconds at worst) and the user has
just opened a file so brief latency is expected. If probe times became a
problem we would move it into the same :class:`QThread` infrastructure
used by :mod:`src.core.ffmpeg_runner`, but for now KISS applies.
"""
from __future__ import annotations

import json
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Dict, Optional

from src.core.media_info import MediaInfo
from src.utils.ffmpeg_locator import FFmpegLocator, _subprocess_no_window_flags


class ProbeError(RuntimeError):
    """Raised when ffprobe fails or produces output we can't interpret."""


def probe(locator: FFmpegLocator, path: Path) -> MediaInfo:
    """Run ffprobe and return a populated :class:`MediaInfo`.

    We ask ffprobe for JSON because parsing it is unambiguous; the default
    key=value output is easier to eyeball but painful to parse robustly.
    The ``-show_streams -show_format`` pair gives us everything the UI
    needs in a single subprocess call.
    """
    cmd = [
        str(locator.ffprobe),
        "-v", "error",                 # suppress banner/warnings so stdout is pure JSON
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]

    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=_subprocess_no_window_flags(),
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        raise ProbeError(f"Could not run ffprobe: {err}") from err

    if completed.returncode != 0:
        raise ProbeError(
            f"ffprobe failed with exit code {completed.returncode}:\n"
            f"{completed.stderr.strip()}"
        )

    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError as err:
        raise ProbeError(f"ffprobe returned invalid JSON: {err}") from err

    return _build_media_info(path, data)


def _build_media_info(path: Path, data: Dict) -> MediaInfo:
    """Translate an ffprobe JSON blob into a :class:`MediaInfo`.

    Kept as a pure function so we can unit-test it with recorded JSON
    fixtures later, without needing a real ffprobe on the CI machine.
    """
    streams = data.get("streams") or []
    video_stream = _first_stream(streams, "video")
    audio_stream = _first_stream(streams, "audio")

    if video_stream is None:
        raise ProbeError("No video stream found in file.")

    width = int(video_stream.get("width") or 0)
    height = int(video_stream.get("height") or 0)

    # Duration can live on the stream or on the format. The format value is
    # usually more accurate for container-level length (includes padding
    # frames, trailing silence, etc.), so we prefer it and fall back to the
    # stream value only if the container doesn't report one.
    fmt = data.get("format") or {}
    duration_seconds = _to_float(fmt.get("duration")) or _to_float(video_stream.get("duration")) or 0.0
    duration_ms = int(round(duration_seconds * 1000))

    fps = _parse_fps(video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate"))

    return MediaInfo(
        path=path.resolve(),
        width=width,
        height=height,
        duration_ms=duration_ms,
        fps=fps,
        has_audio=audio_stream is not None,
        video_codec=str(video_stream.get("codec_name") or ""),
        audio_codec=str(audio_stream.get("codec_name") or "") if audio_stream else "",
    )


def _first_stream(streams, codec_type: str) -> Optional[Dict]:
    """Return the first stream of the given type, or None."""
    for stream in streams:
        if stream.get("codec_type") == codec_type:
            return stream
    return None


def _to_float(value) -> Optional[float]:
    """Coerce an optional ffprobe string to float, returning None on failure."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_fps(rate: Optional[str]) -> float:
    """Parse ffprobe's ``num/den`` frame-rate string into a float.

    ffprobe returns frame rate as a rational like ``"30000/1001"`` for
    29.97 fps content. :class:`~fractions.Fraction` handles that cleanly,
    and we only collapse to float at the very end.
    """
    if not rate or rate == "0/0":
        return 0.0
    try:
        return float(Fraction(rate))
    except (ValueError, ZeroDivisionError):
        return 0.0
