"""
Mutable container for the edit a user is currently composing.

Think of :class:`EditState` as the single source of truth about *what* the
user has set up, separated from *how* the UI displays it. When the user
drags the crop rectangle, the crop panel calls ``state.crop = ...``. When
they adjust the trim slider, the trim panel updates ``state.trim_...``.
At export time we read the whole thing and translate it into FFmpeg
arguments.

Keeping this state object isolated has two practical benefits. First, the
FFmpeg command builder becomes a pure function of ``EditState`` plus the
source ``MediaInfo`` - easy to read and easy to test. Second, if we later
add a "reset all edits" button or undo/redo support, there's exactly one
place to mutate.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class CropRect:
    """Integer pixel rectangle describing the crop region.

    We store the crop in *source* pixel coordinates, not in the displayed
    video widget's coordinates. The preview may scale the video down to
    fit its panel, so scene-space coordinates would drift every time the
    user resizes the window. Source coordinates are stable and are exactly
    what FFmpeg's ``crop`` filter expects.
    """

    x: int
    y: int
    width: int
    height: int

    def is_valid(self, source_w: int, source_h: int) -> bool:
        """True when this rectangle fits entirely within the source frame."""
        return (
            self.width > 0
            and self.height > 0
            and self.x >= 0
            and self.y >= 0
            and self.x + self.width <= source_w
            and self.y + self.height <= source_h
        )


@dataclass
class EditState:
    """All the user-adjustable edit parameters for the current file.

    Fields default to "no edit" so an unchanged ``EditState`` produces an
    FFmpeg command equivalent to a plain format conversion.
    """

    # Trim points in milliseconds. ``None`` means "from the start" and
    # "to the end" respectively, so the command builder can skip emitting
    # the corresponding ``-ss`` / ``-to`` flag when no trim was set.
    trim_start_ms: Optional[int] = None
    trim_end_ms: Optional[int] = None

    # Crop rectangle in source pixels, or None for "no crop".
    crop: Optional[CropRect] = None

    # Rotation in degrees, clockwise, restricted to 0/90/180/270. Using an
    # integer rather than an Enum keeps interop with Qt's setRotation()
    # straightforward; we validate the value wherever it's set.
    rotation: int = 0

    # Independent horizontal and vertical flip flags. FFmpeg applies hflip
    # and vflip as separate filters, so two booleans matches the pipeline
    # more directly than a "flip mode" enum would.
    flip_horizontal: bool = False
    flip_vertical: bool = False

    # Files queued for the Join tab. Empty list means we're in single-file
    # edit mode. We keep this on the same state object so switching tabs
    # doesn't lose the list the user has been curating.
    join_files: List[str] = field(default_factory=list)

    def reset(self) -> None:
        """Clear all edits, leaving the state as if no file were loaded.

        Used when the user opens a new file, so old trim points and crop
        rectangles from the previous video don't silently carry over.
        """
        self.trim_start_ms = None
        self.trim_end_ms = None
        self.crop = None
        self.rotation = 0
        self.flip_horizontal = False
        self.flip_vertical = False
        # join_files is intentionally not cleared here; the join tab owns it.

    def effective_trim(self, duration_ms: int) -> Tuple[int, int]:
        """Resolve ``None`` trim ends to the real start/end for display.

        The command builder uses the raw ``trim_start_ms`` / ``trim_end_ms``
        so it can omit the flags, but UI widgets often want concrete
        numbers (e.g. "selected duration: 00:00:04.500").
        """
        start = self.trim_start_ms if self.trim_start_ms is not None else 0
        end = self.trim_end_ms if self.trim_end_ms is not None else duration_ms
        return start, end
