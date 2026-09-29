#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build a synthetic Avigilon export for testing.

The real container is proprietary and nobody wants to publish their security
footage, so this module writes the small subset of the format that ave2mp4
reads.  It is deliberately independent from ave2mp4 itself: if the parsing in
the converter breaks, these samples still describe the same bytes.

Standalone use:

    python3 tests/make_sample.py sample.ave
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
import tempfile

#: The 24 byte header that precedes the stream in every ``datp`` box.
CHUNK_HEADER = bytes.fromhex(
    "0000000000000001" "000000000000ffe7" "0000000000000000"
)

#: Constant bytes observed in the ``avfs`` box of real exports.
AVFS_PAYLOAD = bytes.fromhex("35a9b51f366842f582dc0073eb5082eb")

#: ``ftyp`` payload: major brand "ave2", minor version, compatible brands.
FTYP_PAYLOAD = b"ave2" + bytes.fromhex("00000000" "0807001a68b9a940")

TICKS_PER_SECOND = 90000

#: Track ids used by Avigilon: video, and a second track that is unused in
#: every export inspected so far.
VIDEO_TRACK_ID = 101
OTHER_TRACK_ID = 201

#: Frame interval of the generated samples: 240 ms, as in real exports.
DEFAULT_INTERVAL_TICKS = 21600


# --------------------------------------------------------------------------
# protobuf writing
# --------------------------------------------------------------------------


def varint(value):
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def tag(field, wire):
    return varint((field << 3) | wire)


def pb_varint(field, value):
    return tag(field, 0) + varint(value)


def pb_bytes(field, payload):
    return tag(field, 2) + varint(len(payload)) + payload


def pb_fixed64(field, value):
    return tag(field, 1) + struct.pack("<Q", value)


# --------------------------------------------------------------------------
# box writing
# --------------------------------------------------------------------------


def box(box_type, payload):
    return struct.pack(">I4s", 8 + len(payload), box_type) + payload


def sdat(protobuf):
    """A ``sdat`` box: four padding bytes, then the protobuf message."""
    return box(b"sdat", b"\x00\x00\x00\x00" + protobuf)


def track_sdat(track_id, frames, first_frame, timestamp_ns, duration_ticks):
    """The per track index message, see docs/FORMAT.md section 5."""
    if frames:
        # One byte per picture: a key frame every eighth picture.
        picture_types = bytes(
            0 if index % 8 == 0 else 2 for index in range(frames)
        )
        per_frame = pb_bytes(3, picture_types)
    else:
        per_frame = b""
    return sdat(
        pb_varint(1, track_id)
        + pb_varint(2, frames)
        + pb_varint(3, first_frame)
        + pb_fixed64(4, timestamp_ns)
        + pb_varint(5, 0)
        + pb_varint(6, duration_ticks)
        + per_frame
        + pb_varint(8, 1)
    )


def tkfh(track_id, timestamp_ns, duration_ticks):
    """The fixed size track header (22 bytes of payload)."""
    payload = (
        struct.pack(">H", 0)
        + struct.pack(">I", track_id)
        + struct.pack(">Q", timestamp_ns)
        + struct.pack(">I", duration_ticks)
        + struct.pack(">I", duration_ticks)
    )
    return box(b"tkfh", payload)


def rcfc(chunk_number, timestamp_ns, clips):
    """The index box for one chunk; ``clips`` is a list of track messages."""
    payload = box(b"rcfh", struct.pack(">Q", chunk_number)[2:])
    payload += sdat(pb_varint(1, 1))
    for track_id, frames, first_frame, duration_ticks in clips:
        header = tkfh(track_id, timestamp_ns, duration_ticks)
        body = track_sdat(
            track_id, frames, first_frame, timestamp_ns, duration_ticks
        )
        payload += box(b"tkfc", header + body)
    return box(b"rcfc", payload)


# --------------------------------------------------------------------------
# elementary stream generation
# --------------------------------------------------------------------------


def encode_clip(frames, width, height, rate, bframes, directory, index):
    """Encode a throwaway H.264 clip with ffmpeg and return its bytes."""
    path = os.path.join(directory, "clip%d.h264" % index)
    command = [
        "ffmpeg", "-hide_banner", "-v", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=%dx%d:rate=%s" % (width, height, rate),
        "-frames:v", str(frames),
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-x264-params", "keyint=8:min-keyint=8:scenecut=0",
        "-bf", "2" if bframes else "0",
        "-f", "h264", path,
    ]
    subprocess.run(command, check=True)
    with open(path, "rb") as handle:
        return handle.read()


def _split_nals(stream):
    """Local Annex-B splitter; kept separate from ave2mp4 on purpose."""
    positions = []
    pos = 0
    total = len(stream)
    while pos + 3 <= total:
        if stream[pos] == 0 and stream[pos + 1] == 0:
            if stream[pos + 2] == 1:
                positions.append((pos, 3))
                pos += 3
                continue
            if pos + 4 <= total and stream[pos + 2] == 0 and stream[pos + 3] == 1:
                positions.append((pos, 4))
                pos += 4
                continue
        pos += 1
    nals = []
    for index, (start, header_len) in enumerate(positions):
        end = positions[index + 1][0] if index + 1 < len(positions) else total
        nals.append((start, stream[start + header_len:end]))
    return nals


def picture_cut_positions(stream, boundaries):
    """Byte offsets where a chunk may start, one per requested boundary.

    A chunk is a storage boundary, not an encoder boundary: the recorder cuts
    the stream between two pictures and the chunks are reassembled before
    decoding.  The samples reproduce that instead of concatenating unrelated
    encodes.
    """
    picture_starts = [
        start for start, nal in _split_nals(stream)
        if nal and (nal[0] & 0x1F) in (1, 5)
    ]
    cuts = []
    for boundary in boundaries:
        if not 0 < boundary < len(picture_starts):
            raise ValueError("boundary %d is outside the stream" % boundary)
        cuts.append(picture_starts[boundary])
    return cuts


# --------------------------------------------------------------------------
# sample builder
# --------------------------------------------------------------------------


def wrap(path, stream, chunk_frames=(40, 25),
         interval_ticks=DEFAULT_INTERVAL_TICKS, width=320, height=240,
         bframes=False, start_timestamp_ns=1758600000_000_000_000):
    """Write a synthetic export around an already encoded elementary stream.

    ``chunk_frames`` are the desired chunk sizes; the real cut points are
    snapped to picture boundaries and reported back, exactly like a recorder
    that splits its storage without restarting the encoder.
    """
    blob = bytearray()
    blob += box(b"avfs", AVFS_PAYLOAD)
    blob += box(b"ftyp", FTYP_PAYLOAD)
    # A stand in for the metadata box; ave2mp4 ignores its contents.
    blob += box(b"expc", box(b"exph", bytes(28)) + box(b"root", bytes(32)))

    boundaries = []
    running = 0
    for frames in chunk_frames[:-1]:
        running += frames
        boundaries.append(running)
    cuts = picture_cut_positions(stream, boundaries)
    pieces = []
    previous = 0
    for cut in cuts:
        pieces.append(stream[previous:cut])
        previous = cut
    pieces.append(stream[previous:])

    first_frame = 0
    timestamp_ns = start_timestamp_ns
    real_chunk_frames = []
    for index, piece in enumerate(pieces):
        frames = 0
        for _start, nal in _split_nals(piece):
            if nal and (nal[0] & 0x1F) in (1, 5):
                frames += 1
        duration_ticks = frames * interval_ticks
        blob += box(b"datp", CHUNK_HEADER + piece)
        blob += rcfc(
            index + 1,
            timestamp_ns,
            [
                (VIDEO_TRACK_ID, frames, first_frame, duration_ticks),
                (OTHER_TRACK_ID, 0, 0, 0),
            ],
        )
        real_chunk_frames.append(frames)
        first_frame += frames
        timestamp_ns += duration_ticks * (1_000_000_000 // TICKS_PER_SECOND)

    # Trailing boxes of unknown content; the converter must ignore them.
    blob += box(b"recc", bytes(64))
    blob += box(b"arcc", bytes(128))

    with open(path, "wb") as handle:
        handle.write(bytes(blob))

    total_frames = sum(real_chunk_frames)
    return {
        "path": path,
        "frames": total_frames,
        "chunk_frames": real_chunk_frames,
        "interval_ticks": interval_ticks,
        "duration": total_frames * interval_ticks / TICKS_PER_SECOND,
        "rate": TICKS_PER_SECOND / interval_ticks,
        "width": width,
        "height": height,
        "bframes": bframes,
    }


def build(path, chunk_frames=(40, 25), interval_ticks=DEFAULT_INTERVAL_TICKS,
          width=320, height=240, rate="25", bframes=False,
          start_timestamp_ns=1758600000_000_000_000):
    """Encode a clip with ffmpeg, wrap it into an export, describe it."""
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required to generate sample clips")

    directory = tempfile.mkdtemp(prefix="ave2mp4-sample-")
    try:
        stream = encode_clip(sum(chunk_frames), width, height, rate, bframes,
                             directory, 0)
    finally:
        shutil.rmtree(directory, ignore_errors=True)

    return wrap(path, stream, chunk_frames, interval_ticks=interval_ticks,
                width=width, height=height, bframes=bframes,
                start_timestamp_ns=start_timestamp_ns)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__.strip())
        print("\nUsage: make_sample.py OUT.ave [FRAMES ...]")
        return 1
    out = argv[0]
    frames = [int(value) for value in argv[1:]] or [40, 25]
    info = build(out, chunk_frames=frames)
    print("wrote %s: %d frames, %.2f s, %.4f fps"
          % (out, info["frames"], info["duration"], info["rate"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
