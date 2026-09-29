#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for ave2mp4.

Runs with pytest, or directly:

    python3 tests/test_ave2mp4.py

Everything here is built from synthetic fixtures, so the suite runs on every
commit without shipping anyone's footage.  The tests that need a real Avigilon
export are at the bottom and are skipped unless AVE2MP4_REAL_SAMPLE points at
one; see the "Contributing" section of the README.
"""

from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import traceback
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ave2mp4  # noqa: E402
import make_sample  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


class Skip(Exception):
    """Raised when a test needs a program that is not installed."""


def require_ffmpeg():
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise Skip("%s is not installed" % tool)


def require_encoder(name):
    require_ffmpeg()
    listing = subprocess.run([shutil.which("ffmpeg"), "-hide_banner", "-encoders"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if name not in listing.stdout.decode("utf-8", "replace").split():
        # -encoders lists one name per line in some versions and name plus
        # description in others; look for it as a whole word.
        if (" %s " % name) not in listing.stdout.decode("utf-8", "replace"):
            raise Skip("ffmpeg has no %s encoder" % name)


def probe(path, count_frames=True):
    """Return {frames, duration, rate, width, height} of a video file."""
    command = ["ffprobe", "-hide_banner", "-v", "error", "-select_streams", "v:0"]
    entries = "stream=width,height,nb_frames,duration,avg_frame_rate,codec_name"
    if count_frames:
        command.append("-count_frames")
        entries = "stream=width,height,nb_read_frames,duration,avg_frame_rate," \
                  "codec_name"
    command += ["-show_entries", entries, "-of", "json", path]
    result = subprocess.run(command, stdout=subprocess.PIPE, check=True)
    stream = json.loads(result.stdout.decode())["streams"][0]
    return {
        "frames": int(stream.get("nb_read_frames" if count_frames else "nb_frames")),
        "duration": float(stream["duration"]),
        "rate": stream["avg_frame_rate"],
        "width": int(stream["width"]),
        "height": int(stream["height"]),
        "codec": stream.get("codec_name"),
    }


def frame_timestamps(path):
    """(pts, dts) of every picture, in the order the file stores them."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "frame=pts,pkt_pts,dts,pkt_dts", "-of", "json", path],
        stdout=subprocess.PIPE, check=True,
    )
    frames = json.loads(result.stdout.decode())["frames"]

    def pick(frame, *names):
        for name in names:
            if frame.get(name) not in (None, "N/A"):
                return int(frame[name])
        return None

    return [(pick(frame, "pts", "pkt_pts"), pick(frame, "dts", "pkt_dts"))
            for frame in frames]


def nals(stream):
    return [nal for _offset, _header_len, nal in ave2mp4.split_nals(stream)]


def elementary_path(directory, tag, codec):
    return os.path.join(directory, "%s.%s" % (tag, make_sample.CODEC_EXTENSIONS[codec]))


def encode_stream(directory, codec="h264", frames=24, bframes=0, source=None,
                  keyint=None, tag="clip"):
    """Encode a small elementary stream with ffmpeg and return its bytes."""
    if source is None:
        source = "testsrc2=size=64x48:rate=25"
    path = elementary_path(directory, tag, codec)
    command = [
        "ffmpeg", "-hide_banner", "-v", "error", "-y",
        "-f", "lavfi", "-i", source,
        "-c:v", make_sample.CODEC_ENCODERS[codec], "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", "-frames:v", str(frames),
        "-bf", str(bframes),
    ]
    if keyint:
        command += ["-g", str(keyint)]
    if codec == "h264":
        command += ["-x264-params", "keyint=%d:min-keyint=%d:scenecut=0:log-level=none"
                    % (keyint or frames, keyint or frames)]
    else:
        command += ["-x265-params", "keyint=%d:min-keyint=%d:log-level=none"
                    % (keyint or frames, keyint or frames)]
    command += ["-f", codec, path]
    subprocess.run(command, check=True)
    with open(path, "rb") as handle:
        return handle.read()


# Every stub has to advertise the capabilities a real ffmpeg 5 or newer has, so
# that the test reaches the failure it is about instead of stopping at the
# capability check.
# A stand-in for ffmpeg has to be an executable the operating system will
# actually run, which a script with a shebang is not on Windows.  The Python
# interpreter itself is: handed ffmpeg's command line it exits non zero, which
# is exactly the "ffmpeg failed" case the output handling has to survive, and it
# needs no stub file on any platform.
UNUSABLE_FFMPEG = sys.executable


def stub_binary(directory, name, source):
    """Write a script stand-in, for the tests that need a scripted binary.

    These are POSIX only: on Windows a script with a shebang is not a
    runnable executable, so the tests that use this raise Skip instead.
    """
    if os.name == "nt":
        raise Skip("a shebang script is not a runnable executable on Windows")
    path = os.path.join(directory, name)
    with open(path, "w") as handle:
        handle.write(source)
    os.chmod(path, 0o755)
    return path


# --------------------------------------------------------------------------
# container parsing
# --------------------------------------------------------------------------


def test_detect_chunk_header_on_real_layout():
    """The layout observed in real exports: SPS in the first chunk, a P slice
    in the second, chunk header of 24 bytes."""
    header = make_sample.CHUNK_HEADER
    first = header + b"\x00\x00\x01\x67" + b"\xaa" * 32
    second = header + b"\x00\x00\x01\x61" + b"\xbb" * 32
    assert ave2mp4.detect_chunk_header([first, second]) == len(header)
    assert len(header) == 24


def test_detect_chunk_header_lands_on_a_valid_start_code():
    """Whatever boundary is chosen must be a real start code plus NAL header."""
    header = make_sample.CHUNK_HEADER
    for start_code in (b"\x00\x00\x01", b"\x00\x00\x00\x01"):
        for first_nal, second_nal in ((0x67, 0x61), (0x67, 0x67)):
            first = header + start_code + bytes([first_nal]) + b"\xaa" * 32
            second = header + start_code + bytes([second_nal]) + b"\xbb" * 32
            offset = ave2mp4.detect_chunk_header([first, second])
            tail = first[offset:]
            if tail.startswith(b"\x00\x00\x00\x01"):
                nal_header = tail[4]
            else:
                assert tail.startswith(b"\x00\x00\x01"), (start_code, offset)
                nal_header = tail[3]
            assert ave2mp4._is_leading_nal_header(nal_header), (start_code, offset)


def test_iter_boxes_reads_largesize():
    """A 64 bit box size must be read, not taken for a corrupt one."""
    payload = b"abcd"
    normal = struct.pack(">I4s", 8 + len(payload), b"free") + payload
    large = (struct.pack(">I4s", 1, b"free")
             + struct.pack(">Q", 16 + len(payload)) + payload)
    assert list(ave2mp4.iter_boxes(normal + large)) == [
        (0, 12, b"free"), (12, 20, b"free")]
    # The payload of a large sized box starts after sixteen bytes, not eight.
    assert ave2mp4.box_payload_offset(normal) == 8
    assert ave2mp4.box_payload_offset(normal + large[0:4]) == 8
    assert ave2mp4.box_payload_offset(large[0:4]) == 16


def test_iter_boxes_rejects_a_truncated_box():
    data = struct.pack(">I4s", 40, b"datp") + b"short"
    try:
        list(ave2mp4.iter_boxes(data))
    except ave2mp4.AveError as error:
        assert "corrupt box" in str(error), error
    else:
        raise AssertionError("a box that runs past the end must be rejected")


def test_rejects_foreign_file():
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "not-an-export.ave")
        with open(path, "wb") as handle:
            handle.write(b"this is definitely not an Avigilon export" * 4)
        try:
            ave2mp4.AveFile(path)
        except ave2mp4.AveError:
            return
        raise AssertionError("a foreign file must be rejected")


def test_parses_synthetic_index():
    require_ffmpeg()
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "sample.ave")
        info = make_sample.build(path, chunk_frames=(16, 8))
        with ave2mp4.AveFile(path) as export:
            chunks = export.video_chunks()
            assert len(chunks) == 2, chunks
            assert sum(chunk["frames"] for chunk in chunks) == info["frames"]
            rate, _note = export.frame_rate()
            assert rate == Fraction(25, 6), rate
            # The exact length of the chunk header is deliberately not
            # asserted: docs/FORMAT.md section 4 records that the last zero of
            # the header is indistinguishable from the first zero of a four
            # byte start code, and the normalised output is the same either
            # way.  What has to hold is that the boundary lands on a real
            # start code followed by a NAL header, which is what the
            # extracted stream starting with one shows.
            assert export.chunk_header_len is not None
            head = next(export.iter_chunks())
            if head.startswith(b"\x00\x00\x00\x01"):
                nal_header = head[4]
            else:
                assert head.startswith(b"\x00\x00\x01"), head[:8]
                nal_header = head[3]
            assert ave2mp4._is_leading_nal_header(nal_header)


# --------------------------------------------------------------------------
# recording index: the protobuf is untrusted input
# --------------------------------------------------------------------------


def test_read_varint_rejects_a_truncated_varint():
    for message in (b"\x80", b"\x80\x80\x80", b"\x80" * 12):
        try:
            ave2mp4.read_varint(message, 0)
        except ave2mp4.AveError as error:
            assert "truncated" in str(error) or "too long" in str(error), error
        else:
            raise AssertionError("%r must not parse" % message)


def test_parse_message_rejects_fields_that_run_past_the_end():
    """A length that claims more bytes than the box holds used to be handed
    out as a short slice, which silently produced a wrong value."""
    cases = {
        "length past the end": b"\x08\x01\x12\x7f\x41\x42",
        "truncated fixed64": b"\x08\x01\x09\x41\x42",
        "truncated fixed32": b"\x08\x01\x15\x41\x42",
        "truncated varint": b"\x08\x80\x80",
        "field number zero": b"\x00\x01",
    }
    for label, message in cases.items():
        try:
            list(ave2mp4.parse_message(message))
        except ave2mp4.AveError as error:
            assert "container index" in str(error), (label, error)
        except Exception as error:      # noqa: BLE001 - the point of the test
            raise AssertionError("%s raised %s: %s"
                                 % (label, type(error).__name__, error))
        else:
            raise AssertionError("%s must be rejected" % label)


def test_parse_message_rejects_wire_types_it_cannot_read():
    for field in (2, 3):
        for wire in (3, 4, 6, 7):
            message = b"\x08\x01" + make_sample.varint((field << 3) | wire)
            try:
                list(ave2mp4.parse_message(message))
            except ave2mp4.AveError as error:
                assert "wire type" in str(error), (wire, error)
            else:
                raise AssertionError("wire type %d must be rejected" % wire)


def _export_with_broken_index(directory, message):
    """An export whose single index message is written by hand."""
    tkfc = make_sample.box(
        b"tkfc",
        make_sample.tkfh(101, 1758600000_000_000_000, 21600)
        + make_sample.sdat(message),
    )
    blob = bytearray()
    blob += make_sample.box(b"avfs", make_sample.AVFS_PAYLOAD)
    blob += make_sample.box(b"ftyp", make_sample.FTYP_PAYLOAD)
    blob += make_sample.box(
        b"datp",
        make_sample.CHUNK_HEADER + make_sample.synthetic_stream(8),
    )
    blob += make_sample.box(
        b"rcfc",
        make_sample.box(b"rcfh", bytes(6))
        + make_sample.sdat(make_sample.pb_varint(1, 1))
        + tkfc,
    )
    path = os.path.join(directory, "broken.ave")
    with open(path, "wb") as handle:
        handle.write(bytes(blob))
    return path


def _good_index_message(frames=8, duration=8 * 21600):
    return (make_sample.pb_varint(1, 101)
            + make_sample.pb_varint(2, frames)
            + make_sample.pb_varint(3, 0)
            + make_sample.pb_fixed64(4, 1758600000_000_000_000)
            + make_sample.pb_varint(6, duration))


def test_index_field_with_the_wrong_wire_type_is_a_clean_error():
    """A picture count delivered as a length delimited field used to reach
    sum() as bytes and raise a bare TypeError out of --info."""
    message = (make_sample.pb_varint(1, 101)
               + make_sample.pb_bytes(2, b"\x08\x01")
               + make_sample.pb_varint(3, 0)
               + make_sample.pb_fixed64(4, 1758600000_000_000_000)
               + make_sample.pb_varint(6, 8 * 21600))
    with tempfile.TemporaryDirectory() as directory:
        path = _export_with_broken_index(directory, message)
        try:
            ave2mp4.AveFile(path).frame_rate()
        except ave2mp4.AveError as error:
            assert "not a varint" in str(error), error
        else:
            raise AssertionError("a wrongly typed index field must be refused")
        assert ave2mp4.main(["--info", path]) == 1


def test_index_field_with_a_truncated_value_is_a_clean_error():
    for label, message in [
        ("fixed64 truncated", make_sample.pb_varint(1, 101)
         + make_sample.tag(4, 1) + b"\x01\x02\x03"),
        ("length past the end", make_sample.pb_varint(1, 101)
         + make_sample.tag(7, 2) + make_sample.varint(99) + b"\x00"),
    ]:
        with tempfile.TemporaryDirectory() as directory:
            path = _export_with_broken_index(directory, message)
            try:
                ave2mp4.AveFile(path)
            except ave2mp4.AveError as error:
                assert "container index" in str(error), (label, error)
            except Exception as error:  # noqa: BLE001
                raise AssertionError("%s raised %s: %s"
                                     % (label, type(error).__name__, error))
            else:
                raise AssertionError("%s must be rejected" % label)


def test_index_timestamp_of_the_wrong_width_is_ignored_not_fatal():
    message = (make_sample.pb_varint(1, 101)
               + make_sample.pb_varint(2, 8)
               + make_sample.pb_bytes(4, b"\x01\x02\x03\x04")
               + make_sample.pb_varint(6, 8 * 21600))
    with tempfile.TemporaryDirectory() as directory:
        path = _export_with_broken_index(directory, message)
        with ave2mp4.AveFile(path) as export:
            # A field 4 that is not a fixed64 cannot be a timestamp, but the
            # rest of the index is still perfectly usable.
            assert export.first_timestamp() is None
            assert export.frame_rate()[0] == Fraction(25, 6)
            assert "starts at" not in export.describe()


def test_index_field_that_is_not_bytes_where_bytes_belong_is_refused():
    message = (make_sample.pb_varint(1, 101)
               + make_sample.pb_varint(2, 8)
               + make_sample.pb_varint(4, 1758600000)
               + make_sample.pb_varint(6, 8 * 21600))
    with tempfile.TemporaryDirectory() as directory:
        path = _export_with_broken_index(directory, message)
        try:
            ave2mp4.AveFile(path)
        except ave2mp4.AveError as error:
            assert "not a byte field" in str(error), error
        else:
            raise AssertionError("a varint where a fixed64 belongs must fail")


def test_export_without_timing_information_is_reported_not_crashed():
    message = make_sample.pb_varint(1, 101) + make_sample.pb_varint(2, 8)
    with tempfile.TemporaryDirectory() as directory:
        path = _export_with_broken_index(directory, message)
        with ave2mp4.AveFile(path) as export:
            try:
                export.frame_rate()
            except ave2mp4.AveError as error:
                assert "timing information" in str(error), error
            else:
                raise AssertionError("a recording without timing must be refused")
            # --info still has to work and say so rather than crash.
            text = export.describe()
            assert "frame rate      : unknown" in text, text


# --------------------------------------------------------------------------
# codec detection
# --------------------------------------------------------------------------

#: A real H.264 SPS and PPS, taken from what libx264 writes.
H264_SPS = bytes.fromhex("6742c00ada11")
H264_PPS = bytes.fromhex("68ce0fc8")
#: A real H.265 VPS, SPS, PPS, IDR and TRAIL, taken from what libx265 writes.
HEVC_VPS = bytes.fromhex("40010c01ffff016000000300900000030000")
HEVC_SPS = bytes.fromhex(
    "42010101600000030090000003000003001ea02083165ba4a4c2f0168080000003008000000c84"
)
HEVC_PPS = bytes.fromhex("4401c073c089")
HEVC_IDR = bytes.fromhex("2801ac76a0e6")
HEVC_TRAIL = bytes.fromhex("0201d0097883b09e5c")


def annexb(*units):
    return b"".join(b"\x00\x00\x00\x01" + unit for unit in units)


def test_detect_codec_reads_h264_parameter_sets():
    assert ave2mp4.detect_codec(annexb(H264_SPS, H264_PPS)) == "h264"


def test_detect_codec_reads_hevc_parameter_sets():
    assert ave2mp4.detect_codec(annexb(HEVC_VPS, HEVC_SPS, HEVC_PPS)) == "hevc"


def test_detect_codec_reads_hevc_slices():
    stream = annexb(HEVC_VPS, HEVC_SPS, HEVC_PPS, HEVC_IDR)
    assert ave2mp4.detect_codec(stream + annexb(HEVC_TRAIL) * 4) == "hevc"


def test_detect_codec_does_not_mistake_h264_slices_for_hevc():
    """The reported failure: every H.264 slice header 0x41..0x45 also reads as
    a valid H.265 VPS, SPS, PPS or AUD header, so a stream that starts with a
    plain P slice used to be read as H.265 and then truncated."""
    for header in (0x41, 0x45):
        slice_nal = bytes([header, 0x9A, 0x22, 0x86, 0xC0])
        stream = annexb(slice_nal) * 8
        assert ave2mp4.detect_codec(stream) == "h264", hex(header)
    for header in (0x61, 0x65, 0x67):
        stream = annexb(bytes([header, 0x9A, 0x22, 0x86, 0xC0])) * 8
        assert ave2mp4.detect_codec(stream) == "h264", hex(header)


def test_detect_codec_refuses_an_ambiguous_head():
    """A single NAL unit that reads as either codec must not be guessed."""
    for header in (0x42, 0x43, 0x44, 0x46, 0x47):
        try:
            ave2mp4.detect_codec(annexb(bytes([header, 0x9A, 0x22, 0x86, 0xC0])))
        except ave2mp4.AveError as error:
            assert "H.264 or H.265" in str(error), (hex(header), error)
        else:
            raise AssertionError("0x%02x must not be guessed" % header)


def test_detect_codec_refuses_data_that_is_not_a_video_stream():
    for stream in (b"", b"\x00\x00\x00\x01" + bytes(40),
                   annexb(b"\xff\xfe\xfd\xfc")):
        try:
            ave2mp4.detect_codec(stream)
        except ave2mp4.AveError as error:
            assert "H.264 or H.265" in str(error) or "no recognizable" in str(error)
        else:
            raise AssertionError("%r must be rejected" % stream[:12])


def test_detect_codec_on_real_encoded_streams():
    require_ffmpeg()
    require_encoder("libx264")
    with tempfile.TemporaryDirectory() as directory:
        h264 = encode_stream(directory, "h264", 12, tag="real264")
        assert ave2mp4.detect_codec(h264) == "h264"
    require_encoder("libx265")
    with tempfile.TemporaryDirectory() as directory:
        hevc = encode_stream(directory, "hevc", 12, tag="real265")
        assert ave2mp4.detect_codec(hevc) == "hevc"


def test_wrong_codec_detection_cannot_truncate_the_stream():
    """An H.264 export that starts with a P slice used to lose 79% of its
    pictures.  The extracted bytes, not just the exit code, are checked."""
    require_ffmpeg()
    require_encoder("libx264")
    with tempfile.TemporaryDirectory() as directory:
        stream = encode_stream(directory, "h264", 40, tag="trunc")
        # Drop everything up to and including the IDR slice, so the stream
        # opens with a plain P slice - the head the old detector read as an
        # H.265 VPS and then truncated the export at.
        idr_end = 0
        for offset, header_len, nal in ave2mp4.split_nals(stream):
            if (nal[0] & 0x1F) == 5:
                idr_end = offset + header_len + len(nal)
        stripped = stream[idr_end:]
        # The head of the stream is a plain P slice, whose header byte is in
        # the 0x41..0x45 range that also reads as an H.265 parameter set.
        head = ave2mp4.split_nals(stripped)[0][2]
        assert 0x41 <= head[0] <= 0x45, "unexpected head 0x%02x" % head[0]
        assert ave2mp4.detect_codec(stripped) == "h264"

        path = os.path.join(directory, "trunc.ave")
        make_sample.wrap(path, stripped, chunk_frames=(10, 10, 10, 10),
                         width=64, height=48)
        expected = [nal for _o, _h, nal in ave2mp4.split_nals(stripped)]
        # Counted with the fixture's own slice header reader, not with the
        # converter's.
        pictures = [nal for _start, nal in make_sample._split_nals(stripped)
                    if make_sample.opens_picture(nal, "h264")]
        with ave2mp4.AveFile(path) as export:
            codec, analysis = export.scan()
            extracted = [nal for _o, _h, nal in export.iter_nals()]
        assert codec == "h264", codec
        assert extracted == expected, (len(extracted), len(expected))
        assert analysis["stream_end"] == sum(4 + len(n) for n in expected)
        assert analysis["pictures"] == len(pictures), analysis["pictures"]


# --------------------------------------------------------------------------
# elementary stream splitting
# --------------------------------------------------------------------------


def test_split_nals_handles_both_start_codes():
    stream = b"\x00\x00\x01\x67AA\x00\x00\x00\x01\x68BB"
    nals = [nal for _o, _h, nal in ave2mp4.split_nals(stream)]
    assert nals == [b"\x67AA", b"\x68BB"], nals


def test_streaming_split_matches_the_batch_split():
    """The streaming splitter is what a conversion uses; it has to produce
    exactly what joining everything and splitting it produces."""
    stream = (b"\x00\x00\x01\x67" + b"\xaa" * 40 + b"\x00\x00\x00\x01\x68BB"
              + b"\x00\x00\x01\x65" + b"\xcc" * 90 + b"\x00\x00\x00\x01")
    expected = bytearray()
    for _offset, _header_len, nal in ave2mp4.split_nals(stream):
        nal = nal.rstrip(b"\x00")
        if nal:
            expected += b"\x00\x00\x00\x01" + nal
    assert ave2mp4.normalise_stream([stream]) == bytes(expected)


def test_a_nal_unit_across_a_chunk_boundary_stays_whole():
    """A chunk is a storage boundary, so the recorder may cut a NAL unit - and
    even its start code - in half.  The stream is reassembled first."""
    stream = (b"\x00\x00\x01\x67" + b"\xaa" * 30 + b"\x00\x00\x00\x01\x65"
              + b"\xbb" * 30 + b"\x00\x00\x01\x68" + b"\xcc" * 10)
    expected = ave2mp4.normalise_stream([stream])
    for cut in range(1, len(stream)):
        assert ave2mp4.normalise_stream([stream[:cut], stream[cut:]]) == expected, cut
    # ... including when the chunks arrive in many small pieces.
    pieces = [stream[index:index + 3] for index in range(0, len(stream), 3)]
    assert ave2mp4.normalise_stream(pieces) == expected


def test_an_export_larger_than_one_window_extracts_byte_for_byte():
    """The converter reads the data in windows, and the chunk header may only
    be removed from the first window of a box.  A payload bigger than one
    window is what catches that."""
    with tempfile.TemporaryDirectory() as directory:
        pictures = 8
        payload = 256 * 1024
        stream = make_sample.synthetic_stream(pictures, payload_size=payload)
        assert len(stream) > ave2mp4.MAX_CHUNK_BYTES, len(stream)
        path = os.path.join(directory, "wide.ave")
        make_sample.container(path, [stream, stream],
                              [[(101, pictures)], [(101, pictures)]])
        # Expected bytes from the fixture's own splitter, not the converter's.
        expected = bytearray()
        for _start, nal in make_sample._split_nals(stream):
            nal = nal.rstrip(b"\x00")
            if nal:
                expected += b"\x00\x00\x00\x01" + nal
        with ave2mp4.AveFile(path) as export:
            extracted = export.stream
            windows = sum(1 for _piece in export.iter_chunks())
        assert extracted == bytes(expected) * 2, (len(extracted), len(expected))
        assert windows > 2, windows


def test_leading_garbage_before_the_first_start_code_is_dropped():
    stream = b"\x00" * 300 + b"\x00\x00\x01\x67" + b"\xaa" * 10
    assert ave2mp4.normalise_stream([stream]) == \
        b"\x00\x00\x00\x01\x67" + b"\xaa" * 10


def test_video_data_without_any_start_code_is_refused():
    try:
        ave2mp4.normalise_stream([b"\xaa" * ((1 << 22) + 1)])
    except ave2mp4.AveError as error:
        assert "start code" in str(error), error
    else:
        raise AssertionError("data with no start code must be refused")


# --------------------------------------------------------------------------
# track selection
# --------------------------------------------------------------------------


def _single_track_export(directory, pictures=16):
    path = os.path.join(directory, "single.ave")
    make_sample.build(path, chunk_frames=(pictures // 2, pictures // 2))
    return path


def test_a_single_video_track_is_extracted():
    require_ffmpeg()
    with tempfile.TemporaryDirectory() as directory:
        path = _single_track_export(directory)
        with ave2mp4.AveFile(path) as export:
            assert export.video_track_id == make_sample.VIDEO_TRACK_ID
            assert len(export.video_spans) == len(export.data_spans)
            assert export.secondary_tracks == []
            assert export.indexed is True


def test_a_second_track_with_data_of_its_own_is_not_spliced_into_the_video():
    """Concatenating a second track's data box produced a video with every
    picture of both tracks in it, and a warning that named the wrong track.

    The second track here is not a video stream, which is what tells the two
    apart: the track number is not used, because the track numbers come from
    one Avigilon version.
    """
    require_ffmpeg()
    require_encoder("libx264")
    with tempfile.TemporaryDirectory() as directory:
        video = encode_stream(directory, "h264", 12, tag="vid")
        # Stands in for the audio or metadata track of a real export: data
        # that does not parse as an Annex-B video stream.
        other = bytes(range(256)) * 40
        path = os.path.join(directory, "multi.ave")
        make_sample.multi_track_export(
            path, [(make_sample.VIDEO_TRACK_ID, video, 12),
                   (make_sample.OTHER_TRACK_ID, other, 40)])
        output = os.path.join(directory, "multi.mp4")
        with ave2mp4.AveFile(path) as export:
            assert export.video_track_id == make_sample.VIDEO_TRACK_ID
            assert len(export.video_spans) == 1, "a second data box was used"
            assert export.secondary_tracks, "the second track was not reported"
            assert export.secondary_tracks[0]["track_id"] == \
                make_sample.OTHER_TRACK_ID
            # The expected NAL units come from the fixture's own splitter, not
            # from ave2mp4, so the two are not compared against themselves.
            extracted = [nal for _o, _h, nal in export.iter_nals()]
            expected = [nal for _start, nal in make_sample._split_nals(video)]
        assert extracted == expected, (len(extracted), len(expected))
        assert other[:64] not in b"".join(extracted)
        ave2mp4.convert(path, output=output, verbosity=0)
        assert probe(output)["frames"] == 12, probe(output)


def test_a_second_track_that_is_also_a_video_is_refused():
    """Two tracks that both hold a video stream: nothing says which one the
    export is about, so nothing is written."""
    require_ffmpeg()
    require_encoder("libx264")
    with tempfile.TemporaryDirectory() as directory:
        video = encode_stream(directory, "h264", 12, tag="one")
        other = encode_stream(directory, "h264", 12,
                              source="smptebars=size=64x48:rate=25", tag="two")
        path = os.path.join(directory, "two.ave")
        make_sample.multi_track_export(
            path, [(make_sample.VIDEO_TRACK_ID, video, 12),
                   (make_sample.OTHER_TRACK_ID, other, 12)])
        output = os.path.join(directory, "two.mp4")
        try:
            ave2mp4.convert(path, output=output, verbosity=0)
        except ave2mp4.AveError as error:
            assert "no way to tell" in str(error), error
        else:
            raise AssertionError("two candidate video tracks must be refused")
        assert not os.path.exists(output)


def test_a_chunk_claiming_two_tracks_is_refused():
    require_ffmpeg()
    with tempfile.TemporaryDirectory() as directory:
        stream = make_sample.synthetic_stream(12)
        path = os.path.join(directory, "mixed.ave")
        make_sample.container(path, [stream],
                              [[(101, 12), (201, 12)]])
        try:
            ave2mp4.convert(path, output=os.path.join(directory, "m.mp4"),
                            verbosity=0)
        except ave2mp4.AveError as error:
            assert "more than one track" in str(error), error
        else:
            raise AssertionError("a chunk with two tracks must be refused")


def test_a_chunk_the_index_does_not_describe_is_refused():
    require_ffmpeg()
    with tempfile.TemporaryDirectory() as directory:
        stream = make_sample.synthetic_stream(8)
        path = os.path.join(directory, "orphan.ave")
        make_sample.container(path, [stream, stream], [[(101, 8)], []])
        try:
            ave2mp4.convert(path, output=os.path.join(directory, "o.mp4"),
                            verbosity=0)
        except ave2mp4.AveError as error:
            assert "does not describe" in str(error), error
        else:
            raise AssertionError("an unattributed data box must be refused")


def test_an_export_without_an_index_uses_every_box_and_says_so():
    require_ffmpeg()
    with tempfile.TemporaryDirectory() as directory:
        stream = make_sample.synthetic_stream(8)
        path = os.path.join(directory, "noindex.ave")
        blob = bytearray()
        blob += make_sample.box(b"avfs", make_sample.AVFS_PAYLOAD)
        blob += make_sample.box(b"ftyp", make_sample.FTYP_PAYLOAD)
        for _ in range(3):
            blob += make_sample.box(b"datp", make_sample.CHUNK_HEADER + stream)
        with open(path, "wb") as handle:
            handle.write(bytes(blob))
        with ave2mp4.AveFile(path) as export:
            assert export.indexed is False
            assert len(export.video_spans) == 3
            text = export.describe()
            assert "recording index does not describe" in text, text
        # Without an index there is no frame rate either, and that is a
        # refusal rather than a guessed one.
        try:
            ave2mp4.convert(path, output=os.path.join(directory, "n.mp4"),
                            verbosity=0)
        except ave2mp4.AveError as error:
            assert ("timing information" in str(error)
                    or "no indexed video frames" in str(error)), error
        else:
            raise AssertionError("a conversion without timing must be refused")


def test_extracted_bytes_match_the_source_track_not_the_extractor():
    """The expected bytes are the stream that went into the fixture, not
    whatever ave2mp4's own splitter produces."""
    require_ffmpeg()
    require_encoder("libx264")
    with tempfile.TemporaryDirectory() as directory:
        video = encode_stream(directory, "h264", 20, tag="src")
        other = bytes(range(256)) * 40
        path = os.path.join(directory, "two.ave")
        # The video track in two chunks, plus a data box of the second track.
        boundaries = make_sample.picture_cut_positions(video, [10], "h264")
        pieces = [video[:boundaries[0]], video[boundaries[0]:]]
        make_sample.container(
            path,
            pieces + [other],
            [[(101, 10)], [(101, 10)], [(201, 4)]],
        )
        # A converter that concatenates every data box returns the second
        # track's bytes as well; this one must not.
        with ave2mp4.AveFile(path) as export:
            extracted = b"".join(ave2mp4.START_CODE + nal
                                 for _o, _h, nal in export.iter_nals())
            picked = export.video_track_id
        expected = make_sample._split_nals(video)
        assert len(extracted) == sum(4 + len(nal) for _s, nal in expected)
        assert other[:64] not in extracted
        assert picked == 101


def test_secondary_tracks_are_reported_not_hidden():
    require_ffmpeg()
    with tempfile.TemporaryDirectory() as directory:
        stream = make_sample.synthetic_stream(8)
        other = bytes(range(256)) * 20
        path = os.path.join(directory, "reported.ave")
        make_sample.container(path, [stream, other],
                              [[(101, 8)], [(201, 4)]])
        with ave2mp4.AveFile(path) as export:
            text = export.describe()
            assert export.secondary_tracks, "the second track was not recorded"
        assert "201" in text, text
        assert "not converted" in text, text


def test_a_second_track_on_a_recorder_that_uses_other_track_numbers():
    """The video track is found by its content, so a recorder that does not
    use track 101 still works; the difference is reported."""
    require_ffmpeg()
    require_encoder("libx264")
    with tempfile.TemporaryDirectory() as directory:
        video = encode_stream(directory, "h264", 8, tag="odd")
        path = os.path.join(directory, "odd.ave")
        make_sample.container(path, [video], [[(7, 8)]])
        with ave2mp4.AveFile(path) as export:
            assert export.video_track_id == 7
            text = export.describe()
        assert "not the 101" in text, text


# --------------------------------------------------------------------------
# reordering
# --------------------------------------------------------------------------


def _ue(value):
    """Encode an unsigned exp-Golomb value, for building slice headers.

    The value plus one is written in binary behind as many zero bits as it has
    bits, which is what the decoder counts.
    """
    bits = value + 1
    zeros = bits.bit_length() - 1
    out = [(0,)] * zeros
    for index in range(zeros, -1, -1):
        out.append(((bits >> index) & 1,))
    return out


def _pack(bits):
    """Turn a list of single bit tuples into bytes, padding with zeros."""
    out = bytearray()
    for start in range(0, len(bits), 8):
        byte = 0
        for bit in bits[start:start + 8]:
            byte = (byte << 1) | bit[0]
        out.append(byte << (8 - min(8, len(bits) - start)))
    return bytes(out)


def test_h264_b_frame_streams_are_detected():
    """slice_type 1 and 6 are B slices; they can only exist in a reordered
    stream."""
    def slice_nal(slice_type):
        # first_mb_in_slice = 0, then slice_type, then filler bytes.
        return b"\x41" + _pack(_ue(0) + _ue(slice_type) + [(0,)] * 32)

    plain = annexb(H264_SPS, H264_PPS) + annexb(slice_nal(0)) * 6
    analysis = ave2mp4.analyse_stream(plain, "h264")
    assert analysis["reordering"] == "no", analysis["reordering"]
    assert analysis["pictures"] == 6, analysis

    for slice_type in (1, 6):
        reordered = annexb(H264_SPS, H264_PPS) + annexb(slice_nal(slice_type)) * 6
        analysis = ave2mp4.analyse_stream(reordered, "h264")
        assert analysis["reordering"] == "yes", (slice_type, analysis)


def test_picture_order_count_decides_hevc_reordering():
    assert ave2mp4.poc_is_in_decode_order([1, 2, 3, 4], 8)
    assert ave2mp4.poc_is_in_decode_order([0, 2, 4, 6], 8)
    assert ave2mp4.poc_is_in_decode_order([250, 251, 252, 253], 8)   # wraps
    assert not ave2mp4.poc_is_in_decode_order([3, 2, 1, 6], 8)
    assert not ave2mp4.poc_is_in_decode_order([0, 4, 2, 6], 8)
    assert not ave2mp4.poc_is_in_decode_order([0, 0, 0, 0], 8)
    assert ave2mp4.poc_is_in_decode_order([5], 8)                   # one picture


def test_hevc_sps_and_pps_are_read_from_the_specification():
    assert ave2mp4.parse_hevc_sps(HEVC_SPS) == {
        "log2_max_pic_order_cnt_lsb": 8, "pic_order_cnt_type": 0}
    assert ave2mp4.parse_hevc_pps(HEVC_PPS) == {
        "dependent_slice_segments_enabled_flag": 0,
        "output_flag_present_flag": 0,
        "num_extra_slice_header_bits": 0,
    }
    assert ave2mp4.parse_hevc_sps(b"\x42\x01") is None            # truncated
    assert ave2mp4.parse_hevc_pps(b"\x44\x01") is None


def test_reordered_hevc_streams_are_refused():
    """A single NAL unit header is not enough here: H.265 calls a P picture a
    B slice too, so only the picture order counts decide."""
    require_ffmpeg()
    require_encoder("libx265")
    for bframes in (1, 2, 3, 4):
        with tempfile.TemporaryDirectory() as directory:
            stream = _tagged_hevc(directory, bframes)
            truth = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
                 "-show_entries", "stream=has_b_frames", "-of", "csv=p=0",
                 elementary_path(directory, "tagged", "hevc")],
                stdout=subprocess.PIPE, check=True).stdout.decode().strip()
            assert truth and int(truth) > 0, "libx265 produced no B-frames"
            path = os.path.join(directory, "ord.ave")
            make_sample.wrap(path, stream, chunk_frames=(10, 20), codec="hevc",
                             width=64, height=48, bframes=True)
            output = os.path.join(directory, "ord.mp4")
            with ave2mp4.AveFile(path) as export:
                codec, analysis = export.scan()
            assert codec == "hevc", codec
            assert analysis["reordering"] == "yes", (bframes, analysis["reordering"])
            try:
                ave2mp4.convert(path, output=output, verbosity=0)
            except ave2mp4.AveError as error:
                assert "B-frames" in str(error), error
            else:
                raise AssertionError("a reordered H.265 export must be refused")
            assert not os.path.exists(output), "a refusal must leave no output"


def _tagged_hevc(directory, bframes):
    """A numbered H.265 stream, so the picture order is observable."""
    return tagged_stream(30, bframes=bool(bframes), directory=directory,
                         codec="hevc")


def test_reordered_hevc_streams_can_be_reencoded():
    require_ffmpeg()
    require_encoder("libx265")
    with tempfile.TemporaryDirectory() as directory:
        stream = _tagged_hevc(directory, 2)
        path = os.path.join(directory, "re.ave")
        make_sample.wrap(path, stream, chunk_frames=(10, 20), codec="hevc",
                         width=64, height=48, bframes=True)
        output = os.path.join(directory, "re.mp4")
        ave2mp4.convert(path, output=output, verbosity=0, reencode=True)
        assert presentation_order(output) == list(range(30))


def test_hevc_without_reordering_converts_in_display_order():
    require_ffmpeg()
    require_encoder("libx265")
    with tempfile.TemporaryDirectory() as directory:
        stream = _tagged_hevc(directory, 0)
        truth = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
             "-show_entries", "stream=has_b_frames", "-of", "csv=p=0",
             elementary_path(directory, "tagged", "hevc")],
            stdout=subprocess.PIPE, check=True).stdout.decode().strip()
        assert truth == "0", "libx265 produced B-frames with -bf 0"
        path = os.path.join(directory, "safe.ave")
        make_sample.wrap(path, stream, chunk_frames=(10, 20), codec="hevc",
                         width=64, height=48)
        output = os.path.join(directory, "safe.mp4")
        with ave2mp4.AveFile(path) as export:
            codec, analysis = export.scan()
        assert codec == "hevc" and analysis["reordering"] == "no", analysis
        ave2mp4.convert(path, output=output, verbosity=0)
        assert presentation_order(output) == list(range(30)), "pictures shuffled"
        assert probe(output)["frames"] == 30


def test_hevc_without_an_sps_has_unknown_ordering_and_is_refused():
    """Without the SPS the picture order count cannot be read, so the export
    is refused rather than converted on a guess."""
    stream = annexb(HEVC_VPS, HEVC_PPS, HEVC_IDR) + annexb(HEVC_TRAIL) * 6
    analysis = ave2mp4.analyse_stream(stream, "hevc")
    assert analysis["reordering"] == "unknown", analysis["reordering"]


def test_a_truncated_stream_is_refused_instead_of_silently_shortened():
    """If a picture in the middle of the recording cannot be read, everything
    behind it would be dropped; that must be an error, not a short file."""
    require_ffmpeg()
    with tempfile.TemporaryDirectory() as directory:
        stream = encode_stream(directory, "h264", 24, tag="damaged")
        path = os.path.join(directory, "damaged.ave")
        make_sample.wrap(path, stream, chunk_frames=(8, 8, 8), width=64, height=48)
        with open(path, "rb") as handle:
            data = bytearray(handle.read())
        # Wreck the slice header of a picture in the middle of the recording.
        # A run of zero bits is an exp-Golomb code that no decoder can read,
        # and everything after that picture would be thrown away with it.
        boxes = [(offset, size) for offset, size, kind
                 in ave2mp4.iter_boxes(bytes(data)) if kind == b"datp"]
        assert len(boxes) == 3, boxes
        start = boxes[1][0] + 8
        first = data.find(b"\x00\x00\x01", start)
        second = data.find(b"\x00\x00\x01", first + 3)
        assert first >= 0 and second > first, "the second chunk is too short"
        header_end = second + 3
        if data[second - 1] == 0:
            header_end = second + 4
        data[header_end:header_end + 12] = b"\x00" * 12
        with open(path, "wb") as handle:
            handle.write(bytes(data))
        output = os.path.join(directory, "t.mp4")
        try:
            ave2mp4.convert(path, output=output, verbosity=0)
        except ave2mp4.AveError as error:
            assert "could not be parsed" in str(error), error
        else:
            raise AssertionError("a damaged stream must be refused")
        assert not os.path.exists(output)


def test_a_stream_shorter_than_the_index_claims_is_refused():
    """The picture count in the container is metadata the parser can be
    checked against, and a big shortfall means video went missing."""
    with tempfile.TemporaryDirectory() as directory:
        stream = make_sample.synthetic_stream(8)
        path = os.path.join(directory, "short.ave")
        # The index claims 400 pictures; the data holds 8.
        make_sample.container(path, [stream], [[(101, 400)]])
        try:
            ave2mp4.convert(path, output=os.path.join(directory, "s.mp4"),
                            verbosity=0)
        except ave2mp4.AveError as error:
            assert "only" in str(error) and "could be read" in str(error), error
        else:
            raise AssertionError("a stream that lost its pictures must be refused")


# --------------------------------------------------------------------------
# finding ffmpeg
# --------------------------------------------------------------------------


def test_ffmpeg_is_found_and_runs():
    path = ave2mp4.find_ffmpeg()
    if not path:
        raise Skip("ffmpeg is not installed")
    result = subprocess.run([path, "-version"], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    assert result.returncode == 0, path


def test_bundled_ffmpeg_is_preferred():
    """A frozen build ships its own ffmpeg and must use it, not the PATH."""
    name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    with tempfile.TemporaryDirectory() as directory:
        bundled = os.path.join(directory, name)
        with open(bundled, "wb") as handle:
            handle.write(b"stand in for a bundled binary")
        had = hasattr(sys, "_MEIPASS")
        previous = getattr(sys, "_MEIPASS", None)
        sys._MEIPASS = directory
        try:
            assert ave2mp4.find_ffmpeg() == bundled
        finally:
            if had:
                sys._MEIPASS = previous
            else:
                del sys._MEIPASS


def test_missing_ffmpeg_explains_how_to_install_it():
    previous = ave2mp4.find_ffmpeg
    ave2mp4.find_ffmpeg = lambda explicit=None: None
    try:
        try:
            ave2mp4.require_ffmpeg()
        except ave2mp4.AveError as error:
            for hint in ("winget", "brew", "apt", "dnf", "imageio-ffmpeg",
                         "--ffmpeg"):
                assert hint in str(error), hint
        else:
            raise AssertionError("a missing ffmpeg must be an error")
    finally:
        ave2mp4.find_ffmpeg = previous


def test_an_ffmpeg_without_the_setts_filter_is_refused():
    """setts arrived in ffmpeg 5.0.  A private copy that is older cannot give
    every copied picture its own timestamp, so it has to be said up front."""
    with tempfile.TemporaryDirectory() as directory:
        path = stub_binary(directory, "oldffmpeg", """#!/usr/bin/env python3
import sys
print('ffmpeg version 4.2.2-1ubuntu1')
if 'bsfs' in sys.argv:
    print('Bitstream Filters:')
    print('aac_adtstoasc')
sys.exit(0)
""")
        tool = ave2mp4.Ffmpeg.get(path)
        assert tool.has_filter("setts") is False
        try:
            tool.require()
        except ave2mp4.AveError as error:
            assert "setts" in str(error) and "5.0" in str(error), error
        else:
            raise AssertionError("an ffmpeg without setts must be refused")


def test_reencode_needs_the_encoder_it_names():
    with tempfile.TemporaryDirectory() as directory:
        path = stub_binary(directory, "noh264", """#!/usr/bin/env python3
import sys
if '-version' in sys.argv:
    print('ffmpeg version 8.1.1')
elif '-bsfs' in sys.argv:
    print('Bitstream Filters:')
    print('setts')
elif '-encoders' in sys.argv:
    print('Encoders:')
    print(' V....D mpeg4                 MPEG-4 part 2')
sys.exit(0)
""")
        tool = ave2mp4.Ffmpeg.get(path)
        assert tool.has_filter("setts") is True
        try:
            tool.require("libx264")
        except ave2mp4.AveError as error:
            assert "libx264" in str(error), error
        else:
            raise AssertionError("a missing libx264 must be reported")


# --------------------------------------------------------------------------
# end to end conversion
# --------------------------------------------------------------------------


def _convert_and_check(directory, bframes):
    source = os.path.join(directory, "bframes.ave" if bframes else "plain.ave")
    output = os.path.join(directory, "out.mp4")
    info = make_sample.build(source, chunk_frames=(40, 25), bframes=bframes)

    ave2mp4.convert(source, output=output, verbosity=0)
    assert os.path.exists(output), "no output written"

    probed = probe(output)
    assert probed["frames"] == info["frames"], (probed, info)
    assert abs(probed["duration"] - info["duration"]) < 0.3, (probed, info)
    assert (probed["width"], probed["height"]) == (info["width"], info["height"])

    # The pictures must decode without a single error.
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-v", "error", "-i", output, "-f", "null", "-"],
        stderr=subprocess.PIPE, check=True,
    )
    assert result.stderr == b"", result.stderr.decode()
    return output, info


def test_conversion_is_lossless_for_the_pictures():
    require_ffmpeg()
    with tempfile.TemporaryDirectory() as directory:
        source = os.path.join(directory, "sample.ave")
        stream = encode_stream(directory, "h264", 24, tag="lossless")
        info = make_sample.wrap(source, stream, chunk_frames=(12, 12),
                                width=64, height=48)
        output = os.path.join(directory, "sample.mp4")
        ave2mp4.convert(source, output=output, verbosity=0)

        # The expected NAL units are taken from the stream that went into the
        # fixture, not from ave2mp4's own extraction.
        expected = nals(stream)

        with tempfile.TemporaryDirectory() as scratch:
            rewritten_path = os.path.join(scratch, "rewritten.h264")
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-v", "error", "-y", "-i", output,
                 "-c", "copy", "-bsf:v", "h264_mp4toannexb", "-f", "h264",
                 rewritten_path],
                check=True,
            )
            with open(rewritten_path, "rb") as handle:
                rewritten = nals(handle.read())
        assert len(rewritten) == len(expected), (len(rewritten), len(expected))
        assert all(a == b for a, b in zip(expected, rewritten))
        assert probe(output)["frames"] == info["frames"]


TAGGED_SOURCE = "color=black:s=64x48:r=25"


def tagged_stream(frames=30, bframes=False, directory=None, codec="h264"):
    """Encode a stream in which picture n is a solid colour that encodes n.

    The average colour of a decoded picture then *is* its number, which makes
    the presentation order of a file directly observable.
    """
    holder = tempfile.TemporaryDirectory() if directory is None else None
    target = directory or holder.name
    path = elementary_path(target, "tagged", codec)
    command = [
        "ffmpeg", "-hide_banner", "-v", "error", "-y",
        "-f", "lavfi", "-i", TAGGED_SOURCE,
        "-vf", "geq=r='N*8':g='0':b='0'",
        "-frames:v", str(frames),
        "-c:v", make_sample.CODEC_ENCODERS[codec], "-preset", "veryfast",
        "-pix_fmt", "yuv420p",
        "-bf", "2" if bframes else "0", "-g", str(frames),
    ]
    if codec == "h264":
        command += ["-x264-params", "keyint=%d:min-keyint=%d:log-level=none"
                    % (frames, frames)]
    else:
        command += ["-x265-params", "keyint=%d:min-keyint=%d:log-level=none"
                    % (frames, frames)]
    command += ["-f", codec, path]
    subprocess.run(command, check=True)
    with open(path, "rb") as handle:
        return handle.read()


def presentation_order(path):
    """The picture numbers of a file, in the order a player would show them.

    The frames are written to a file rather than a pipe: a pipe on Windows
    translates line endings and would corrupt binary output.
    """
    with tempfile.TemporaryDirectory() as directory:
        raw = os.path.join(directory, "order.rgb")
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-v", "error", "-y", "-i", path,
             "-vf", "scale=1:1", "-f", "rawvideo", "-pix_fmt", "rgb24", raw],
            check=True,
        )
        with open(raw, "rb") as handle:
            data = handle.read()
    return [round(data[index] / 8) for index in range(0, len(data), 3)]


def test_presentation_order_is_preserved():
    require_ffmpeg()
    with tempfile.TemporaryDirectory() as directory:
        source = os.path.join(directory, "tagged.ave")
        output = os.path.join(directory, "tagged.mp4")
        make_sample.wrap(source, tagged_stream(30, directory=directory),
                         chunk_frames=(12, 18), width=64, height=48)
        ave2mp4.convert(source, output=output, verbosity=0)
        assert presentation_order(output) == list(range(30))


def test_timestamps_start_at_zero_and_never_go_backwards():
    require_ffmpeg()
    with tempfile.TemporaryDirectory() as directory:
        source = os.path.join(directory, "plain.ave")
        output = os.path.join(directory, "plain.mp4")
        make_sample.build(source, chunk_frames=(20, 20), width=64, height=48)
        ave2mp4.convert(source, output=output, verbosity=0)
        stamps = frame_timestamps(output)
        assert len(stamps) == 40, len(stamps)
        presentation = [pts for pts, _dts in stamps]
        decoding = [dts for _pts, dts in stamps]
        assert presentation[0] == 0, presentation[:4]
        assert all(b > a for a, b in zip(decoding, decoding[1:])), decoding[:6]
        assert all(b > a for a, b in zip(presentation, presentation[1:]))
        # The interval comes from the recording index, not from a guess.
        step = presentation[1] - presentation[0]
        assert {b - a for a, b in zip(presentation, presentation[1:])} == {step}
        assert step == 21600, step


def test_bframes_are_refused_by_default():
    require_ffmpeg()
    with tempfile.TemporaryDirectory() as directory:
        source = os.path.join(directory, "tagged.ave")
        output = os.path.join(directory, "tagged.mp4")
        # A plain copy would shuffle a reordered stream; that must never
        # happen silently.
        make_sample.wrap(source, tagged_stream(30, bframes=True, directory=directory),
                         chunk_frames=(12, 18), bframes=True, width=64, height=48)
        try:
            ave2mp4.convert(source, output=output, verbosity=0)
        except ave2mp4.AveError as error:
            assert "B-frames" in str(error)
        else:
            raise AssertionError("a B-frame export must not be copied blindly")
        assert not os.path.exists(output)


def test_bframes_can_be_reencoded():
    require_ffmpeg()
    with tempfile.TemporaryDirectory() as directory:
        source = os.path.join(directory, "tagged.ave")
        output = os.path.join(directory, "tagged.mp4")
        make_sample.wrap(source, tagged_stream(30, bframes=True, directory=directory),
                         chunk_frames=(12, 18), bframes=True, width=64, height=48)
        ave2mp4.convert(source, output=output, verbosity=0, reencode=True)
        assert presentation_order(output) == list(range(30))


def test_conversion_without_bframes():
    require_ffmpeg()
    with tempfile.TemporaryDirectory() as directory:
        _convert_and_check(directory, bframes=False)


def test_conversion_with_bframes_reencoded():
    require_ffmpeg()
    with tempfile.TemporaryDirectory() as directory:
        source = os.path.join(directory, "bframes.ave")
        output = os.path.join(directory, "bframes.mp4")
        info = make_sample.build(source, chunk_frames=(40, 25), bframes=True)
        ave2mp4.convert(source, output=output, verbosity=0, reencode=True)
        probed = probe(output)
        assert probed["frames"] == info["frames"], (probed, info)
        assert abs(probed["duration"] - info["duration"]) < 0.3, (probed, info)


def test_conversion_works_without_ffprobe():
    """The frozen Windows build has no ffprobe; that must merely skip the
    checked summary line, not the validation of the file itself."""
    require_ffmpeg()
    with tempfile.TemporaryDirectory() as directory:
        source = os.path.join(directory, "sample.ave")
        output = os.path.join(directory, "sample.mp4")
        make_sample.build(source, chunk_frames=(8,))
        previous = ave2mp4.find_ffprobe
        ave2mp4.find_ffprobe = lambda ffmpeg=None: None
        try:
            ave2mp4.convert(source, output=output, verbosity=0)
        finally:
            ave2mp4.find_ffprobe = previous
        assert os.path.exists(output)
        assert probe(output)["frames"] == 8


# --------------------------------------------------------------------------
# output safety
# --------------------------------------------------------------------------


def test_existing_output_is_not_overwritten():
    require_ffmpeg()
    with tempfile.TemporaryDirectory() as directory:
        source = os.path.join(directory, "sample.ave")
        output = os.path.join(directory, "sample.mp4")
        make_sample.build(source, chunk_frames=(8,))
        ave2mp4.convert(source, output=output, verbosity=0)
        marker = os.path.getmtime(output)
        with open(output, "rb") as handle:
            before = handle.read()
        try:
            ave2mp4.convert(source, output=output, verbosity=0)
        except ave2mp4.AveError as error:
            assert "already exists" in str(error)
        else:
            raise AssertionError("an existing output must not be overwritten")
        assert os.path.getmtime(output) == marker
        with open(output, "rb") as handle:
            assert handle.read() == before


def test_a_failed_forced_conversion_preserves_the_previous_output():
    """--force used to delete the destination first, so an ffmpeg that failed
    afterwards left a truncated file where a good MP4 had been.

    The Python interpreter stands in for ffmpeg: it is a real executable on
    every platform, and handed ffmpeg's command line it exits non zero.
    """
    require_ffmpeg()
    with tempfile.TemporaryDirectory() as directory:
        source = os.path.join(directory, "sample.ave")
        output = os.path.join(directory, "sample.mp4")
        make_sample.build(source, chunk_frames=(8,))
        ave2mp4.convert(source, output=output, verbosity=0)
        with open(output, "rb") as handle:
            good = handle.read()
        assert len(good) > 4096
        try:
            ave2mp4.convert(source, output=output, force=True, verbosity=0,
                            ffmpeg_path=UNUSABLE_FFMPEG)
        except ave2mp4.AveError as error:
            assert "ffmpeg" in str(error), error
        else:
            raise AssertionError("a failing ffmpeg must be an error")
        with open(output, "rb") as handle:
            assert handle.read() == good, "the previous MP4 was damaged"
        leftovers = [name for name in os.listdir(directory)
                     if name.startswith(".sample.mp4")]
        assert not leftovers, "a temporary file was left behind: %s" % leftovers


def test_an_output_that_is_not_an_mp4_is_rejected():
    """What ffmpeg left behind is checked before it can replace anything."""
    with tempfile.TemporaryDirectory() as directory:
        junk = os.path.join(directory, "junk.mp4")
        with open(junk, "wb") as handle:
            handle.write(b"this is not an MP4 at all")
        try:
            ave2mp4.validate_output(junk, 8, None, ave2mp4.VERIFY_CHEAP)
        except ave2mp4.AveError as error:
            assert "not a usable MP4" in str(error), error
        else:
            raise AssertionError("a file that is not an MP4 must be rejected")
        empty = os.path.join(directory, "empty.mp4")
        open(empty, "wb").close()
        try:
            ave2mp4.validate_output(empty, 8, None, ave2mp4.VERIFY_CHEAP)
        except ave2mp4.AveError as error:
            assert "empty" in str(error), error
        else:
            raise AssertionError("an empty file must be rejected")


def test_an_output_that_is_not_an_mp4_never_replaces_a_good_one():
    """The same rejection through a real conversion, on the platforms where a
    scripted stand-in for ffmpeg is a runnable executable."""
    require_ffmpeg()
    rubbish = """#!/usr/bin/env python3
import sys
# Advertise what the converter checks before it runs anything, so the test
# reaches the behaviour it is about rather than the capability check.
if '-version' in sys.argv:
    print('ffmpeg version 8.1.1')
elif '-bsfs' in sys.argv:
    print('Bitstream Filters:')
    print('setts')
elif '-encoders' in sys.argv:
    print('Encoders:')
    print(' V....D libx264              libx264')
else:
    out = [a for a in sys.argv if a.endswith('.mp4')][-1]
    with open(out, 'wb') as handle:
        handle.write(b'this is not an MP4')
sys.exit(0)
"""
    with tempfile.TemporaryDirectory() as directory:
        source = os.path.join(directory, "sample.ave")
        output = os.path.join(directory, "sample.mp4")
        make_sample.build(source, chunk_frames=(8,))
        ave2mp4.convert(source, output=output, verbosity=0)
        with open(output, "rb") as handle:
            good = handle.read()
        failing = stub_binary(directory, "ffmpeg", rubbish)
        try:
            ave2mp4.convert(source, output=output, force=True, verbosity=0,
                            ffmpeg_path=failing)
        except ave2mp4.AveError as error:
            assert "not a usable MP4" in str(error), error
        else:
            raise AssertionError("a file that is not an MP4 must be rejected")
        with open(output, "rb") as handle:
            assert handle.read() == good


def test_an_output_with_fewer_pictures_than_extracted_is_rejected():
    """A real MP4 that holds only two of the pictures the export has is
    refused, because committing it would silently drop the rest."""
    require_ffmpeg()
    with tempfile.TemporaryDirectory() as directory:
        source = os.path.join(directory, "sample.ave")
        info = make_sample.build(source, chunk_frames=(16,))
        full = os.path.join(directory, "full.mp4")
        ave2mp4.convert(source, output=full, verbosity=0)
        assert probe(full)["frames"] == info["frames"]
        short = os.path.join(directory, "short.mp4")
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-v", "error", "-y", "-i", full,
             "-frames:v", "2", "-c", "copy", short], check=True)
        assert ave2mp4.mp4_box_types(short)[0] == b"ftyp"
        # The picture count is only compared when there is an ffprobe to
        # read it with; without one the output is only checked for being a
        # well formed MP4, and the converter says so.
        properties, _notes = ave2mp4.validate_output(
            short, info["frames"], None, ave2mp4.VERIFY_CHEAP)
        assert properties is None
        try:
            ave2mp4.validate_output(short, info["frames"],
                                    ave2mp4.find_ffprobe(),
                                    ave2mp4.VERIFY_CHEAP)
        except ave2mp4.AveError as error:
            assert "incomplete" in str(error), error
        else:
            raise AssertionError("a short output must be rejected")


def test_a_missing_ffmpeg_binary_is_a_clean_error():
    require_ffmpeg()
    with tempfile.TemporaryDirectory() as directory:
        source = os.path.join(directory, "sample.ave")
        make_sample.build(source, chunk_frames=(8,))
        try:
            ave2mp4.convert(source, output=os.path.join(directory, "x.mp4"),
                            verbosity=0,
                            ffmpeg_path=os.path.join(directory, "nothing"))
        except ave2mp4.AveError as error:
            assert "--ffmpeg" in str(error), error
        else:
            raise AssertionError("a missing --ffmpeg path must be an error")


def test_an_output_directory_that_cannot_be_created_is_a_clean_error():
    require_ffmpeg()
    with tempfile.TemporaryDirectory() as directory:
        source = os.path.join(directory, "sample.ave")
        make_sample.build(source, chunk_frames=(8,))
        # A regular file where a directory has to be, so creating the output
        # directory fails the same way on every platform.  chmod is no help
        # here: on Windows the owner may always write.
        blocker = os.path.join(directory, "not-a-directory")
        with open(blocker, "wb") as handle:
            handle.write(b"")
        try:
            ave2mp4.convert(source,
                            output=os.path.join(blocker, "sub", "x.mp4"),
                            verbosity=0)
        except ave2mp4.AveError as error:
            assert "cannot create" in str(error), error
        else:
            raise AssertionError("an unusable output directory must be an error")


def test_verify_full_and_verify_none_agree_on_the_frame_count():
    require_ffmpeg()
    with tempfile.TemporaryDirectory() as directory:
        source = os.path.join(directory, "sample.ave")
        make_sample.build(source, chunk_frames=(16,))
        counts = []
        for level in (ave2mp4.VERIFY_CHEAP, ave2mp4.VERIFY_FULL,
                      ave2mp4.VERIFY_NONE):
            output = os.path.join(directory, "v-%s.mp4" % level)
            ave2mp4.convert(source, output=output, verbosity=0, verify=level)
            counts.append(probe(output)["frames"])
        assert counts == [16, 16, 16], counts


def test_an_unknown_verify_level_is_refused():
    with tempfile.TemporaryDirectory() as directory:
        source = os.path.join(directory, "sample.ave")
        make_sample.container(source, [make_sample.synthetic_stream(4)],
                              [[(101, 4)]])
        try:
            ave2mp4.convert(source, output=os.path.join(directory, "o.mp4"),
                            verbosity=0, verify="exhaustive")
        except ave2mp4.AveError as error:
            assert "verify" in str(error), error
        else:
            raise AssertionError("an unknown --verify level must be refused")


def test_a_verification_failure_is_reported_and_nothing_is_written():
    require_ffmpeg()
    with tempfile.TemporaryDirectory() as directory:
        source = os.path.join(directory, "sample.ave")
        make_sample.build(source, chunk_frames=(8,))
        # A stand-in ffprobe that reports no video stream at all.
        blind = stub_binary(directory, "ffprobe", """#!/usr/bin/env python3
import sys
if '-count_frames' in sys.argv:
    print('{"streams":[{"width":320,"height":240,"nb_read_frames":"3",'
          '"duration":"1.0"}]}')
else:
    print('{"streams":[]}')
sys.exit(0)
""")
        previous = ave2mp4.find_ffprobe
        ave2mp4.find_ffprobe = lambda ffmpeg=None: blind
        try:
            try:
                ave2mp4.convert(source, output=os.path.join(directory, "v.mp4"),
                                verbosity=0, verify=ave2mp4.VERIFY_FULL)
            except ave2mp4.AveError as error:
                assert "decodes to 3 pictures" in str(error), error
            else:
                raise AssertionError("a verification failure must be an error")
        finally:
            ave2mp4.find_ffprobe = previous
        assert not os.path.exists(os.path.join(directory, "v.mp4"))


def test_describe_output_survives_missing_metadata():
    """ffprobe says N/A for a duration it cannot work out; that must not be
    turned into a float() traceback."""
    with tempfile.TemporaryDirectory() as directory:
        source = os.path.join(directory, "sample.ave")
        make_sample.container(source, [make_sample.synthetic_stream(4)],
                              [[(101, 4)]])
        lie = stub_binary(directory, "ffprobe", """#!/usr/bin/env python3
print('{"streams":[{"width":64,"height":48,"nb_frames":"4",'
      '"duration":"N/A"}]}')
""")
        text = ave2mp4.describe_output(source, 1, lie)
        assert "N/A" not in text, text
        assert "unknown" in text, text
        # No streams at all, and no ffprobe at all, are both survivable.
        empty = stub_binary(directory, "ffprobe2", """#!/usr/bin/env python3
print('{"streams":[]}')
""")
        assert ave2mp4.describe_output(source, 1, empty) == ""
        assert ave2mp4.describe_output(source, 1, None) == ""
        assert ave2mp4.describe_output(source, 0, lie) == ""


# --------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------


def test_info_has_no_filesystem_side_effects():
    require_ffmpeg()
    with tempfile.TemporaryDirectory() as directory:
        source = os.path.join(directory, "sample.ave")
        make_sample.build(source, chunk_frames=(8,))
        outdir = os.path.join(directory, "should-not-exist")
        before = sorted(os.listdir(directory))
        assert ave2mp4.main(["--info", "-o", outdir, source]) == 0
        assert not os.path.exists(outdir), "--info created the output directory"
        assert sorted(os.listdir(directory)) == before, \
            "--info created or removed a file"


def test_dump_stream_follows_the_force_policy():
    require_ffmpeg()
    with tempfile.TemporaryDirectory() as directory:
        source = os.path.join(directory, "sample.ave")
        dump = os.path.join(directory, "dump.h264")
        make_sample.build(source, chunk_frames=(8,))

        assert ave2mp4.main([source, "-o", os.path.join(directory, "o1"),
                             "--dump-stream", dump, "-q"]) == 0
        with open(dump, "rb") as handle:
            data = handle.read()
        assert data.startswith(b"\x00\x00\x00\x01")
        assert ave2mp4.detect_codec(data) == "h264"

        with open(dump, "wb") as handle:
            handle.write(b"PRIOR CONTENT")
        assert ave2mp4.main([source, "-o", os.path.join(directory, "o2"),
                             "--dump-stream", dump, "-q"]) == 1
        with open(dump, "rb") as handle:
            assert handle.read() == b"PRIOR CONTENT", \
                "--dump-stream overwrote a file without --force"

        assert ave2mp4.main([source, "-o", os.path.join(directory, "o3"),
                             "--dump-stream", dump, "--force", "-q"]) == 0
        with open(dump, "rb") as handle:
            assert handle.read() != b"PRIOR CONTENT"


def test_cli_info_and_missing_file(capsys=None):
    require_ffmpeg()
    with tempfile.TemporaryDirectory() as directory:
        source = os.path.join(directory, "sample.ave")
        make_sample.build(source, chunk_frames=(8,))
        assert ave2mp4.main(["--info", source]) == 0
        assert ave2mp4.main([os.path.join(directory, "missing.ave")]) == 1
        assert ave2mp4.main([]) == 1


def test_cli_reports_a_broken_export_without_a_traceback():
    require_ffmpeg()
    with tempfile.TemporaryDirectory() as directory:
        source = os.path.join(directory, "broken.ave")
        with open(source, "wb") as handle:
            handle.write(b"\x00\x00\x00\x10avfs" + b"\x00" * 8)
        status = ave2mp4.main([source])
        assert status == 1


def test_quiet_still_reports_the_result():
    require_ffmpeg()
    with tempfile.TemporaryDirectory() as directory:
        source = os.path.join(directory, "sample.ave")
        output = os.path.join(directory, "sample.mp4")
        make_sample.build(source, chunk_frames=(8,))
        import io
        import contextlib
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            ave2mp4.convert(source, output=output, verbosity=1)
        text = captured.getvalue()
        assert "written" in text and "frames" in text, text
        assert "no B-frames" in text, text


# --------------------------------------------------------------------------
# memory
# --------------------------------------------------------------------------

MEASURE_SCRIPT = """
import os, resource, sys
sys.path.insert(0, %(root)r)
import ave2mp4
# ru_maxrss is a high water mark, and on some platforms a child inherits the
# high water mark of the process that spawned it.  The measuring process is
# therefore asked how much memory the work cost it, not what its peak was:
# the difference is the same number on every platform and is not polluted by
# whatever the process that built the fixture had allocated.
start = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
total = 0
length = 0
with ave2mp4.AveFile(sys.argv[1]) as export:
    for offset, header_len, _nal in export.iter_nals():
        total += 1
        length = offset + header_len + len(_nal)
end = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
if sys.platform == 'darwin':
    start //= 1024
    end //= 1024
print(total, length, max(0, end - start), end)
"""

# One extra process so the measuring process is not the one that built the
# fixture; harmless, and it keeps the inherited high water mark small.
LAUNCHER_SCRIPT = """
import subprocess, sys
subprocess.run([sys.executable] + sys.argv[1:], check=True)
"""


def _have_resource_module():
    """Whether this platform can report a peak resident set size at all."""
    if os.name == "nt":
        return False
    try:
        import resource
    except ImportError:
        return False
    return hasattr(resource, "getrusage")


def _measure(directory, path):
    """Read an export in a process of its own.

    Returns ``(nal units, bytes, growth in KB, absolute peak in KB)``, where
    the growth is what reading the file cost on top of the interpreter that
    was already running.
    """
    measure = os.path.join(directory, "measure.py")
    with open(measure, "w") as handle:
        handle.write(MEASURE_SCRIPT % {"root": ROOT})
    launcher = os.path.join(directory, "launch.py")
    with open(launcher, "w") as handle:
        handle.write(LAUNCHER_SCRIPT)
    result = subprocess.run(
        [sys.executable, launcher, measure, path],
        stdout=subprocess.PIPE, check=True)
    fields = result.stdout.decode().split()
    return (int(fields[0]), int(fields[1]), int(fields[2]), int(fields[3]))


def test_a_large_export_is_read_in_bounded_memory():
    """The memory a conversion needs must not grow with the size of the file.

    Two exports with the same chunk size but six times as many chunks are read
    in separate processes and what each read cost them is compared.  A reader
    that copied the recording would show six times as much for the larger one;
    a reader that walks it does not.
    """
    if not _have_resource_module():
        raise Skip("the resource module is not available on this platform")

    chunk_bytes = 2 << 20
    pictures = 24
    stream = make_sample.synthetic_stream(
        pictures, payload_size=chunk_bytes // pictures)
    expected_length = sum(
        4 + len(nal.rstrip(b"\x00"))
        for _start, nal in make_sample._split_nals(stream))
    results = {}
    with tempfile.TemporaryDirectory() as directory:
        for chunks in (4, 24):
            path = os.path.join(directory, "large-%d.ave" % chunks)
            make_sample.container(
                path, [stream] * chunks,
                [[(101, pictures)] for _ in range(chunks)])
            size = os.path.getsize(path)
            counted, length, growth, _peak = _measure(directory, path)
            # The count and the byte length are both checked: a reader that
            # dropped part of a window would still report the right number of
            # NAL units.
            assert counted == chunks * len(nals(stream)), counted
            assert length == chunks * expected_length, (length, expected_length)
            results[chunks] = (size, growth)
            del path

    small_size, small_growth = results[4]
    large_size, large_growth = results[24]
    assert large_size > small_size * 4, (small_size, large_size)
    # A reader that holds the recording in memory would need the whole file.
    assert large_growth < large_size, (large_growth, large_size)
    # ... and reading six times as much must not cost six times as much: the
    # memory follows the size of one window, not the size of the export.
    assert large_growth - small_growth < 8 * 1024, \
        "reading %d more bytes of export cost %d KB more" % (
            large_size - small_size, large_growth - small_growth)


# --------------------------------------------------------------------------
# real exports
# --------------------------------------------------------------------------


def test_a_real_export_from_the_environment():
    """Convert a real .ave, if one is pointed at.

    The procedure for checking an export from an Avigilon version nobody has
    tested is in the README under "Contributing".  Set AVE2MP4_REAL_SAMPLE to
    the path of an export to run it:

        AVE2MP4_REAL_SAMPLE="recording.ave" python3 tests/test_ave2mp4.py

    Nothing is uploaded anywhere; the file is only read.
    """
    path = os.environ.get("AVE2MP4_REAL_SAMPLE")
    if not path:
        raise Skip("AVE2MP4_REAL_SAMPLE is not set")
    require_ffmpeg()
    with tempfile.TemporaryDirectory() as directory:
        output = os.path.join(directory, "real.mp4")
        ave2mp4.convert(path, output=output, verbosity=1)
        with ave2mp4.AveFile(path) as export:
            codec, analysis = export.scan()
            index_frames = sum(chunk["frames"] for chunk in export.video_chunks())
        probed = probe(output)
        assert probed["codec"] == codec, (probed, codec)
        assert abs(probed["frames"] - analysis["pictures"]) <= max(
            len(export_video_spans(path)), 2), probed
        # Whatever it is, the conversion must be a clean refusal or a file
        # that holds every picture the export actually contains.
        assert probed["frames"] >= analysis["pictures"] - 2, probed
        del index_frames


def export_video_spans(path):
    with ave2mp4.AveFile(path) as export:
        return export.data_spans


# --------------------------------------------------------------------------
# runner
# --------------------------------------------------------------------------


def run():
    tests = sorted(
        (name, value) for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    )
    failures = []
    skipped = 0
    for name, test in tests:
        try:
            test()
        except Skip as error:
            skipped += 1
            print("skip %s (%s)" % (name, error))
        except Exception:
            failures.append(name)
            print("FAIL %s" % name)
            traceback.print_exc()
        else:
            print("ok   %s" % name)
    print("\n%d passed, %d failed, %d skipped, %d total"
          % (len(tests) - len(failures) - skipped, len(failures), skipped,
             len(tests)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(run())
