# ave2mp4

[![tests](https://github.com/fereshetyan/ave2mp4/actions/workflows/tests.yml/badge.svg)](https://github.com/fereshetyan/ave2mp4/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-windows%20%7C%20linux%20%7C%20macOS-lightgrey.svg)](#requirements)
[![No re-encoding](https://img.shields.io/badge/video-remuxed%20losslessly-success.svg)](#fidelity)

**Convert Avigilon Unity Export (`.ave`) security footage to MP4 — without
re-encoding, without the Avigilon Player.**

`.ave` is Avigilon's native export format. There is no online converter for
it, and the official way to get an MP4 out of an export is to install the
Avigilon Unity Player — a Windows-only application — open the file and export
it again. This tool does the same job in one command, because the video inside
the container is already a plain H.264 stream that only has to be rewrapped.

## Download

**[ave2mp4.exe](https://github.com/fereshetyan/ave2mp4/releases/latest)** is a
standalone Windows build with ffmpeg inside it. No Python, no ffmpeg, no
command line: download it, then drag an `.ave` file onto it, or run
`ave2mp4.exe "recording.ave"`. On Linux and macOS, use the script below.

```
$ ave2mp4 "recording.ave"
recording.ave
  ffmpeg       : /usr/bin/ffmpeg
  codec        : h264
  frame rate   : 4.166666666666667 fps (dominant interval; the index disagrees for some chunks (the last one is usually cut short))
  pictures     : 467 in the stream, 467 in the index
  note         : no B-frames, ignoring the bogus reorder delay advertised in the bitstream
  note         : dropping 216 trailing bytes (0.0% of the stream) that do not form a picture
  written      : recording.mp4
                 2592x1944, 467 frames, 112.08 s
```

Every step of that run is described further down: what was checked, what was
assumed, and what would have made the tool stop instead.

## Contents

- [Download](#download)
- [Requirements](#requirements)
- [Installation](#installation)
- [Usage](#usage)
- [What is supported](#what-is-supported)
- [How it works](#how-it-works)
- [Timestamps and frame rate](#timestamps-and-frame-rate)
- [Fidelity and verification](#fidelity-and-verification)
- [Memory, disk and time](#memory-disk-and-time)
- [Limitations](#limitations)
- [Before you use this for legal or insurance purposes](#before-you-use-this-for-legal-or-insurance-purposes)
- [FAQ](#faq)
- [Contributing](#contributing)
- [License](#license)

## Requirements

* **Python 3.8 or newer** — the converter imports nothing outside the standard
  library.
* **ffmpeg 5.0 or newer** — used for the final rewrapping. The tool asks the
  binary it found whether it has the `setts` bitstream filter and refuses an
  older one up front rather than after a conversion has already failed. The
  standalone Windows build ships its own ffmpeg; otherwise one is looked up in
  this order: `--ffmpeg PATH`, the folder of a frozen build, `PATH`, the
  [`imageio-ffmpeg`](https://pypi.org/project/imageio-ffmpeg/) wheel, and the
  usual Windows install locations.
* **ffprobe** is **optional**, and unlike in earlier versions it is not what
  decides whether the conversion succeeded. It is used to check the finished
  MP4 and to print its dimensions; without it the output is still validated,
  just with a weaker check. See
  [verification](#fidelity-and-verification).

```bash
# Fedora / RHEL
sudo dnf install ffmpeg
# Debian / Ubuntu
sudo apt install ffmpeg
# macOS
brew install ffmpeg
# Windows, then reopen the terminal
winget install Gyan.FFmpeg
# or let pip bring a private ffmpeg along with the script
pip install imageio-ffmpeg
```

## Installation

The whole converter is a single file, so pick whatever suits you:

```bash
# Windows, no Python and no ffmpeg: download ave2mp4.exe
#   https://github.com/fereshetyan/ave2mp4/releases/latest
#   and then:  ave2mp4.exe "recording.ave"

# Option 1: clone the repository
git clone https://github.com/fereshetyan/ave2mp4.git
python3 ave2mp4/ave2mp4.py --help

# Option 2: copy it into your PATH (Linux and macOS)
curl -fsSL https://raw.githubusercontent.com/fereshetyan/ave2mp4/main/ave2mp4.py \
  -o ~/.local/bin/ave2mp4
chmod +x ~/.local/bin/ave2mp4
ave2mp4 --help
```

## Usage

```
usage: ave2mp4 [-h] [-o DIR] [-f] [-i] [--dump-stream PATH] [-q] [--reencode]
               [--verify {none,cheap,full}] [--temp-dir DIR] [--ffmpeg PATH]
               [--version]
               [FILE ...]
```

| Task | Command |
|------|---------|
| Convert one file | `ave2mp4 recording.ave` |
| Convert everything in a folder | `ave2mp4 *.ave` |
| Put the MP4s somewhere else | `ave2mp4 *.ave -o converted/` |
| Overwrite existing MP4s | `ave2mp4 *.ave --force` |
| Inspect without converting | `ave2mp4 --info recording.ave` |
| Transcode an export that uses B-frames | `ave2mp4 recording.ave --reencode` |
| Check every picture of the result | `ave2mp4 recording.ave --verify full` |
| Use a specific ffmpeg | `ave2mp4 recording.ave --ffmpeg /opt/ffmpeg/bin/ffmpeg` |
| Put the scratch file on a real disk | `ave2mp4 recording.ave --temp-dir /var/tmp` |

**Overwrite policy.** The `.ave` file is only ever read, never modified. An
MP4 that already exists is **not** replaced unless you pass `--force`, and
`--dump-stream` follows the same rule. With `--force` the new file is built
under a temporary name in the destination directory and only replaces the old
one once it has been validated, so a failure never leaves a half-written file
where a good MP4 used to be.

`--info` prints what the export contains and touches nothing: no output
directory is created, no file is written.

## What is supported

| | Status |
|---|---|
| H.264, no B-frames, one video track | Supported, lossless copy |
| H.264 with B-frames | Refused; `--reencode` transcodes instead |
| H.265, no reordering, one video track | Supported, lossless copy |
| H.265 with reordered pictures | Refused; `--reencode` transcodes instead |
| H.265 where the picture order cannot be read | Refused rather than guessed |
| A second track that holds no video | Reported, not converted |
| A second track that holds non-video data | Reported, not converted |
| Several tracks that all hold a video stream | Refused: nothing says which is the video |
| One chunk holding two tracks at once | Refused: the data cannot be separated |
| Audio | Not converted (see [Limitations](#limitations)) |
| Password protected exports | Detected and refused |
| Encrypted or corrupt exports | Refused with a diagnostic, never converted partially |

"Supported" means the elementary stream inside the export is copied into the
MP4 byte for byte, with the timing that the recording index describes. It does
not mean every export from every recorder is covered — see
[Limitations](#limitations) and [Contributing](#contributing).

## How it works

The `.ave` container is a flat list of ISO-BMFF-like boxes. The video is not
wrapped in a normal MP4 track structure at all: the `datp` boxes contain an
undecorated H.264 Annex-B elementary stream, and the `rcfc` boxes contain a
protobuf index that describes how many pictures each chunk holds, how long it
is and when it started.

So the conversion is:

1. Walk the box list and read the recording index. Each `datp` box is
   attributed to the track the index behind it names; only the track whose
   data is a video stream is used, and anything unclear is an error rather
   than a guess.
2. Read the video data in windows of one mebibyte, split it into NAL units
   across the window boundaries, and analyse it in the same pass: the codec,
   the picture count, whether the pictures are reordered, and where the
   trailing fragment of a cut frame begins.
3. Write the normalised stream to a temporary file, hand it to
   `ffmpeg -c copy`, check what came out, and only then move it onto the
   destination.

No pixel is decoded and no pixel is re-encoded. The frame rate cannot be read
by `ffprobe` from the export, which is why `ffmpeg -i file.ave` on its own
fails with `moov atom not found`.

The reverse engineered layout, including the exact protobuf fields, is
documented in **[docs/FORMAT.md](docs/FORMAT.md)**.

## Timestamps and frame rate

The timestamps are **reconstructed from the recording index, not preserved
from the container** — the container has no MP4-style timing for the video
track, only a frame count and a duration per chunk.

* The frame interval comes from field 6 of the index (90 kHz ticks per
  picture). When every chunk agrees, that constant is used. When the last
  chunk is cut short, the dominant interval is used and the disagreement is
  reported. When the chunks disagree more widely, the average is used and
  playback speed is approximate.
* Presentation times are then `n × interval`, and decode times are set equal
  to them by the `setts` bitstream filter. The first picture is at
  presentation time 0 — there is no edit list hiding it.
* The container's own start timestamp is read and reported by `--info`, but it
  is **not** written into the MP4. If you need the wall-clock time of a
  recording, read it from the burned-in overlay or from `--info`.
* Per-frame timing is not reconstructed. A recording whose chunks disagree
  about the interval is played back at the average rate, with a warning.
* The start codes, trailing zero padding and the trailing fragment of a frame
  that the export cut in half are the only bytes that change; see
  [docs/FORMAT.md §7](docs/FORMAT.md#7-normalisation-performed-by-ave2mp4).

The test suite checks these invariants directly: the first presentation
timestamp is 0, both timestamps increase strictly, and the interval equals the
one the index describes.

## Fidelity and verification

A copy is lossless in the sense that matters here: the elementary stream in
the MP4 is the elementary stream from the export, NAL unit for NAL unit. The
test suite compares the two against a stream that was built independently of
the converter, not against the converter's own output.

What is **not** claimed: that the pictures are identical to the camera's, and
that nothing about the export's provenance survives (see
[legal use](#before-you-use-this-for-legal-or-insurance-purposes)).

Before the new file replaces the destination, `--verify` decides how much is
checked:

| `--verify` | What is checked | Cost on the reference export |
|---|---|---|
| `none` | the file is non-empty and contains `ftyp`, `mdat` and `moov` boxes | 0.0 s |
| `cheap` (default) | the above, plus: ffprobe reads it, there is a video stream, it has a picture size, and it does not hold **fewer** pictures than were extracted from the export | 0.08 s |
| `full` | the above, plus: every picture is decoded, the decoded count has to equal the extracted count exactly, and decoder errors fail the conversion | 7.4 s |

The reference export is a 57.7 MB H.264 export of 467 pictures at 2592×1944;
the times are for the verification step alone, on the machine described in
[Memory, disk and time](#memory-disk-and-time). `full` is roughly fifty times
more expensive than `cheap`, which is why it is not the default. Use it when
you want the strongest available statement that the result is decodable from
end to end.

If a check fails, the temporary file is deleted and the previous MP4 is left
untouched, so a failed run never replaces a good file with a bad one.

`--verify full` is the only mode that decodes anything. `cheap` and `none` read
container metadata only: they prove the file is a complete, well-formed MP4
with a plausible number of pictures, not that every picture decodes.

## Memory, disk and time

Measured on the reference export — 57.7 MB, 467 pictures, 2592×1944 — with
Python 3.14.7 and ffmpeg 8.1.3 on a 13th generation Intel Core i5-1334U
(Fedora, 12 threads), taking the best of three runs:

| Stage | Time | Peak resident memory |
|---|---|---|
| Parse and extract the elementary stream | 0.09 s | 21 MB |
| `ffmpeg -c copy` remux | 0.13 s | 74 MB |
| `--verify none` (whole conversion) | 0.32 s | 74 MB |
| `--verify cheap` (whole conversion) | 0.40 s | 74 MB |
| `--verify full` (whole conversion) | 7.8 s | 95 MB |

So a conversion of that export takes about half a second end to end with the
default verification, which is roughly 140 MB of input per second on this
machine. These are single measurements on one machine and one file; treat them
as a reference point, not as a guarantee. The peak memory of the converter
itself does **not** grow with the size of the export: it is set by the window
size (1 MiB) and by the interpreter. ffmpeg's own footprint is what you see in
the last column.

Disk: the export is read in place, and one temporary copy of the elementary
stream is written — the same size as the video inside the export. **On Linux
the system temporary directory is often a RAM-backed `tmpfs`**, so a large
export can consume that much RAM. Point `--temp-dir` at a real directory if
`/tmp` is small. The temporary file is removed when the conversion ends, and
the file that is being replaced is only unlinked at the very last moment.

## Limitations

* **Exports that use B-frames are refused by default.** A reordered stream can
  only be stored correctly if its display order is known, and ffmpeg's raw
  H.264 and H.265 demuxers do not derive it — copying one blindly shuffles the
  pictures. `ave2mp4` decides this from the bitstream, not from a single NAL
  header, and stops instead of writing a subtly wrong file:
  * H.264: any slice whose `slice_type` is a B slice.
  * H.265: the picture order counts read out of the slice headers. The order
    the pictures are shown in is the order of their picture order counts, so a
    decode order whose counts only ever step forwards is the display order; any
    other step means the display order has to be told to ffmpeg separately.
    The `slice_type` field cannot decide this — H.265 calls a plain P picture a
    "B slice" — which is why the picture order counts are parsed instead, from
    the SPS and PPS that the export has to contain. If they cannot be read, the
    order is reported as *unknown* and the export is refused.
  * If you need such an export anyway, `--reencode` transcodes it. The
    pictures come out in the right order, but the copy is no longer bit exact.
* **Audio is not converted.** If the export has more than one track and only
  one of them holds a video stream, the other is named in the output and left
  alone. If more than one track holds a video stream, nothing says which one the
  export is about, so the export is refused instead of picking one.
* **H.265 exports have not been tested against a real camera.** Codec
  detection, reordering detection, the safe path and the refused path are all
  covered by the test suite using real libx265 output, but no H.265 export from
  an Avigilon recorder was available. If you have one, please send the output of
  `--info`.
* **Password protected exports are not supported.** Encrypted exports are
  detected and rejected rather than mangled.
* **One export is not a specification.** The format was reconstructed from real
  exports of one Avigilon Unity version. Boxes and index fields that differ in
  your version will show up as a clear error, not as corrupted video — but a fix
  needs a sample. See [Contributing](#contributing).
* **Variable frame rate** is not reconstructed per frame; if the chunks of a
  recording disagree about the frame interval, the average is used and a
  warning is printed.
* **Windows replacement semantics.** The new MP4 is moved onto the destination
  with an atomic rename where the filesystem supports it. On Windows that
  rename fails if another program has the destination open — a player that is
  still using the file, for instance — and the error says so; the tool does not
  fall back to deleting the destination first, because that is what used to
  destroy a good file.

## Before you use this for legal or insurance purposes

Two things are worth knowing before you hand an MP4 to a court, an insurer or
a police department:

1. **The export's digital signature does not survive the conversion.** The
   `.ave` container carries integrity and identity data in the `recc` and
   `arcc` boxes. None of that exists in an MP4, so the converted file is a
   normal video file with no evidentiary chain of custody attached. Keep the
   original `.ave` files and, if authenticity may be challenged, ask the
   receiving party whether they need the native format as well. Converting
   does not destroy the original — this tool only reads it.
2. **Check that the visible timestamp survived.** Avigilon can burn
   overlays — timestamp, device name, location — into the exported pixels.
   If the export was made with those overlays, they are part of the video and
   they are preserved. If it was exported without them, the MP4 will not have
   them either, because the information is not in the recorded video stream.
   The container's own start timestamp is not carried into the MP4 either; run
   `--info` to see it.

## FAQ

<details>
<summary>Why does ffmpeg refuse to open my .ave file?</summary>

Because the container is proprietary. `ffprobe` reports `moov atom not found`:
the file has ISO-BMFF style boxes and an `ftyp` brand of `ave2`, but the video
is stored outside of any MP4 track structure. Parsing the container first is
unavoidable.

</details>

<details>
<summary>Is the video re-encoded? Will the quality drop?</summary>

No, unless you pass `--reencode`. The elementary stream is copied into the new
container without being decoded. Quality, frame count, resolution and key frame
placement are exactly as recorded.

</details>

<details>
<summary>Can I convert the MP4 back into an .ave file?</summary>

No. The index, the signature material and the original chunking are not
reconstructed by this tool. Keep the export.

</details>

<details>
<summary>The video plays at about 4 frames per second. Is that a bug?</summary>

No. Many exports are sampled at a reduced image rate, and the container
records that rate explicitly. `--info` shows the interval taken from the index
— in the tested exports one picture every 240 ms. Playback matches real time;
the footage simply is not smooth.

</details>

<details>
<summary>Does this work on Windows and macOS?</summary>

The container parsing is pure Python with no platform specific code, and the
continuous integration runs the whole test suite on `windows-latest`,
`macos-latest` and Ubuntu, so those platforms are covered by CI on every
commit. The results quoted in this README were all produced on Linux; the
Windows and macOS results in CI are the evidence for those platforms, not a
local measurement. Two things have never been exercised by a test at all: the
drag-and-drop behaviour and the console window that the frozen `.exe` keeps
open, since they need a human double-clicking a file in a Windows Explorer
window.

On Windows the easiest route is the standalone
[`ave2mp4.exe`](#download), which needs neither Python nor ffmpeg.

</details>

<details>
<summary>Windows says "Windows protected your PC" when I start ave2mp4.exe</summary>

That is SmartScreen complaining about an unsigned executable that few people
have downloaded yet, not an antivirus finding. Choose *More info* → *Run
anyway*, or run it from a terminal. If you prefer not to trust a prebuilt
binary, build it yourself in one command:

```powershell
pip install pyinstaller imageio-ffmpeg
python -m PyInstaller --onefile --name ave2mp4 --add-binary "ffmpeg.exe;." ave2mp4.py
```

The release build additionally checks that the bundled ffmpeg has the `setts`
bitstream filter and smoke tests the packaged executable before publishing it,
so a build with too old an ffmpeg inside it fails rather than shipping.

</details>

<details>
<summary>It says "ffmpeg was not found"</summary>

Install it (`winget install Gyan.FFmpeg` on Windows, `brew install ffmpeg` on
macOS, `apt install ffmpeg` on Debian and Ubuntu) and reopen the terminal, or
run `pip install imageio-ffmpeg` to get a private copy, or point the program at
an existing binary with `--ffmpeg PATH`. The standalone Windows `.exe` already
contains one.

</details>

<details>
<summary>It says my ffmpeg is too old</summary>

The copy path needs the `setts` bitstream filter, which gives every copied
picture its own timestamp, and that arrived in ffmpeg 5.0. Install a current
ffmpeg, or use the standalone `.exe`, which bundles one. The check happens
before any of your footage is read.

</details>

<details>
<summary>The exe does not print width, height and frame count</summary>

`ffprobe` is what reports the dimensions of a finished MP4, and the standalone
Windows build bundles `ffmpeg` only. The conversion is complete either way: the
output is still validated before it replaces the destination, and the summary
line reports the frame count and duration promised by the recording index and
says that is where they came from.

</details>

<details>
<summary>Is the conversion slow?</summary>

No, and it does not depend on the resolution: not a single pixel is decoded.
The work is proportional to the size of the file — about 140 MB per second on
the machine measured in
[Memory, disk and time](#memory-disk-and-time), with the default verification.
The one exception is `--verify full`, which decodes every picture to prove the
result is intact and is roughly fifty times slower; it is not the default.

</details>

## Contributing

The most valuable contribution is a **sample export** from a different
Avigilon version, codec or recorder configuration — especially H.265, audio, or
an export whose `--info` output looks different from the example above.

### Checking a real export

`--info` on its own is a good check: if it prints a codec, a picture count
that matches the index, and a frame rate, the file is understood. To run the
full conversion against a real export, point the test suite at it — the file
is only read, and nothing is uploaded anywhere:

```bash
AVE2MP4_REAL_SAMPLE="recording.ave" python3 tests/test_ave2mp4.py
```

That test is skipped when the variable is not set. It converts the export,
checks that the MP4 really holds the pictures the export contains, and prints
the usual log. Run it with `--verify full` (`AVE2MP4_REAL_SAMPLE=... python3
ave2mp4.py --verify full recording.ave`) to have every picture decoded as
well.

When reporting a problem, include the `--info` output and the exact command
line. Never upload real footage to a public issue tracker; the `--info` output
contains no picture data and is enough to diagnose a parsing problem.

## License

[MIT](LICENSE) — do whatever you want with it, no warranty.
