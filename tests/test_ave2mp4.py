#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for ave2mp4.

Runs with pytest, or directly:

    python3 tests/test_ave2mp4.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ave2mp4  # noqa: E402
import make_sample  # noqa: E402


class Skip(Exception):
    """Raised when a test needs a program that is not installed."""


def require_ffmpeg():
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise Skip("%s is not installed" % tool)


def probe(path):
    """Return {frames, duration, rate, width, height} of a video file."""
    result = subprocess.run(
        ["ffprobe", "-hide_banner", "-v", "error", "-select_streams", "v:0",
         "-count_frames", "-show_entries",
         "stream=width,height,nb_read_frames,duration,avg_frame_rate",
         "-of", "json", path],
        stdout=subprocess.PIPE, check=True,
    )
    import json

    stream = json.loads(result.stdout.decode())["streams"][0]
    return {
        "frames": int(stream["nb_read_frames"]),
        "duration": float(stream["duration"]),
        "rate": stream["avg_frame_rate"],
        "width": stream["width"],
        "height": stream["height"],
    }


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


def test_split_nals_handles_both_start_codes():
    stream = b"\x00\x00\x01\x67AA\x00\x00\x00\x01\x68BB"
    nals = [nal for _o, _h, nal in ave2mp4.split_nals(stream)]
    assert nals == [b"\x67AA", b"\x68BB"], nals


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
        export = ave2mp4.AveFile(path)
        chunks = export.video_chunks()
        assert len(chunks) == 2, chunks
        assert sum(chunk["frames"] for chunk in chunks) == info["frames"]
        rate, _note = export.frame_rate()
        assert rate == Fraction(25, 6), rate
        assert export.chunk_header_len == len(make_sample.CHUNK_HEADER)


# --------------------------------------------------------------------------
# end to end
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
        make_sample.build(source, chunk_frames=(24, 8))
        output = os.path.join(directory, "sample.mp4")
        ave2mp4.convert(source, output=output, verbosity=0)

        nals = lambda stream: [nal for _o, _h, nal in ave2mp4.split_nals(stream)]  # noqa: E731
        stored = nals(bytes(ave2mp4.AveFile(source).stream))

        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-v", "error", "-i", output,
             "-c", "copy", "-bsf:v", "h264_mp4toannexb", "-f", "h264", "-"],
            stdout=subprocess.PIPE, check=True,
        )
        rewritten = nals(result.stdout)
        assert len(rewritten) == len(stored), (len(rewritten), len(stored))
        assert all(a == b for a, b in zip(stored, rewritten))


def tagged_stream(frames=30, bframes=False):
    """Encode a stream in which picture n is a solid colour that encodes n.

    The average colour of a decoded picture then *is* its number, which makes
    the presentation order of a file directly observable.
    """
    command = [
        "ffmpeg", "-hide_banner", "-v", "error", "-y",
        "-f", "lavfi", "-i", "color=black:s=64x48:r=25",
        "-vf", "geq=r='N*8':g='0':b='0'",
        "-frames:v", str(frames),
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-bf", "2" if bframes else "0", "-g", str(frames),
        "-f", "h264", "-",
    ]
    return subprocess.run(command, stdout=subprocess.PIPE, check=True).stdout


def presentation_order(path):
    """The picture numbers of a file, in the order a player would show them."""
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-v", "error", "-i", path,
         "-vf", "scale=1:1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        stdout=subprocess.PIPE, check=True,
    )
    data = result.stdout
    return [round(data[index] / 8) for index in range(0, len(data), 3)]


def test_presentation_order_is_preserved():
    require_ffmpeg()
    with tempfile.TemporaryDirectory() as directory:
        source = os.path.join(directory, "tagged.ave")
        output = os.path.join(directory, "tagged.mp4")
        make_sample.wrap(source, tagged_stream(30), chunk_frames=(12, 18))
        ave2mp4.convert(source, output=output, verbosity=0)
        assert presentation_order(output) == list(range(30))


def test_bframes_are_refused_by_default():
    require_ffmpeg()
    with tempfile.TemporaryDirectory() as directory:
        source = os.path.join(directory, "tagged.ave")
        output = os.path.join(directory, "tagged.mp4")
        # A plain copy would shuffle a reordered stream; that must never
        # happen silently.
        make_sample.wrap(source, tagged_stream(30, bframes=True),
                         chunk_frames=(12, 18), bframes=True)
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
        make_sample.wrap(source, tagged_stream(30, bframes=True),
                         chunk_frames=(12, 18), bframes=True)
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


def test_existing_output_is_not_overwritten():
    require_ffmpeg()
    with tempfile.TemporaryDirectory() as directory:
        source = os.path.join(directory, "sample.ave")
        output = os.path.join(directory, "sample.mp4")
        make_sample.build(source, chunk_frames=(8,))
        ave2mp4.convert(source, output=output, verbosity=0)
        marker = os.path.getmtime(output)
        try:
            ave2mp4.convert(source, output=output, verbosity=0)
        except ave2mp4.AveError as error:
            assert "already exists" in str(error)
        else:
            raise AssertionError("an existing output must not be overwritten")
        assert os.path.getmtime(output) == marker


def test_cli_info_and_missing_file(capsys=None):
    require_ffmpeg()
    with tempfile.TemporaryDirectory() as directory:
        source = os.path.join(directory, "sample.ave")
        make_sample.build(source, chunk_frames=(8,))
        assert ave2mp4.main(["--info", source]) == 0
        assert ave2mp4.main([os.path.join(directory, "missing.ave")]) == 1
        assert ave2mp4.main([]) == 1


def test_dump_stream_writes_annexb():
    require_ffmpeg()
    with tempfile.TemporaryDirectory() as directory:
        source = os.path.join(directory, "sample.ave")
        dump = os.path.join(directory, "dump.h264")
        output = os.path.join(directory, "sample.mp4")
        make_sample.build(source, chunk_frames=(8,))
        ave2mp4.convert(source, output=output, dump_stream=dump, verbosity=0)
        with open(dump, "rb") as handle:
            data = handle.read()
        assert data.startswith(b"\x00\x00\x00\x01")
        assert ave2mp4.detect_codec(data) == "h264"


# --------------------------------------------------------------------------
# runner
# --------------------------------------------------------------------------


def run():
    tests = sorted(
        (name, value) for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    )
    failures = []
    for name, test in tests:
        try:
            test()
        except Skip as error:
            print("skip %s (%s)" % (name, error))
        except Exception:
            failures.append(name)
            print("FAIL %s" % name)
            traceback.print_exc()
        else:
            print("ok   %s" % name)
    print("\n%d passed, %d failed, %d total"
          % (len(tests) - len(failures), len(failures), len(tests)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(run())
