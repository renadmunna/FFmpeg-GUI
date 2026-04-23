# bin/

This folder is where you drop the FFmpeg binaries when you want to
build a fully self-contained, portable copy of FFmpeg GUI. The folder is
otherwise empty on purpose: the binaries themselves are platform
specific and several tens of megabytes in size, so committing them into
version control would be both wasteful and incorrect (a Windows binary
committed here would confuse a Linux contributor who cloned the
repository expecting a Linux build).

## What to put here

You need two executables, both from the same FFmpeg release:

- `ffmpeg` (or `ffmpeg.exe` on Windows)
- `ffprobe` (or `ffprobe.exe` on Windows)

Static builds that bundle every dependency into a single file are the
easiest to work with because they will run on any reasonably modern
machine of the same operating system without needing extra shared
libraries installed. You can download signed static builds from the
official project page at <https://ffmpeg.org/download.html>, or from
one of the widely trusted third-party builders listed there.

## How the application finds them

The locator in `src/utils/ffmpeg_locator.py` searches in the following
order, returning the first pair it finds:

1. This `bin/` folder next to the application root.
2. The `FFMPEG_GUI_BIN` environment variable, treated as a directory
   path. Useful when you want to share a single copy of the binaries
   across multiple development checkouts.
3. The system `PATH`. This is the fallback, and is what most developer
   machines will use while they work on the application from source.

If you are packaging a portable distribution with PyInstaller, the
`build_portable.py` script copies this entire folder into the packaged
output using the `--add-data` flag, so the binaries you drop here end
up inside the final build automatically.

## Licensing note

FFmpeg is distributed under the LGPL or GPL depending on the build
options used. If you redistribute a copy of FFmpeg GUI that bundles
these binaries, make sure the FFmpeg license text is included alongside
your build so your users can see the terms under which the bundled code
is provided.
