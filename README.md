# ave2mp4

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-linux%20%7C%20macOS%20%7C%20windows-lightgrey.svg)](#requirements)
[![No re-encoding](https://img.shields.io/badge/video-remuxed%20losslessly-success.svg)](#fidelity)

**Convert Avigilon Unity Export (`.ave`) security footage to MP4 — without
re-encoding, without Windows, without the Avigilon Player.**

`.ave` is Avigilon's native export format. There is no online converter for
it, and the official way to get an MP4 out of an export is to install the
Avigilon Unity Player — a Windows-only application — open the file and export
it again. This tool does the same job in one command, on any platform, in
about a second per hour of footage, because the video inside the container is
already a plain H.264 stream that only has to be rewrapped.

```
$ ave2mp4 "Avigilon Unity Export-2026-09-29 08.54.07.485 AM.ave"
Avigilon Unity Export-2026-09-29 08.54.07.485 AM.ave
  codec        : h264
  frame rate   : 4.166666666666667 fps (dominant interval; the index disagrees for some chunks (the last one is usually cut short))
  pictures     : 467 in the stream, 467 in the index
  note         : no B-frames, ignoring the bogus reorder delay advertised in the bitstream
  note         : dropping 216 trailing bytes (0.0% of the stream) that do not form a picture
  written      : Avigilon Unity Export-2026-09-29 08.54.07.485 AM.mp4
                 2592x1944, 467 frames, 112.08 s
```

## Contents

- [Requirements](#requirements)
- [Installation](#installation)
- [Usage](#usage)
- [How it works](#how-it-works)
- [Fidelity](#fidelity)
- [Limitations](#limitations)
- [Before you use this for legal or insurance purposes](#before-you-use-this-for-legal-or-insurance-purposes)
- [FAQ](#faq)
- [Contributing](#contributing)
- [License](#license)

## Requirements

* **Python 3.8 or newer** — the converter itself has no third-party
  dependencies, everything else is in the standard library.
* **ffmpeg** (with `ffprobe`) — used for the final rewrapping. It is a hard
  requirement; the container parsing is pure Python.

```bash
# Fedora
sudo dnf install ffmpeg
# Debian / Ubuntu
sudo apt install ffmpeg
# macOS
brew install ffmpeg
# Windows
winget install Gyan.FFmpeg
```

## Installation

The whole converter is a single file, so pick whatever suits you:

```bash
# Option 1: clone the repository
git clone https://github.com/fereshetyan/ave2mp4.git
~/ave2mp4/ave2mp4.py --help

# Option 2: copy it into your PATH
curl -fsSL https://raw.githubusercontent.com/fereshetyan/ave2mp4/main/ave2mp4.py \
  -o ~/.local/bin/ave2mp4
chmod +x ~/.local/bin/ave2mp4
ave2mp4 --help
```

## Usage

```
usage: ave2mp4 [-h] [-o DIR] [-f] [-i] [--dump-stream PATH] [-q] [--version] [FILE ...]
```

| Task | Command |
|------|---------|
| Convert one file | `ave2mp4 recording.ave` |
| Convert everything in a folder | `ave2mp4 *.ave` |
| Put the MP4s somewhere else | `ave2mp4 *.ave -o converted/` |
| Overwrite existing MP4s | `ave2mp4 *.ave --force` |
| Inspect without converting | `ave2mp4 --info recording.ave` |
| Transcode an export that uses B-frames | `ave2mp4 recording.ave --reencode` |

The MP4 is written next to the `.ave` file with the same name, and the `.ave`
file is never modified. Existing MP4s are **not** overwritten unless you pass
`--force`.

`--info` prints what the export contains without touching ffmpeg:

```
file            : recording.ave
size            : 57.8 MB
codec           : h264
pictures        : 467 (index says 467)
B-frames        : no
frame rate      : 4.166666666666667 fps (dominant interval; ...)
chunk header    : 24 bytes
starts at       : 2026-09-23 04:31:11.083 (UTC)
duration        : 10065600 ticks (111.840 s)
tracks          :
  [0] id=101  frames=126   first=0     duration=2721600 ticks (30.240 s)  (primary video)
  [1] id=201  frames=0     first=0     duration=0 ticks (0.000 s)  (unused)
  ...
```

## How it works

The `.ave` container is a flat list of ISO-BMFF-like boxes. The video is not
wrapped in a normal MP4 track structure at all: the `datp` boxes contain an
undecorated H.264 Annex-B elementary stream, and the `rcfc` boxes contain a
protobuf index that describes how many pictures each chunk holds, how long it
is and when it started.

So the conversion is:

1. Walk the box list, collect the video payloads and the index entries.
2. Split the payloads into NAL units and drop the trailing fragment that does
   not form a complete picture (exports are cut mid-frame).
3. Take the frame interval from the index — not from a guess — and hand the
   stream to `ffmpeg -c copy`.

No pixel is decoded and no pixel is re-encoded. The frame rate cannot be read
by `ffprobe` from the export, which is why `ffmpeg -i file.ave` on its own
fails with `moov atom not found`.

The reverse engineered layout, including the exact protobuf fields, is
documented in **[docs/FORMAT.md](docs/FORMAT.md)**.

## Fidelity

The conversion is lossless, and this is checked, not assumed:

* Every NAL unit of the output was compared byte by byte against the units in
  the export — identical, including the key frames and the SEI metadata units.
* Every decoded picture was hashed (`framemd5`) on both sides — identical for
  all frames.
* The presentation order is verified with a stream in which every picture is a
  solid colour that encodes its own number, so a shuffled file cannot pass the
  test suite unnoticed.
* The only bytes that do not survive are the trailing fragment of the frame
  that the export itself cut in half, and untagged zero padding at chunk
  boundaries (see [docs/FORMAT.md §7](docs/FORMAT.md#7-normalisation-performed-by-ave2mp4)).

The test suite in [`tests/`](tests/) builds a synthetic `.ave` file from
scratch, so the round trip is verified on every commit without shipping anyone's
footage.

## Limitations

* **Exports that use B-frames are refused by default.** A reordered stream can
  only be stored correctly if its presentation order is known, and ffmpeg
  cannot derive that from a raw H.264 stream — copying one blindly shuffles
  the pictures. `ave2mp4` therefore detects B-frames and stops instead of
  writing a subtly wrong file. If you need such an export anyway, `--reencode`
  transcodes it; the pictures come out in the right order, but the copy is no
  longer bit exact. None of the exports inspected so far used B-frames.
* **Audio is not converted.** The exports seen so far contain no audio track
  at all (`--info` shows an empty second track). If your export does contain
  audio, the tool will print a warning instead of staying silent about it.
* **Password protected exports are not supported.** Encrypted exports are
  detected and rejected rather than mangled.
* **H.265 exports are untested.** The codec is detected and forwarded, but no
  such export was available while writing this. If you have one, please send
  the output of `--info`.
* **One export is not a specification.** The format was reconstructed from
  real exports of one Avigilon Unity version. Boxes and index fields that
  differ in your version will show up as a clear error, not as corrupted
  video — but a fix needs a sample. See [Contributing](#contributing).
* **Variable frame rate** is not reconstructed per frame; if the chunks of a
  recording disagree about the frame interval, the average is used and a
  warning is printed.

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

No. The H.264 stream is copied verbatim into a new container. Quality, frame
count, resolution and key frame placement are exactly as recorded.

</details>

<details>
<summary>Can I convert the MP4 back into an .ave file?</summary>

No. The index, the signature material and the original chunking are not
reconstructed by this tool. Keep the export.

</details>

<details>
<summary>The video plays at about 4 frames per second. Is that a bug?</summary>

No. Many exports are sampled at a reduced image rate, and the container
records that rate explicitly. `--info` shows the interval taken from the
index — in the tested exports one picture every 240 ms. Playback matches real
time; the footage simply is not smooth.

</details>

<details>
<summary>Does this work on Windows and macOS?</summary>

The container parsing is pure Python and the rest is a call to ffmpeg, so yes —
as long as ffmpeg is installed. The converter was developed and tested on
Linux.

</details>

## Contributing

The most valuable contribution is a **sample export** from a different
Avigilon version, codec or recorder configuration — especially H.265, audio,
or an export whose `--info` output looks different from the example above.
Even the `--info` output alone is useful, and you can redact the footage.

Bug reports should include the `--info` output and the exact command line.
Never upload real footage to a public issue tracker.

## Disclaimer

This is an unofficial, community tool. It is not affiliated with, endorsed by
or supported by Motorola Solutions or Avigilon. The file format was
reconstructed from publicly available exports for interoperability, and the
tool only ever reads the files it is given.

## License

[MIT](LICENSE) — do whatever you want with it, no warranty.
