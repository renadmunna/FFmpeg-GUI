"""
FFmpeg command building and background execution.

This module has two responsibilities that are deliberately kept separate.
First, the pure functions near the top translate an :class:`EditState`
plus a :class:`MediaInfo` into a ready-to-run FFmpeg argument list. Those
functions have no side effects and can be unit-tested with no subprocess
at all. Second, the :class:`FFmpegWorker` class wraps a subprocess in a
:class:`QThread`, parses progress from FFmpeg's stderr, and emits Qt
signals the UI can connect to.

Splitting the two layers this way means the scariest parts of the app -
command construction and stderr parsing - are in one file, but each has
a narrow, testable surface.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import QObject, QThread, Signal

from src.core.edit_state import EditState
from src.core.media_info import MediaInfo
from src.utils.ffmpeg_locator import FFmpegLocator, _subprocess_no_window_flags


# ---------------------------------------------------------------------------
# Pure command-building helpers
# ---------------------------------------------------------------------------

def build_edit_command(
    locator: FFmpegLocator,
    media: MediaInfo,
    state: EditState,
    output_path: Path,
) -> List[str]:
    """Translate a single-file edit into an FFmpeg argument list.

    The resulting command applies, in this order: trim, crop, rotation,
    horizontal flip, vertical flip. Order matters inside the filter chain
    because a rotation changes which axis ``hflip`` mirrors across, and
    because cropping before rotating uses source-space coordinates which
    is what the crop panel produced.

    We always re-encode to H.264/AAC here because every supported edit
    except a straight format conversion requires a filter that stream
    copy can't handle. The caller can fall back to
    :func:`build_copy_command` when they know the edit is a no-op.
    """
    args: List[str] = [str(locator.ffmpeg), "-y"]  # -y overwrites output if it exists

    # Accurate seek: place ``-ss`` *after* ``-i`` so FFmpeg decodes from the
    # nearest keyframe and then discards frames up to the target. This is
    # slower than fast seek but lands on the exact millisecond we asked
    # for, which matters for trim precision. If the user doesn't trim,
    # both flags are omitted entirely.
    args += ["-i", str(media.path)]
    if state.trim_start_ms is not None:
        args += ["-ss", _ms_to_ffmpeg_time(state.trim_start_ms)]
    if state.trim_end_ms is not None:
        args += ["-to", _ms_to_ffmpeg_time(state.trim_end_ms)]

    video_filters = _build_video_filter_chain(state)
    if video_filters:
        args += ["-vf", ",".join(video_filters)]

    # Video encoder: libx264 is the most widely compatible H.264 encoder
    # and is bundled with every reasonable FFmpeg build. ``veryfast`` is a
    # sensible default for a GUI wrapper - it's much faster than ``medium``
    # at a small quality cost, and ``-crf 20`` gives visually transparent
    # results for most consumer footage.
    args += [
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "20",
        "-pix_fmt", "yuv420p",  # ensures playback in QuickTime and hardware decoders
    ]

    # Audio: copy when we can (no filters affect audio), re-encode when we
    # can't. Copying is near-instantaneous and lossless; AAC re-encoding
    # happens only when the source didn't carry an AAC track to begin with.
    if media.has_audio:
        if media.audio_codec == "aac":
            args += ["-c:a", "copy"]
        else:
            args += ["-c:a", "aac", "-b:a", "192k"]
    else:
        args += ["-an"]  # no audio stream, disable audio in output

    # ``-movflags +faststart`` rearranges the moov atom to the front of the
    # MP4 so browsers can start playback before the whole file downloads.
    # Harmless for local files, essential if the user uploads somewhere.
    args += ["-movflags", "+faststart"]

    args += [str(output_path)]
    return args


def build_copy_command(
    locator: FFmpegLocator,
    media: MediaInfo,
    output_path: Path,
) -> List[str]:
    """Build a stream-copy command for format-only conversion.

    When the user loads, say, an MKV file and clicks Export without making
    any edits, we should copy the streams into an MP4 container instead
    of re-encoding. This is almost instantaneous and perfectly lossless.
    The UI calls this function when :class:`EditState` is unmodified.
    """
    args = [
        str(locator.ffmpeg), "-y",
        "-i", str(media.path),
        "-c", "copy",
        "-movflags", "+faststart",
        str(output_path),
    ]
    return args


def build_join_command(
    locator: FFmpegLocator,
    inputs: List[Path],
    media_infos: List[MediaInfo],
    output_path: Path,
) -> List[str]:
    """Build a concat-filter command that joins multiple clips into one MP4.

    We use FFmpeg's ``concat`` filter rather than the concat demuxer.
    The demuxer is faster (it can stream-copy) but it requires every
    input to share codec, resolution, and frame rate exactly - a
    brittle assumption for arbitrary user files. The filter decodes
    each input and re-encodes once, which is slower but much more
    flexible.

    The filter itself still has one hard requirement, though: every
    stream it receives must present the same video dimensions, the
    same sample aspect ratio, and a common frame rate. If one input
    is 1920x1080 and the next is 1280x720, the filter cannot stitch
    them because there is no well-defined way to put a 720p frame
    onto a 1080p timeline without deciding whether to stretch, scale,
    or pad. So before we concatenate we normalise every input to a
    common target.

    The target is picked to be as large as the largest input needs it
    to be: the width is the maximum of every input's width, the
    height is the maximum of every input's height, and the frame rate
    is the maximum of every input's frame rate. The reason for this
    "maximum wins" rule is a quality concern. If we instead forced
    everything to match the first clip, a 1080p clip following a 720p
    clip would be scaled *down* to 720p and lose detail permanently.
    Scaling down in a re-encode is throwing information away for no
    gain. Scaling up is also imperfect (it softens the image because
    there is no new detail to invent), but it preserves every pixel
    of every input and keeps the door open to delivering the output
    at the full resolution of its best source. The same logic applies
    to frame rate: forcing a 60 fps clip down to 24 fps drops two
    thirds of its motion information forever, whereas forcing a 24
    fps clip up to 60 fps simply duplicates frames. One option loses
    information, the other does not.

    For each input the filter graph looks like this::

        [0:v:0]scale=W:H:force_original_aspect_ratio=decrease,
               pad=W:H:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=F[v0]
        [0:a:0]aresample=48000,
               aformat=sample_fmts=fltp:channel_layouts=stereo[a0]

    ``scale`` with ``force_original_aspect_ratio=decrease`` resizes the
    frame to fit inside the target rectangle without stretching; ``pad``
    then adds black bars to reach the exact target dimensions, centred
    by the ``(ow-iw)/2`` expressions; ``setsar=1`` forces the sample
    aspect ratio to 1:1 because even identical pixel dimensions are
    not enough if the SAR differs; ``fps`` converts to the common
    frame rate by duplicating or dropping frames as needed. The audio
    side resamples to a common rate and forces stereo float output.
    After every input has been run through its own normalisation
    chain, the concat filter gets a uniform sequence it can stitch
    without complaining.

    The resulting filter_complex expression is::

        [0:v:0]...[v0];[0:a:0]...[a0];[1:v:0]...[v1];[1:a:0]...[a1];
        [v0][a0][v1][a1]concat=n=2:v=1:a=1[vout][aout]
    """
    if len(inputs) < 2:
        raise ValueError("Join requires at least two input files.")
    if len(inputs) != len(media_infos):
        raise ValueError(
            "inputs and media_infos must be the same length; got "
            f"{len(inputs)} paths and {len(media_infos)} info objects."
        )

    # Pick the target dimensions axis by axis, so the output canvas is
    # always at least as large as the largest input in each axis. Both
    # axes are independent: for a queue of one landscape and one
    # portrait clip, the landscape clip pushes the width up and the
    # portrait clip pushes the height up, and the canvas ends up
    # square. That is the honest extension of the "maximum wins" rule
    # to mixed orientations, even though neither original clip fills
    # the square on its own.
    #
    # We round each dimension down to the nearest even number because
    # libx264 requires even dimensions in its default yuv420p chroma
    # subsampling - odd values cause the encoder to error out at
    # export time (not at filter-graph parse time), which would be a
    # confusing failure to surface to the user. Rounding up would
    # also work mathematically, but rounding down guarantees we never
    # ask for a pixel that is not supported by any input frame after
    # scaling.
    target_w = max(2, (max(m.width for m in media_infos) // 2) * 2)
    target_h = max(2, (max(m.height for m in media_infos) // 2) * 2)

    # Frame rate follows the same "maximum wins" principle for the
    # same reason: choosing the max never drops motion information,
    # only duplicates frames from slower inputs. We filter out any
    # fps values that are zero or negative before taking the max,
    # because a probe that fails to detect a frame rate reports zero
    # and feeding max() a list of zeros would give a nonsense target.
    # If every input lacks a frame rate (extremely unusual - usually
    # only static-image "videos" in unusual containers) we fall back
    # to 30, which is a safe default the fps filter knows how to
    # produce by duplicating frames.
    valid_fps = [m.fps for m in media_infos if m.fps > 0]
    target_fps = max(valid_fps) if valid_fps else 30.0

    # 48 kHz stereo is the MP4/AAC convention and what every browser
    # and player expects. Resampling to this target also sidesteps
    # the concat filter's audio-mismatch complaints when inputs use
    # different sample rates.
    target_sample_rate = 48000

    args: List[str] = [str(locator.ffmpeg), "-y"]
    for path in inputs:
        args += ["-i", str(path)]

    # Build the per-input normalisation chains plus the concat tail.
    # We keep each chain as a separate entry in ``filter_parts`` and
    # join them with semicolons at the end, because reading a huge
    # single-line filter_complex expression when debugging is painful
    # and the extra list-building costs nothing.
    filter_parts: List[str] = []
    concat_inputs = ""
    for i in range(len(inputs)):
        # Video chain: scale-to-fit, pad-to-exact, force square pixels,
        # resample frame rate. The order matters: scale must precede
        # pad so the pad computes ow/oh against the post-scale size,
        # and setsar must come after pad so the padded canvas is what
        # gets the 1:1 aspect lock. fps goes last so earlier filters
        # can operate in the input's native frame rate.
        filter_parts.append(
            f"[{i}:v:0]"
            f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
            f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2,"
            f"setsar=1,"
            f"fps={target_fps:.4f}"
            f"[v{i}]"
        )
        # Audio chain: resample to the common rate and force a
        # uniform sample format (32-bit float planar) with a stereo
        # channel layout. aformat will upmix mono and downmix 5.1 as
        # needed; aresample handles rate conversion with a default
        # high-quality algorithm.
        filter_parts.append(
            f"[{i}:a:0]"
            f"aresample={target_sample_rate},"
            f"aformat=sample_fmts=fltp:channel_layouts=stereo"
            f"[a{i}]"
        )
        concat_inputs += f"[v{i}][a{i}]"

    filter_parts.append(
        f"{concat_inputs}concat=n={len(inputs)}:v=1:a=1[vout][aout]"
    )
    filter_graph = ";".join(filter_parts)

    args += [
        "-filter_complex", filter_graph,
        "-map", "[vout]",
        "-map", "[aout]",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        # Explicitly set output audio parameters to the same values
        # the filter chain normalises to, so the final MP4 container
        # carries the expected sample rate and channel count.
        "-ar", str(target_sample_rate),
        "-ac", "2",
        "-movflags", "+faststart",
        str(output_path),
    ]
    return args


def _build_video_filter_chain(state: EditState) -> List[str]:
    """Return the ordered list of ``-vf`` filter expressions for this edit.

    Order rationale:

    * Crop first so rotation/flip operate on the smaller, already-trimmed
      frame - slightly faster and avoids confusion about coordinate
      systems (the crop rectangle is given in source pixels).
    * Rotation before flip because ``transpose=1`` (90 CW) already
      includes a horizontal flip internally; applying hflip after gives
      users the mental model "the flip mirrors what you see, after the
      rotation took effect".
    """
    filters: List[str] = []

    if state.crop is not None:
        c = state.crop
        filters.append(f"crop={c.width}:{c.height}:{c.x}:{c.y}")

    if state.rotation == 90:
        filters.append("transpose=1")
    elif state.rotation == 180:
        # Two transpose=2 also works, but hflip+vflip is one filter step
        # cheaper and expresses the intent more clearly.
        filters.append("hflip,vflip")
    elif state.rotation == 270:
        filters.append("transpose=2")

    if state.flip_horizontal:
        filters.append("hflip")
    if state.flip_vertical:
        filters.append("vflip")

    return filters


def _ms_to_ffmpeg_time(ms: int) -> str:
    """Format an integer ms count as the ``HH:MM:SS.mmm`` FFmpeg expects."""
    total_seconds, millis = divmod(max(ms, 0), 1000)
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

# FFmpeg writes progress to stderr in lines like
#   frame=  123 fps= 30 q=28.0 size=    1024kB time=00:00:04.10 bitrate=...
# We only need the ``time=`` field to calculate percentage complete.
_FFMPEG_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")


class FFmpegWorker(QObject):
    """Runs an FFmpeg subprocess on a worker thread and reports progress.

    We use the classic "worker object moved to QThread" pattern rather
    than subclassing QThread directly. The pattern keeps the worker's
    responsibilities (running ffmpeg, parsing stderr) separate from Qt's
    thread-management responsibilities, and it's the approach the Qt docs
    recommend for new code.

    Signals:
        progress(int): 0-100 percent complete. Emitted whenever we parse
            a new time= line from FFmpeg's stderr.
        log(str): One line of FFmpeg output, for the UI's log panel.
        finished(bool, str): Emitted exactly once when the subprocess
            exits. The bool is True on success (exit code 0); the string
            is an error message on failure or an empty string on success.
    """

    progress = Signal(int)
    log = Signal(str)
    finished = Signal(bool, str)

    def __init__(self, command: List[str], total_duration_ms: int) -> None:
        super().__init__()
        self._command = command
        self._total_duration_ms = max(total_duration_ms, 1)  # avoid /0
        self._process: Optional[subprocess.Popen] = None
        self._cancelled = False

    def run(self) -> None:
        """Launch FFmpeg and stream its output until it exits.

        This method is the slot that the owning :class:`QThread`'s
        ``started`` signal connects to. It must never raise: any exception
        escaping here would be swallowed by Qt and the UI would hang on a
        progress bar that never advances.
        """
        try:
            self._process = subprocess.Popen(
                self._command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,          # line-buffered stderr
                creationflags=_subprocess_no_window_flags(),
            )
        except OSError as err:
            self.finished.emit(False, f"Could not start FFmpeg: {err}")
            return

        # Drain stderr line by line. FFmpeg interleaves progress lines with
        # diagnostic output; we emit everything to the log panel but only
        # use the progress-shaped lines to move the bar.
        assert self._process.stderr is not None  # for type checkers
        for line in self._process.stderr:
            if self._cancelled:
                break
            line = line.rstrip()
            if not line:
                continue
            self.log.emit(line)

            match = _FFMPEG_TIME_RE.search(line)
            if match:
                hours = int(match.group(1))
                minutes = int(match.group(2))
                seconds = float(match.group(3))
                elapsed_ms = int(((hours * 3600) + (minutes * 60) + seconds) * 1000)
                percent = min(100, int(elapsed_ms * 100 / self._total_duration_ms))
                self.progress.emit(percent)

        return_code = self._process.wait()

        if self._cancelled:
            self.finished.emit(False, "Cancelled by user.")
        elif return_code == 0:
            # A clean run ends at 100% even if FFmpeg didn't emit a final
            # time= line that matched the full duration.
            self.progress.emit(100)
            self.finished.emit(True, "")
        else:
            self.finished.emit(
                False,
                f"FFmpeg exited with code {return_code}. See log for details.",
            )

    def cancel(self) -> None:
        """Request early termination; safe to call from the UI thread.

        We call ``terminate()`` rather than ``kill()`` so FFmpeg gets a
        chance to flush partial output and release file handles cleanly.
        The ``run()`` loop checks ``_cancelled`` on each stderr line so
        the ``finished`` signal carries a "cancelled" message instead of
        "exit code -15", which would be confusing to users.
        """
        self._cancelled = True
        if self._process is not None and self._process.poll() is None:
            try:
                self._process.terminate()
            except OSError:
                # Process already exited between our poll and terminate.
                # Nothing to do - the stderr drain loop will exit shortly.
                pass


class FFmpegJob(QObject):
    """Convenience owner that pairs a worker with its thread.

    Creating a :class:`QThread`, moving a worker onto it, wiring the
    ``started``/``finished`` signals, and cleaning both up afterwards is
    boilerplate we'd otherwise repeat every time we export. This class
    bundles it so callers just write::

        job = FFmpegJob(command, duration_ms)
        job.worker.progress.connect(progress_bar.setValue)
        job.worker.finished.connect(on_done)
        job.start()

    The job stays alive until its thread finishes, which Qt signals via
    :meth:`QThread.finished`. Holding a reference in the owning widget
    until then is the caller's job; otherwise Python may garbage-collect
    the job mid-run and the thread will be orphaned.
    """

    def __init__(self, command: List[str], total_duration_ms: int) -> None:
        super().__init__()
        self.thread = QThread()
        self.worker = FFmpegWorker(command, total_duration_ms)
        self.worker.moveToThread(self.thread)

        # Kick the worker off when the thread starts.
        self.thread.started.connect(self.worker.run)
        # When the worker reports it's done, ask the thread to quit so we
        # don't leak OS threads. deleteLater()s are scheduled so Python
        # and Qt agree on the teardown order (Qt deletes the worker, then
        # the thread, after event loops have processed remaining signals).
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)

    def start(self) -> None:
        self.thread.start()

    def cancel(self) -> None:
        self.worker.cancel()
