#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build synthetic Avigilon exports for testing.

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

#: Codec names as they appear on the command line and in --info.
CODECS = ("h264", "hevc")
CODEC_EXTENSIONS = {"h264": "h264", "hevc": "h265"}
CODEC_ENCODERS = {"h264": "libx264", "hevc": "libx265"}


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


def rcfc(chunk_number, timestamp_ns, clips, tkfh_track_ids=None):
    """The index box for one chunk; ``clips`` is a list of track messages.

    ``tkfh_track_ids`` overrides the track id written into the fixed track
    header, which is how a sample is built whose fixed header disagrees with
    its protobuf.
    """
    payload = box(b"rcfh", struct.pack(">Q", chunk_number)[2:])
    payload += sdat(pb_varint(1, 1))
    for position, (track_id, frames, first_frame, duration_ticks) in enumerate(clips):
        header_id = track_id
        if tkfh_track_ids is not None:
            header_id = tkfh_track_ids[position]
        header = tkfh(header_id, timestamp_ns, duration_ticks)
        body = track_sdat(
            track_id, frames, first_frame, timestamp_ns, duration_ticks
        )
        payload += box(b"tkfc", header + body)
    return box(b"rcfc", payload)


# --------------------------------------------------------------------------
# elementary stream generation
# --------------------------------------------------------------------------


def encode_clip(frames, width, height, rate, bframes, directory, index=0,
                codec="h264"):
    """Encode a throwaway clip with ffmpeg and return its bytes."""
    path = os.path.join(directory, "clip%d.%s" % (index, CODEC_EXTENSIONS[codec]))
    command = [
        "ffmpeg", "-hide_banner", "-v", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=%dx%d:rate=%s" % (width, height, rate),
        "-frames:v", str(frames),
        "-c:v", CODEC_ENCODERS[codec], "-preset", "ultrafast",
        "-pix_fmt", "yuv420p",
        "-g", str(frames),
        "-bf", "2" if bframes else "0",
    ]
    if codec == "h264":
        command += ["-x264-params", "keyint=%d:min-keyint=%d:scenecut=0:log-level=none"
                    % (frames, frames)]
    else:
        command += ["-x265-params", "keyint=%d:min-keyint=%d:log-level=none"
                    % (frames, frames)]
    command += ["-f", codec, path]
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


def _unescape(payload):
    """Drop the 00 00 03 emulation prevention bytes of a NAL payload."""
    out = bytearray()
    zeros = 0
    for byte in payload:
        if zeros >= 2 and byte == 0x03:
            zeros = 0
            continue
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return bytes(out)


def _ue(data, bit):
    """Independent MSB-first exp-Golomb reader; returns (value, next bit)."""
    zeros = 0
    while True:
        if bit >= len(data) * 8:
            raise ValueError("ran out of data")
        value = (data[bit >> 3] >> (7 - (bit & 7))) & 1
        bit += 1
        if value:
            break
        zeros += 1
        if zeros > 32:
            raise ValueError("invalid exp-Golomb code")
    result = (1 << zeros) - 1
    for _ in range(zeros):
        if bit >= len(data) * 8:
            raise ValueError("ran out of data")
        result = (result << 1) | ((data[bit >> 3] >> (7 - (bit & 7))) & 1)
        bit += 1
    return result, bit


def opens_picture(nal, codec):
    """True when this NAL unit is the first of a coded picture.

    H.264: a slice whose ``first_mb_in_slice`` is zero.  H.265: a VCL NAL with
    ``first_slice_segment_in_pic_flag`` set.  Both are read here from the
    specifications, without using anything from the converter.
    """
    if not nal or nal[0] & 0x80:
        return False
    if codec == "h264":
        if (nal[0] & 0x1F) not in (1, 5):
            return False
        try:
            first_mb, _slice_type = _ue(_unescape(nal[1:]), 0)
        except ValueError:
            return False
        return first_mb == 0
    nal_type = (nal[0] >> 1) & 0x3F
    if not 0 <= nal_type <= 31 or len(nal) < 3:
        return False
    return ((_unescape(nal[2:])[0] >> 7) & 1) == 1


def picture_offsets(stream, codec="h264"):
    """Byte offsets of every NAL unit that opens a picture, in decode order."""
    return [start for start, nal in _split_nals(stream)
            if opens_picture(nal, codec)]


def picture_cut_positions(stream, boundaries, codec="h264"):
    """Byte offsets where a chunk may start, one per requested boundary.

    A chunk is a storage boundary, not an encoder boundary: the recorder cuts
    the stream between two pictures and the chunks are reassembled before
    decoding.  The samples reproduce that instead of concatenating unrelated
    encodes.
    """
    starts = picture_offsets(stream, codec)
    cuts = []
    for boundary in boundaries:
        if not 0 < boundary < len(starts):
            raise ValueError("boundary %d is outside the stream" % boundary)
        cuts.append(starts[boundary])
    return cuts


# --------------------------------------------------------------------------
# a synthetic stream that needs no encoder
# --------------------------------------------------------------------------

#: A parameter set and slice header pair that parse the way a real encoder
#: writes them, so a large sample can be built without running ffmpeg.
SYNTHETIC_SPS = bytes.fromhex("6742c00ae8")
SYNTHETIC_PPS = bytes.fromhex("68ce0fc8")
#: first_mb_in_slice = 0 and slice_type = 2 (an I slice, never a B slice).
SYNTHETIC_SLICE_HEADER = b"\xb0"


def synthetic_stream(pictures, payload_size=512, filler=b"\xa5"):
    """A plausible H.264 Annex-B stream of ``pictures`` pictures.

    The pictures are not decodable, but they are laid out exactly like a real
    one: an SPS and a PPS, then a key frame every eighth picture, each NAL
    unit with its own start code.  That is what the container parser is
    exercised against, and it lets a large export be built for the memory
    tests without encoding one.
    """
    out = bytearray(b"\x00\x00\x00\x01" + SYNTHETIC_SPS)
    out += b"\x00\x00\x00\x01" + SYNTHETIC_PPS
    for index in range(pictures):
        header = 0x65 if index % 8 == 0 else 0x61
        out += bytes((0x00, 0x00, 0x00, 0x01, header))
        out += SYNTHETIC_SLICE_HEADER
        out += filler * max(0, payload_size - 1)
    return bytes(out)


# --------------------------------------------------------------------------
# container assembly
# --------------------------------------------------------------------------


def container(path, chunks, tracks_per_chunk, interval_ticks=DEFAULT_INTERVAL_TICKS,
              start_timestamp_ns=1758600000_000_000_000, extra_boxes=True,
              tkfh_track_ids=None):
    """Write an export around a list of already cut chunks.

    ``chunks[i]`` is the elementary stream bytes of the i-th ``datp`` box and
    ``tracks_per_chunk[i]`` is the list of ``(track_id, frames)`` pairs the
    index of that chunk claims.  The pairs are what ties an index entry to the
    bytes in front of it, so a test can build a single video track, a second
    track with data of its own, a chunk that claims two tracks at once, or a
    chunk the index says nothing about.
    """
    blob = bytearray()
    blob += box(b"avfs", AVFS_PAYLOAD)
    blob += box(b"ftyp", FTYP_PAYLOAD)
    # A stand in for the metadata box; ave2mp4 ignores its contents.
    blob += box(b"expc", box(b"exph", bytes(28)) + box(b"root", bytes(32)))

    first_frame = 0
    timestamp_ns = start_timestamp_ns
    total_frames = 0
    for index, (chunk, clips) in enumerate(zip(chunks, tracks_per_chunk)):
        if clips:
            frames = sum(count for _track_id, count in clips)
            entries = [(track_id, count, 0) for track_id, count in clips]
        else:
            # An index that names no track at all, so the chunk before it
            # cannot be attributed to anything.
            frames = 0
            entries = [(VIDEO_TRACK_ID, 0, 0), (OTHER_TRACK_ID, 0, 0)]
        duration_ticks = frames * interval_ticks
        blob += box(b"datp", CHUNK_HEADER + chunk)
        described = [
            (track_id, count,
             first_frame if track_id == VIDEO_TRACK_ID else 0,
             duration_ticks)
            for track_id, count, _start in entries
        ]
        blob += rcfc(index + 1, timestamp_ns, described, tkfh_track_ids)
        total_frames += frames
        first_frame += frames
        timestamp_ns += duration_ticks * (1_000_000_000 // TICKS_PER_SECOND)

    if extra_boxes:
        # Trailing boxes of unknown content; the converter must ignore them.
        blob += box(b"recc", bytes(64))
        blob += box(b"arcc", bytes(128))

    with open(path, "wb") as handle:
        handle.write(bytes(blob))
    return total_frames


def wrap(path, stream, chunk_frames=(40, 25),
         interval_ticks=DEFAULT_INTERVAL_TICKS, width=320, height=240,
         bframes=False, codec="h264",
         start_timestamp_ns=1758600000_000_000_000):
    """Write a synthetic export around an already encoded elementary stream.

    ``chunk_frames`` are the desired chunk sizes; the real cut points are
    snapped to picture boundaries and reported back, exactly like a recorder
    that splits its storage without restarting the encoder.
    """
    boundaries = []
    running = 0
    for frames in chunk_frames[:-1]:
        running += frames
        boundaries.append(running)
    cuts = picture_cut_positions(stream, boundaries, codec)
    bounds = [0] + cuts + [len(stream)]
    pieces = [stream[bounds[i]:bounds[i + 1]]
              for i in range(len(bounds) - 1)]
    counts = [len(picture_offsets(piece, codec)) for piece in pieces]
    tracks_per_chunk = [[(VIDEO_TRACK_ID, count), (OTHER_TRACK_ID, 0)]
                        for count in counts]
    total = container(path, pieces, tracks_per_chunk,
                      interval_ticks=interval_ticks,
                      start_timestamp_ns=start_timestamp_ns)
    return {
        "path": path,
        "frames": total,
        "chunk_frames": counts,
        "interval_ticks": interval_ticks,
        "duration": total * interval_ticks / TICKS_PER_SECOND,
        "rate": TICKS_PER_SECOND / interval_ticks,
        "width": width,
        "height": height,
        "bframes": bframes,
        "codec": codec,
    }


def multi_track_export(path, tracks, interval_ticks=DEFAULT_INTERVAL_TICKS):
    """An export whose ``datp`` boxes belong to several tracks.

    ``tracks`` is a list of ``(track_id, elementary_stream, frames)`` tuples,
    one data box each.  Concatenating them is what a converter that ignores the
    index does, and the result is a video with every track's pictures in it.
    """
    chunks = [stream for _track_id, stream, _frames in tracks]
    tracks_per_chunk = [[(track_id, frames)]
                        for track_id, _stream, frames in tracks]
    total = container(path, chunks, tracks_per_chunk,
                      interval_ticks=interval_ticks)
    return total


def build(path, chunk_frames=(40, 25), interval_ticks=DEFAULT_INTERVAL_TICKS,
          width=320, height=240, rate="25", bframes=False,
          start_timestamp_ns=1758600000_000_000_000, codec="h264"):
    """Encode a clip with ffmpeg, wrap it into an export, describe it."""
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required to generate sample clips")

    directory = tempfile.mkdtemp(prefix="ave2mp4-sample-")
    try:
        stream = encode_clip(sum(chunk_frames), width, height, rate, bframes,
                             directory, 0, codec)
    finally:
        shutil.rmtree(directory, ignore_errors=True)

    return wrap(path, stream, chunk_frames, interval_ticks=interval_ticks,
                width=width, height=height, bframes=bframes, codec=codec,
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
