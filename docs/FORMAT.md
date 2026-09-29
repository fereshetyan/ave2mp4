# The `.ave` container format

This document describes what is inside an *Avigilon Unity Export* file, as
reverse engineered from real exports. It is the reference for the conversion
`ave2mp4` performs.

Everything below was established by inspecting the bytes of the files, so the
wording distinguishes what is **verified** (seen in the data) from what is
**assumed** (consistent with the data, but not proven).

## 1. Overview

A `.ave` file is not a normal media file. It is a custom ISO-BMFF-like
container: a flat list of boxes, each with the usual `size` (uint32, big
endian) and `type` (four ASCII characters) header. General purpose tools stop
at the very first box, because there is no `moov` and the major brand in
`ftyp` is `ave2` instead of a standard one.

A typical file looks like this:

```
avfs | ftyp | expc | (datp | rcfc)* | recc | arcc*
```

| box    | meaning                                                |
|--------|--------------------------------------------------------|
| `avfs` | file signature / header, always first, 24 bytes        |
| `ftyp` | brand box, major brand `ave2`                          |
| `expc` | export metadata (camera, tools, identifiers)           |
| `datp` | **video data**: one chunk of the elementary stream     |
| `rcfc` | **recording index** for the `datp` chunk before it     |
| `recc` | recording metadata / signature material                |
| `arcc` | additional record data (identity data, signatures)     |

The important boxes for video playback are `datp` (payload) and `rcfc`
(timing and frame index). Everything else can be ignored for the purpose of
conversion.

## 2. `avfs` and `ftyp`

```
0000000  00 00 00 18 61 76 66 73  35 a9 b5 1f 36 68 42 f5   ....avfs 5...6hB.
0000016  82 dc 00 73 eb 50 82 eb                             ...s.P..
```

`avfs` is 24 bytes: the box header plus 16 bytes that look constant for a
given export (observed identical across the chunks of one recording; the
meaning is unknown).

`ftyp` is 24 bytes with major brand `ave2`:

```
00 00 00 18 66 74 79 70 61 76 65 32 00 00 00 00 08 07 00 1a 68 b9 a9 40
            'f'  't'  'y'  'p'  'a'  'v'  'e'  '2'
```

## 3. `expc`

Contains an `exph` box followed by a `root` box holding a protobuf message
with printable strings. In the inspected exports they included the recording
identifier (`1.001885057f20`), the player version (`2.6.0.184(9875)`), the
camera model (`5.0-H3-BO1-IR`), the MAC address, the camera name (`CAM 12/5/3
w`) and the internal data path (`1.001885057f20.cam008`). This box is not
needed for conversion, but it is a good source of provenance information.

## 4. `datp` – the video payload

Each `datp` box holds one chunk of a plain **H.264 Annex-B elementary
stream** (H.265 is expected to work the same way, see §8). The payload starts
with a 24 byte chunk header that is byte-identical in every chunk of an
export:

```
00 00 00 00 00 00 00 01  00 00 00 00 00 00 ff e7  00 00 00 00 00 00 00 00
```

Twenty of these 24 bytes are zero, which makes the boundary between the
header and the first start code ambiguous: the last zero of the header can
just as well be read as the leading zero of a four byte start code, and the
decoded data is identical either way. `ave2mp4` therefore does not rely on
the exact boundary: it re-emits every NAL unit with a canonical four byte
start code and strips trailing zero padding (§7).

The stream itself follows immediately, starting with a three byte start code
and the first NAL unit (typically SPS `0x67`, PPS `0x68`, an SEI `0x06`, then
an IDR slice `0x65`).

Observed properties of the elementary stream in the inspected exports:

* One slice per picture, one SEI NAL unit in front of every picture, a key
  frame every 8 pictures, no B-frames.
* The SPS advertises a reorder delay even though the stream contains no
  B-frames, so a naive demuxer shifts the timestamps and hides the first
  pictures behind an edit list. `ave2mp4` detects the absence of B-frames and
  assigns timestamps by packet index instead.
* An export is cut in the middle of a picture, so the last NAL units of the
  file do not form a complete picture and are dropped.

## 5. `rcfc` – the recording index

One `rcfc` box follows each `datp` box and describes it:

```
rcfc
├── rcfh    14 bytes: the chunk number (uint48 in the observed files)
├── sdat    recording level metadata
└── tkfc*   one per track
    ├── tkfh  22 bytes
    └── sdat  track level metadata, including the frame index
```

`tkfh` (22 bytes), observed layout:

| offset | size | content                                        |
|--------|------|------------------------------------------------|
| 0      | 2    | 0                                              |
| 2      | 4    | track id written again (101 for the video)      |
| 6      | 8    | chunk start, nanoseconds since the Unix epoch   |
| 14     | 4    | running counter, equal to the end of the video  |
| 18     | 4    | chunk duration in 90 kHz ticks                  |

Every `sdat` box starts with four bytes of padding followed by a protobuf
message. Table of the fields seen in the video track message:

| field | wire | meaning (verified unless noted)                        |
|-------|------|--------------------------------------------------------|
| 1     | 0    | track id: `101` video, `201` a second track            |
| 2     | 0    | number of pictures in this chunk                        |
| 3     | 0    | index of the first picture of this chunk (cumulative)   |
| 4     | 1    | chunk start, fixed64 little endian nanoseconds, Unix    |
| 5     | 0    | 0 in all observed chunks *(meaning unknown)*            |
| 6     | 0    | chunk duration in 90 kHz ticks                          |
| 7     | 2    | per frame index message, see below                      |
| 8     | 0    | 1 in all observed chunks *(meaning unknown)*            |

The timestamps are nanoseconds since the Unix epoch; consecutive chunks of
the inspected files are exactly 30.24 s apart, which matches the durations in
field 6.

### Field 7 – the per frame index

| field | wire | meaning                                                |
|-------|------|--------------------------------------------------------|
| 1, 2  | 2    | pairs of ints, e.g. `{1,0} {125,2}` *(meaning unknown)* |
| 3     | 2    | one byte per picture: `0` = key frame, `2` = delta      |
| 4     | 2    | 299 bytes for 126 pictures *(unclear, looks compressed)* |
| 5     | 2    | 486 bytes for 126 pictures *(unknown)*                  |
| 8     | 2    | 788 bytes, variable length integers *(unknown)*         |
| 9     | 2    | 284 zero bytes in the inspected export *(unknown)*      |
| 10    | 2    | `{126, 0}` *(unknown)*                                  |
| 11    | 2    | 8 bytes per picture: two uint32, the second always 0    |

Field 3 is the one the converter does not need but which confirms the
structure: reading it for the inspected exports gives exactly the key frame
positions computed from the decoded stream. The other fields look like per
frame bookkeeping (sizes, hashes, seek tables).

## 6. Timing

Frame timing is described only by the index, and it is **constant per chunk**:
`field 6 / field 2` is the frame interval in 90 kHz ticks. In the inspected
exports the first chunks use 2 721 600 / 126 = 21 600 ticks, i.e. 240 ms per
frame, or 25/6 ≈ 4.1667 frames per second — a surveillance export sampled
from a higher native frame rate.

The final chunk of an export is usually cut short: in one inspected file it
reported 89 pictures but a duration of 88 intervals, because the recording
stopped in the middle of the last frame interval. `ave2mp4` uses the dominant
interval of the file and warns when the index is inconsistent.

## 7. Normalisation performed by `ave2mp4`

The container is ambiguous in two places, so the converter normalises instead
of guessing:

1. **Chunk header vs. start code.** The boundary cannot be determined from a
   single chunk. All NAL units are split out of the concatenated stream and
   re-emitted with four byte start codes, which removes the ambiguity
   completely. NAL unit payloads are never modified.
2. **Trailing zero padding.** Annex-B allows trailing zero bytes after a NAL
   unit, and chunk boundaries introduce more of them. They carry no
   information, and their number depends on where the header was assumed to
   end, so they are stripped to make the output deterministic.

The resulting NAL units, and therefore the decoded pictures, are identical to
the export.

## 8. What is not supported

* **Password protected exports.** Avigilon can encrypt an export. The bytes
  are then not a readable elementary stream and `ave2mp4` refuses the file
  instead of producing garbage.
* **Additional tracks.** The inspected exports contain a second track (id
  `201`) with no frames at all. A non empty second track is reported but not
  converted; audio would need the same treatment as the video.
* **Exports with B-frames.** This is a limitation of the converter rather than
  of the format. A reordered stream can only be converted losslessly if the
  presentation order of every picture is known, and ffmpeg's raw H.264
  demuxer does not derive it from the bitstream. `ave2mp4` therefore refuses
  such exports (see the `--reencode` option) instead of writing a file whose
  pictures are in the wrong order. Parsing the POC from the slice headers, or
  storing the presentation order while remuxing, would lift the restriction.
* **H.265 exports.** Avigilon cameras can record H.265. The converter detects
  the codec and asks ffmpeg for the matching demuxer, but no such export has
  been available for testing.
* **Variable frame rate.** Only in the sense that a file whose chunks disagree
  about the frame interval falls back to the average interval, which makes
  playback speed approximate.

## 9. Reproducing this analysis

The format is small enough to be explored with a few lines of Python. Dump
the box list, find a box whose payload starts with `00 00 00 01` or
`00 00 01`, and hand it to the H.264 parser of your choice:

```python
import struct

data = open("export.ave", "rb").read()
offset = 0
while offset < len(data):
    size, box_type = struct.unpack_from(">I4s", data, offset)
    print(offset, size, box_type)
    offset += size
```

`ave2mp4 --info export.ave` prints the same information at a higher level.
