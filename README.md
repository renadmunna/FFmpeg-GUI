# FFmpeg GUI

A lightweight, portable desktop video editor written in Python. It is a
focused graphical wrapper around FFmpeg and FFprobe, built for people who
want a simple way to cut, crop, rotate, flip, join, and convert videos
without learning command-line flags and without installing a full
non-linear editor. Every export produces an MP4 file, and the application
is designed to be packaged as a single portable executable when you are
ready to ship it.

## What the application does

When you open a video the program shows an embedded preview with play,
pause, stop, seek, and frame-step controls. Four edit panels sit next to
the preview in a tab widget. The trim panel lets you mark a start and end
point down to the millisecond, either by typing times directly or by
parking the playhead on the exact frame you want and pressing a button.
The crop panel overlays an interactive rectangle on the preview itself;
you can drag its corners and edges freely or lock it to a common aspect
ratio such as 1:1, 4:3, 16:9, or 9:16. The rotate-and-flip panel applies
rotations in 90-degree increments and horizontal or vertical flips, and
the preview reflects the transform immediately so you can see the result
before exporting. The join panel accepts any number of extra files, lets
you reorder and remove them, and concatenates them into a single MP4. Any
time your edit state is empty (for example, you opened a MOV and want to
store it as an MP4 without changing anything else) the application
detects this and uses stream copy, which completes in milliseconds
without re-encoding.

When you join clips with different resolutions, aspect ratios, or frame
rates, the application normalises every input before concatenating so
that FFmpeg's concat filter receives a uniform sequence it can stitch
without complaining. The normalisation target is chosen axis by axis
to be as large as the largest input needs it to be: the target width
is the maximum of every input's width, the target height is the
maximum of every input's height, and the target frame rate is the
maximum of every input's frame rate. Clips smaller than the target are
scaled up proportionally and padded with black bars to centre them
inside the target canvas, and clips slower than the target frame rate
have frames duplicated to fill. Audio is always resampled to 48 kHz
stereo because that is the MP4/AAC convention and every concat input
has to agree on sample rate and channel layout anyway. The reasoning
behind "maximum wins" is that it never throws information away:
scaling down or dropping frames would lose detail from the best
sources in the queue permanently, whereas scaling up and duplicating
frames are reversible in the sense that they preserve every pixel of
every input. If your queue mixes a landscape clip and a portrait clip
the target canvas ends up square, because the max width comes from
the landscape clip and the max height from the portrait clip, and
both clips then sit letterboxed inside that square.

## Running from source

The application expects Python 3.10 or newer. Create an isolated virtual
environment before installing dependencies so that PySide6 does not clash
with any system-wide Qt installation you may have.

On Linux or macOS:

    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt
    python run.py

On Windows, in a regular Command Prompt (cmd.exe — not PowerShell):

    python -m venv .venv
    .venv\Scripts\activate.bat
    pip install -r requirements.txt
    python run.py

The activation script is `activate.bat` for CMD. If you ever switch to
PowerShell you would use `Activate.ps1` instead, but there is no need
for that here — everything works identically from a plain CMD window.
After `activate.bat` runs, the prompt will be prefixed with `(.venv)`
so you can see that the virtual environment is active.

The first time you launch the program it will look for `ffmpeg` and
`ffprobe` in the usual places (see the next section for the full
priority order). If it cannot find them the application still opens —
it would be a chicken-and-egg problem to refuse to launch, because the
Preferences menu that lets you configure a path lives inside the
application itself. Instead, you will see a friendly prompt offering to
open Preferences right then, the FFmpeg status chip in the bottom-right
corner will turn red to indicate "not connected," and the menu items
that actually need FFmpeg (Open video, Open multiple for join) will be
greyed out until you configure a working path. Preferences and About
remain available so you can fix the situation or inspect what the
application is looking at.

## Where the FFmpeg binaries come from

The locator in `src/utils/ffmpeg_locator.py` searches four places in
order and uses the first one that yields a working pair of binaries.
The highest priority goes to anything you set explicitly through the
application's Preferences menu — if you point it at a folder that
contains `ffmpeg` and `ffprobe`, those binaries are used in preference
to every other source. Second priority goes to the `bin/` folder that
ships next to the application, which is where a portable distribution
keeps its private copy. Third priority is the `FFMPEG_GUI_BIN`
environment variable, which is convenient for build scripts or shared
development setups where you do not want to click through a dialog.
Fourth and last priority is the system `PATH`, which is what most
developer machines will already have configured.

You can configure a custom path at any time by opening the
**Preferences → FFmpeg path…** menu entry. The dialog has a field for
the folder, a Browse button to pick one without typing, and a Clear
button that removes the preference so the application falls back to its
usual discovery order. As you type, the dialog runs a live test and
tells you whether the binaries were actually found and what version
string FFmpeg reported, so you do not have to save and close to find
out whether your path was valid.

The preference is persisted through Qt's `QSettings` in whatever store
is native to your operating system. On Windows that is the registry
under `HKCU\Software\FFmpeg GUI`; on Linux it is an INI file under
`~/.config/FFmpeg GUI`; on macOS it is a `.plist` under
`~/Library/Preferences`. The exact path is shown in the About dialog so
you can find, copy, or delete the settings file without hunting for it.

For a portable distribution the recommended approach is still to drop
the two binaries into the `bin/` folder before packaging. That way the
packaged application is completely self-contained and works on a
machine that has never had FFmpeg installed, while still allowing the
user to override the bundled copy through Preferences if they want to
use a newer FFmpeg version than the one you shipped. You can get
static builds from
[ffmpeg.org/download.html](https://ffmpeg.org/download.html) for every
major platform. Both binaries together are around 80 to 120 megabytes
depending on platform and build flavor, which dominates the final
package size but is unavoidable for a working FFmpeg wrapper.

## Live connection status

The bottom-right corner of the main window always shows a small status
chip for the current FFmpeg connection. A green dot with a short
version banner means the binaries were found and responded to a
`-version` query; a red dot means no working FFmpeg is reachable. The
chip's tooltip carries the full paths and the complete version banner
from FFmpeg, so you can confirm at a glance which binary is in use
without opening any dialog. When you change the FFmpeg path through
Preferences the chip updates immediately, which is the fastest way to
verify that a new path took effect.

## About dialog

The **Help → About FFmpeg GUI** menu opens a detailed About dialog
that aggregates every piece of information a user or support person is
likely to want in one place: the application name and version, the
current FFmpeg connection state and banner, the full `ffmpeg` and
`ffprobe` paths, the custom path preference (if any), the location of
the settings file, the operating system and architecture, the Python
and Qt versions, the Python executable path, the system requirements,
and a list of third-party components with their licence summaries. You
can select and copy any of this text, which is handy when filing bug
reports.

## Packaging as a portable executable

The application is packaging-ready in the sense that it has no hardcoded
absolute paths, uses only the standard library and PySide6 at runtime,
and keeps its data-directory layout flat. For a one-file or one-folder
build, PyInstaller is the simplest option:

    pip install -r requirements-dev.txt
    pyinstaller --noconfirm --name "FFmpegGUI" --windowed \
                --add-data "bin:bin" run.py

The `--add-data` argument tells PyInstaller to include the `bin/` folder
(with the FFmpeg binaries inside) in the packaged output. On Windows
the separator is a semicolon instead of a colon, so the argument becomes
`--add-data "bin;bin"`. The `--windowed` flag suppresses the console
window on Windows and macOS so the user sees only the GUI. The resulting
folder under `dist/FFmpegGUI` is everything the user needs; they can
copy it to another machine and run it without installation.

A helper script `build_portable.py` is included that performs the same
command with the right platform-specific separator; run `python
build_portable.py` if you would rather not type the flags.

## Project structure

The code is organized around a clear separation between the GUI layer,
the FFmpeg integration layer, and the cross-cutting utility modules.
Everything visible to the user lives in `src/gui/`: the main window, the
video preview with its crop overlay, and one module per edit panel
(trim, crop, transform, join). The `src/core/` package is responsible
for all FFmpeg and FFprobe interaction; it has no imports from the GUI
layer and could in principle be reused from a command-line front end.
The `src/utils/` package holds the ffmpeg binary locator and the
millisecond time-formatting helpers. The entry point is `run.py` at the
repository root, which exists so that the application can be launched
with a single file without needing to set `PYTHONPATH`.

## Design decisions worth noting

PySide6 was chosen over Tkinter because it provides a native video
preview through `QMediaPlayer` and `QGraphicsVideoItem`, hardware
accelerated on every major platform, and because its graphics view
framework makes the crop overlay implementation a straightforward layer
of `QGraphicsItem` instances instead of a pile of custom painting code.
PySide6 is LGPL-licensed, so you can bundle it in commercial products
without paying for a Qt license.

FFmpeg is invoked directly via `subprocess` rather than through a
wrapper library. The direct approach removes a dependency, gives the
application full control over argument ordering (which matters for
accurate seeking, where `-ss` must appear after `-i` to decode from a
keyframe and then discard frames), and makes the progress parser simpler
because we read FFmpeg's own `time=` reports from stderr.

Every filter-chain edit is applied in a single FFmpeg invocation in the
order crop, rotate, horizontal flip, vertical flip. That order is not
arbitrary: cropping first keeps the crop coordinates in source pixel
space, which is exactly what the crop overlay produces; rotating
afterwards means the crop is applied before rotation changes which axis
is horizontal. Trimming uses accurate seek (decoding from the nearest
keyframe and discarding frames up to the target) so the cut lands on the
exact millisecond the user selected.

Joining multiple clips uses the FFmpeg `concat` filter rather than the
`concat` demuxer. The demuxer would be faster because it can stream-copy,
but it requires every input to share codec, resolution, and frame rate
exactly, which is a brittle assumption for arbitrary user files. The
filter-based approach decodes and re-encodes once, which is slower but
works regardless of what the user throws at it.

The GUI never calls FFmpeg on the main thread. Every export runs inside
an `FFmpegWorker` object that has been moved onto a `QThread`, and
progress is reported back to the UI through Qt signals. The export
dialog is modal for the duration of a job so the user cannot start a
second export that would race for the same output file, and the cancel
button asks FFmpeg to terminate cleanly rather than killing the process
outright so any partial output is flushed properly.

All times are represented as integer milliseconds throughout the code
base. That eliminates floating-point drift between widgets that display
the position and widgets that calculate a trim duration, and it matches
the integer range that `QSlider` and `QMediaPlayer` use natively.

## Troubleshooting

If the application starts but complains that FFmpeg cannot be found,
either install FFmpeg system-wide, set the `FFMPEG_GUI_BIN` environment
variable to a folder that contains both `ffmpeg` and `ffprobe`, or drop
both binaries into the `bin/` folder next to the application.

If an export fails with an exit code, open the export dialog's log
panel. Every line FFmpeg emitted is visible there; the last few lines
usually explain what went wrong (missing codec, unsupported pixel
format, permission denied on the output path, and similar).

If the preview plays audio but not video on Linux, your Qt install is
probably missing a multimedia backend. Installing the `gstreamer1.0-
libav` package (Debian/Ubuntu) or `gst-libav` (Arch, Fedora) gives Qt
the codecs it needs without adding any Python dependencies.
