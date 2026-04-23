"""
Time utilities.

Throughout the app we represent video positions as **integer milliseconds**
rather than floating-point seconds. Integers avoid rounding drift when we
add or subtract positions (critical for frame-accurate trimming) and they
map directly onto ``QSlider`` values, which must be integers anyway.

This module provides the two conversions we need constantly: turning a
millisecond count into a human ``HH:MM:SS.mmm`` string for display, and
parsing a user-typed string back into milliseconds.
"""
from __future__ import annotations

import re
from typing import Optional


# Accepts ``SS``, ``SS.mmm``, ``MM:SS``, ``MM:SS.mmm``, ``HH:MM:SS``,
# ``HH:MM:SS.mmm``. We keep this regex permissive so users can paste almost
# any conventional timestamp without the app nit-picking the format.
_TIME_RE = re.compile(
    r"^\s*"
    r"(?:(?P<h>\d+):)?"            # optional hours
    r"(?:(?P<m>\d+):)?"            # optional minutes
    r"(?P<s>\d+)"                  # seconds (required)
    r"(?:\.(?P<ms>\d{1,3}))?"      # optional fractional seconds, 1-3 digits
    r"\s*$"
)


def format_ms(ms: int, *, show_ms: bool = True) -> str:
    """Render a millisecond count as ``HH:MM:SS.mmm`` (or ``HH:MM:SS``).

    We always include hours, even for short clips, because a fixed-width
    format is easier to scan when values jump around during playback. The
    milliseconds are padded to three digits so the label width is stable,
    which stops neighbouring widgets from jiggling as the timecode ticks.
    """
    if ms < 0:
        # Negative values should never reach the UI, but we render them
        # sensibly just in case - easier to spot a bug than to crash.
        return "-" + format_ms(-ms, show_ms=show_ms)

    total_seconds, millis = divmod(ms, 1000)
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)

    if show_ms:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def parse_time(text: str) -> Optional[int]:
    """Parse a user-supplied timestamp into milliseconds.

    Returns ``None`` when the input doesn't match any accepted form. The
    caller decides how to react (flash the field red, ignore, etc.) so
    parsing stays side-effect free.

    We normalise a trailing fractional portion to exactly three digits by
    right-padding with zeros. That means ``"1.5"`` parses to 1500 ms and
    ``"1.50"`` also parses to 1500 ms, matching the user's intuition.
    """
    match = _TIME_RE.match(text)
    if not match:
        return None

    hours = int(match.group("h") or 0)
    minutes = int(match.group("m") or 0)
    seconds = int(match.group("s"))
    ms_str = match.group("ms") or "0"
    # Pad on the *right* so "1.5" means 500 ms (five-tenths), not 5 ms.
    ms_str = ms_str.ljust(3, "0")
    millis = int(ms_str)

    # If the user typed only two colon-separated parts, the regex captured
    # them as HH:SS. Re-shuffle so the less-significant value is seconds.
    if match.group("h") is not None and match.group("m") is None:
        # e.g. "1:30" -> treat 1 as minutes, not hours
        minutes, hours = hours, 0

    return ((hours * 3600) + (minutes * 60) + seconds) * 1000 + millis
