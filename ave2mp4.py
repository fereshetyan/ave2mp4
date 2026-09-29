#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ave2mp4 - convert Avigilon Unity Export (.ave) recordings to MP4.

The .ave container is a custom ISO-BMFF-like format that no general purpose
tool understands.  Inside it, however, the video is stored as a plain H.264
(or H.265) Annex-B elementary stream, so it can be remuxed into MP4 without
any re-encoding: the resulting video is bit-for-bit identical to the data in
the export.

See docs/FORMAT.md for the container layout and how it was reverse engineered.

Usage:
    ave2mp4 file.ave [file2.ave ...]
    ave2mp4 --info file.ave
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from fractions import Fraction

__version__ = "1.0.0"

#: 90 kHz is the clock the container uses for all durations.
TICKS_PER_SECOND = 90000

#: Ratio below which a trailing fragment is considered "not a picture".
MIN_PICTURE_BYTES = 4096

#: Boxes that carry the elementary stream and the recording index.
BOX_DATA = b"datp"
BOX_RECORD_INDEX = b"rcfc"

VIDEO_TRACK_HINT = 101


class AveError(Exception):
    """Raised for anything the converter cannot handle."""


# --------------------------------------------------------------------------
# box parsing
# --------------------------------------------------------------------------


def iter_boxes(data, start=0, end=None):
    """Yield (offset, size, type) tuples of the boxes in ``data[start:end]``."""
    if end is None:
        end = len(data)
    offset = start
    while offset + 8 <= end:
        size, box_type = struct.unpack_from(">I4s", data, offset)
        if size < 8 or offset + size > end:
            raise AveError(
                "corrupt box at offset %d (type %r, size %d)"
                % (offset, box_type, size)
            )
        yield offset, size, box_type
        offset += size


def read_varint(buf, pos):
    """Read a base-128 varint, return (value, new_position)."""
    result = 0
    shift = 0
    while True:
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise AveError("varint too long while parsing container index")


def parse_message(buf):
    """Yield (field, wire_type, value) for every field of a protobuf message."""
    pos = 0
    total = len(buf)
    while pos < total:
        key, pos = read_varint(buf, pos)
        field, wire = key >> 3, key & 7
        if field == 0:
            raise AveError("invalid protobuf field 0 while parsing container index")
        if wire == 0:
            value, pos = read_varint(buf, pos)
        elif wire == 2:
            length, pos = read_varint(buf, pos)
            value = buf[pos:pos + length]
            pos += length
        elif wire == 5:
            value = buf[pos:pos + 4]
            pos += 4
        elif wire == 1:
            value = buf[pos:pos + 8]
            pos += 8
        else:
            raise AveError("unsupported protobuf wire type %d" % wire)
        yield field, wire, value


def parse_index_message(payload):
    """Parse the protobuf of a ``sdat`` box, tolerating its 4 byte prefix."""
    for skip in (4, 0):
        try:
            fields = {}
            for field, _wire, value in parse_message(payload[skip:]):
                if field not in fields:
                    fields[field] = value
            if fields:
                return fields
        except (AveError, IndexError):
            continue
    raise AveError("cannot parse container index (unknown sdat layout)")


# --------------------------------------------------------------------------
# elementary stream helpers
# --------------------------------------------------------------------------


def split_nals(stream):
    """Split an Annex-B stream into [(offset, header_length, nal)]."""
    nals = []
    pos = 0
    total = len(stream)
    start = -1
    start_len = 0
    while pos + 3 <= total:
        if stream[pos] == 0 and stream[pos + 1] == 0:
            if stream[pos + 2] == 1:
                if start >= 0:
                    nals.append((start, start_len, stream[start + start_len:pos]))
                start, start_len = pos, 3
                pos += 3
                continue
            if pos + 4 <= total and stream[pos + 2] == 0 and stream[pos + 3] == 1:
                if start >= 0:
                    nals.append((start, start_len, stream[start + start_len:pos]))
                start, start_len = pos, 4
                pos += 4
                continue
        pos += 1
    if start >= 0:
        nals.append((start, start_len, stream[start + start_len:total]))
    return nals


def remove_emulation_prevention(data, limit=64):
    """Drop 00 00 03 escape bytes; only the first ``limit`` bytes matter."""
    out = bytearray()
    zeros = 0
    for byte in data[:limit]:
        if zeros >= 2 and byte == 0x03:
            zeros = 0
            continue
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return bytes(out)


class BitReader:
    """Minimal MSB-first bit reader for slice headers."""

    def __init__(self, data):
        self.data = data
        self.bit = 0

    def u(self, count):
        value = 0
        for _ in range(count):
            byte_index = self.bit >> 3
            if byte_index >= len(self.data):
                raise IndexError("out of data")
            value = (value << 1) | (
                (self.data[byte_index] >> (7 - (self.bit & 7))) & 1
            )
            self.bit += 1
        return value

    def ue(self):
        zeros = 0
        while self.u(1) == 0:
            zeros += 1
            if zeros > 32:
                raise IndexError("invalid exp-golomb code")
        return (1 << zeros) - 1 + (self.u(zeros) if zeros else 0)


def h264_slice_header(nal):
    """Return (first_mb_in_slice, slice_type) of an H.264 slice NAL."""
    reader = BitReader(remove_emulation_prevention(nal[1:]))
    return reader.ue(), reader.ue()


def hevc_first_slice_flag(nal):
    """Return True when an HEVC VCL NAL starts a new picture."""
    reader = BitReader(remove_emulation_prevention(nal[2:]))
    return reader.u(1) == 1


def detect_codec(stream):
    """Guess the codec from the first few NAL units."""
    for _offset, _header_len, nal in split_nals(stream[:4096]):
        if not nal:
            continue
        if nal[0] & 0x80:
            continue  # forbidden_zero_bit set: not a NAL header at all
        h264_type = nal[0] & 0x1F
        hevc_type = (nal[0] >> 1) & 0x3F
        if h264_type in (7, 8, 5, 1) and h264_type != 0:
            # SPS(7)/PPS(8)/IDR(5)/non-IDR(1) is far more likely than the
            # HEVC reading of the same byte (VPS/SPS/IDR_W_RADL/...), so
            # prefer H.264 unless the HEVC parameter set types show up.
            if h264_type in (7, 8):
                return "h264"
        if hevc_type in (32, 33, 34):
            return "hevc"
        if h264_type in (5, 1):
            return "h264"
    raise AveError("no recognizable video NAL units found (encrypted export?)")


def analyse_stream(stream, codec):
    """Count pictures, detect B-frames and locate the trailing fragment."""
    nals = split_nals(stream)
    vcl_types = (1, 2, 3, 4, 5) if codec == "h264" else tuple(range(0, 32))
    pictures = 0
    has_b_frames = False
    last_vcl_end = 0
    for offset, header_len, nal in nals:
        if not nal:
            continue
        if codec == "h264":
            nal_type = nal[0] & 0x1F
        else:
            nal_type = (nal[0] >> 1) & 0x3F
        if nal_type not in vcl_types:
            continue
        if len(nal) < header_len + 1:
            continue
        try:
            if codec == "h264":
                first_mb, slice_type = h264_slice_header(nal)
            else:
                first_mb = 0 if hevc_first_slice_flag(nal) else 1
                slice_type = 0
        except (IndexError, AveError):
            continue
        if codec == "h264" and slice_type in (1, 6):
            has_b_frames = True
        if first_mb == 0:
            pictures += 1
        last_vcl_end = offset + header_len + len(nal)
    if not pictures:
        raise AveError("no pictures found in the elementary stream")
    return {
        "nal_units": len(nals),
        "pictures": pictures,
        "has_b_frames": has_b_frames,
        "stream_end": last_vcl_end,
    }


# --------------------------------------------------------------------------
# container
# --------------------------------------------------------------------------


class AveFile:
    """A parsed Avigilon Unity Export file."""

    def __init__(self, path):
        self.path = path
        with open(path, "rb") as handle:
            self.data = handle.read()
        if len(self.data) < 16:
            raise AveError("file is too small to be an Avigilon export")
        if self.data[4:8] != b"avfs":
            raise AveError(
                "not an Avigilon export: missing 'avfs' signature "
                "(if this file was renamed, restore the .ave extension)"
            )
        self.boxes = list(iter_boxes(self.data))
        self.stream = bytearray()
        self.chunk_header_len = None
        self.tracks = []
        self._parse()

    # -- parsing ---------------------------------------------------------

    def _parse(self):
        payloads = []
        for offset, size, box_type in self.boxes:
            if box_type == BOX_DATA:
                payloads.append(self.data[offset + 8:offset + size])
            elif box_type == BOX_RECORD_INDEX:
                self._take_index(offset, size)
        if not payloads:
            raise AveError("no video data ('datp' boxes) found in the export")
        self.chunk_header_len = detect_chunk_header(payloads)
        if self.chunk_header_len is None:
            raise AveError(
                "video boxes do not contain a raw H.264/H.265 stream "
                "(password protected export?)"
            )
        # Split the NAL units of the whole recording at once: a chunk boundary
        # must never be able to cut a NAL unit in half.  Re-emitting them with
        # canonical start codes and without trailing zero padding makes the
        # result independent of where exactly the chunk header ends.
        body = b"".join(
            payload[self.chunk_header_len:] for payload in payloads
        )
        self.stream = bytearray()
        for _offset, _header_len, nal in split_nals(body):
            nal = nal.rstrip(b"\x00")
            if nal:
                self.stream += b"\x00\x00\x00\x01" + nal

    def _take_index(self, offset, size):
        for inner_off, inner_size, inner_type in iter_boxes(
            self.data, offset + 8, offset + size
        ):
            if inner_type != b"tkfc":
                continue
            for leaf_off, leaf_size, leaf_type in iter_boxes(
                self.data, inner_off + 8, inner_off + inner_size
            ):
                if leaf_type != b"sdat":
                    continue
                index = parse_index_message(self.data[leaf_off + 8:leaf_off + leaf_size])
                self.tracks.append(
                    {
                        "track_id": index.get(1),
                        "frames": index.get(2, 0),
                        "first_frame": index.get(3, 0),
                        "timestamp_ns": struct.unpack("<Q", index[4])[0]
                        if isinstance(index.get(4), bytes) and len(index[4]) == 8
                        else None,
                        "duration_ticks": index.get(6, 0),
                    }
                )

    # -- derived information ---------------------------------------------

    @property
    def video_tracks(self):
        """All index entries that actually carry frames."""
        return [track for track in self.tracks if track["frames"]]

    def video_chunks(self):
        """Chunks of the primary video track, in recording order."""
        candidates = self.video_tracks
        if not candidates:
            raise AveError("the export contains no indexed video frames")
        # The main video track is the one with the most frames overall.
        best = max(
            {track["track_id"] for track in candidates},
            key=lambda track_id: sum(
                t["frames"] for t in candidates if t["track_id"] == track_id
            ),
        )
        return [track for track in candidates if track["track_id"] == best]

    def frame_rate(self):
        """Return (rate, note) where rate is a Fraction in frames per second."""
        chunks = self.video_chunks()
        timed = [c for c in chunks if c["frames"] and c["duration_ticks"]]
        if not timed:
            raise AveError("the export does not describe any timing information")
        intervals = [
            Fraction(chunk["duration_ticks"], chunk["frames"]) for chunk in timed
        ]
        total_frames = sum(chunk["frames"] for chunk in chunks)
        total_ticks = sum(chunk["duration_ticks"] for chunk in chunks)

        if len(set(intervals)) == 1:
            interval = intervals[0]
            note = "constant, taken from the recording index"
        else:
            # The last chunk of an export is often cut short, so its
            # duration/frames ratio differs from the rest of the recording.
            counts = {}
            for value in intervals:
                counts[value] = counts.get(value, 0) + 1
            dominant = max(counts, key=lambda value: (counts[value], -value))
            if abs(total_ticks - total_frames * dominant) <= dominant:
                interval = dominant
                note = ("dominant interval; the index disagrees for some "
                        "chunks (the last one is usually cut short)")
            else:
                interval = Fraction(total_ticks, max(total_frames, 1))
                note = ("the index reports varying frame intervals, using the "
                        "average - playback speed is approximate")
        if interval <= 0:
            raise AveError("invalid frame interval in the recording index")
        return Fraction(TICKS_PER_SECOND, 1) / interval, note

    def first_timestamp(self):
        """Unix timestamp (ns, UTC) of the first recorded chunk, if known."""
        for chunk in sorted(self.video_chunks(), key=lambda c: c["first_frame"]):
            if chunk["timestamp_ns"]:
                return chunk["timestamp_ns"]
        return None

    def describe(self):
        """Human readable summary used by --info."""
        codec = detect_codec(bytes(self.stream))
        analysis = analyse_stream(bytes(self.stream), codec)
        rate, note = self.frame_rate()
        lines = ["file            : %s" % self.path]
        lines.append("size            : %.1f MB" % (len(self.data) / 1e6))
        lines.append("codec           : %s" % codec)
        lines.append("pictures        : %d (index says %d)"
                     % (analysis["pictures"],
                        sum(c["frames"] for c in self.video_chunks())))
        lines.append("B-frames        : %s" % ("yes" if analysis["has_b_frames"] else "no"))
        lines.append("frame rate      : %s fps (%s)" % (float(rate), note))
        lines.append("chunk header    : %s bytes" % self.chunk_header_len)
        stamp = self.first_timestamp()
        if stamp:
            lines.append("starts at       : %s (UTC)" % format_timestamp(stamp))
        lines.append("duration        : %s" % format_duration(
            sum(c["duration_ticks"] for c in self.video_chunks())
        ))
        lines.append("tracks          :")
        for index, track in enumerate(self.tracks):
            kind = "primary video" if track in self.video_chunks() else (
                "unused" if not track["frames"] else "additional stream"
            )
            lines.append(
                "  [%d] id=%-4s frames=%-5d first=%-5d duration=%s"
                % (index, track["track_id"], track["frames"],
                   track["first_frame"],
                   format_duration(track["duration_ticks"]))
            )
            lines[-1] += "  (%s)" % kind
        return "\n".join(lines)


#: NAL types that may legitimately open a stream (H.264 and H.265).
LEADING_NAL_TYPES_H264 = frozenset((1, 5, 6, 7, 8, 9))
LEADING_NAL_TYPES_HEVC = frozenset((0, 1, 19, 20, 21, 32, 33, 34, 35, 39))


def _is_leading_nal_header(byte):
    if byte == 0 or byte & 0x80:
        return False
    if (byte & 0x1F) in LEADING_NAL_TYPES_H264:
        return True
    return ((byte >> 1) & 0x3F) in LEADING_NAL_TYPES_HEVC


def find_stream_offset(payload, limit=4096):
    """Length of the chunk header in front of the first NAL unit.

    The header itself contains 00 00 00 01 padding, so a start code is only
    accepted when the byte after it looks like a NAL header that may open a
    stream.
    """
    end = min(limit, len(payload)) - 4
    pos = 0
    while pos < end:
        # The three byte form is checked first: a four byte start code also
        # matches the three byte pattern one byte later, and the shorter
        # reading keeps the header at its natural size.
        if payload[pos:pos + 3] == b"\x00\x00\x01":
            candidate = pos + 3
        elif payload[pos:pos + 4] == b"\x00\x00\x00\x01":
            candidate = pos + 4
        else:
            pos += 1
            continue
        if candidate < len(payload) and _is_leading_nal_header(payload[candidate]):
            return pos
        pos += 1
    return None


def detect_chunk_header(payloads):
    """Length of the header that precedes the stream in every video box.

    The last bytes of the header and the start code of the first NAL unit are
    both zero, so the boundary cannot be seen in a single payload.  Comparing
    two payloads does show it: they are identical up to the first differing
    byte, which is the NAL header.  The start code therefore ends there.
    """
    if len(payloads) > 1:
        reference = payloads[0]
        common = None
        for other in payloads[1:]:
            limit = min(len(reference), len(other))
            pos = 0
            while pos < limit and reference[pos] == other[pos]:
                pos += 1
            common = pos if common is None else min(common, pos)
        if common:
            for start_code_len in (3, 4):
                candidate = common - start_code_len
                if candidate >= 0 and reference[candidate:common] == (
                    b"\x00" * (start_code_len - 1) + b"\x01"
                ):
                    return candidate
    return find_stream_offset(payloads[0])


def format_duration(ticks):
    seconds = ticks / TICKS_PER_SECOND
    return "%d ticks (%.3f s)" % (ticks, seconds)


def format_timestamp(nanoseconds):
    import datetime

    return datetime.datetime.fromtimestamp(
        nanoseconds / 1e9, datetime.timezone.utc
    ).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


# --------------------------------------------------------------------------
# conversion
# --------------------------------------------------------------------------


def check_tools():
    missing = [tool for tool in ("ffmpeg", "ffprobe") if not shutil.which(tool)]
    if missing:
        raise AveError(
            "required program(s) not found in PATH: %s (install ffmpeg)"
            % ", ".join(missing)
        )


def convert(path, output=None, force=False, dump_stream=None, verbosity=1,
            reencode=False):
    """Convert a single .ave file; returns the path of the written MP4."""
    def say(message, level=1):
        if verbosity >= level:
            print(message)

    ave = AveFile(path)
    stream = bytes(ave.stream)
    codec = detect_codec(stream)
    analysis = analyse_stream(stream, codec)
    rate, rate_note = ave.frame_rate()

    index_frames = sum(chunk["frames"] for chunk in ave.video_chunks())
    extra_tracks = [
        track for track in ave.tracks
        if track["frames"] and track not in ave.video_chunks()
    ]

    say("%s" % path)
    say("  codec        : %s" % codec)
    say("  frame rate   : %s fps (%s)" % (float(rate), rate_note))
    say("  pictures     : %d in the stream, %d in the index"
        % (analysis["pictures"], index_frames))

    if analysis["pictures"] != index_frames:
        say("  warning      : stream and index disagree about the frame count",
            level=0)
    if extra_tracks:
        for track in extra_tracks:
            say("  warning      : track id %s holds %d frames that are not "
                "converted (audio is not supported yet)"
                % (track["track_id"], track["frames"]), level=0)

    if analysis["has_b_frames"]:
        # ffmpeg has to be told the display order of a reordered stream, and
        # the raw H.264 demuxer does not derive it: a plain remux shuffles the
        # pictures.  Never do that silently.
        if not reencode:
            raise AveError(
                "this export uses B-frames, and copying such a stream without "
                "its display order would shuffle the pictures. Re-run with "
                "--reencode to transcode instead (lossy), or report this "
                "export with the --info output so a lossless path can be "
                "added."
            )
        say("  warning      : re-encoding because the export uses B-frames - "
            "the result is no longer bit exact", level=0)
    else:
        say("  note         : no B-frames, ignoring the bogus reorder delay "
            "advertised in the bitstream")

    # Everything after the last picture is a partial frame cut off by the
    # export; feeding it to ffmpeg only produces a decoder error.
    usable = stream[:analysis["stream_end"]]
    dropped = len(stream) - len(usable)
    if dropped:
        say("  note         : dropping %d trailing bytes (%.1f%% of the "
            "stream) that do not form a picture"
            % (dropped, 100.0 * dropped / len(stream)))
        if dropped > MIN_PICTURE_BYTES:
            say("  warning      : that fragment is large enough to be a real "
                "frame; the export may be truncated", level=0)

    if dump_stream:
        with open(dump_stream, "wb") as handle:
            handle.write(usable)
        say("  elementary   : %s" % dump_stream)

    if output is None:
        output = os.path.splitext(path)[0] + ".mp4"
    if os.path.exists(output) and not force:
        raise AveError(
            "%s already exists (use --force to overwrite)" % output
        )

    temp_dir = tempfile.mkdtemp(prefix="ave2mp4-")
    elementary = os.path.join(temp_dir, "stream.h264")
    try:
        with open(elementary, "wb") as handle:
            handle.write(usable)

        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-r", "%d/%d" % (rate.numerator, rate.denominator),
            "-f", codec, "-i", elementary,
        ]
        if reencode:
            command += [
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                "-pix_fmt", "yuv420p", "-fps_mode", "passthrough",
            ]
        else:
            # Without B-frames presentation order is decode order, so the
            # packet index is the timestamp.  This also prevents ffmpeg from
            # hiding the first frames behind an edit list.
            command += ["-c", "copy", "-bsf:v", "setts=pts=N:dts=N"]
        command += [
            "-video_track_timescale", str(TICKS_PER_SECOND),
            "-movflags", "+faststart",
            output,
        ]
        result = subprocess.run(command, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        if result.returncode != 0:
            raise AveError(
                "ffmpeg failed:\n%s" % result.stderr.decode("utf-8", "replace").strip()
            )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    say("  written      : %s%s" % (output, describe_output(output, verbosity)))
    return output


def describe_output(path, verbosity=1):
    """Append a short verification of the produced file."""
    if verbosity < 1:
        return ""
    try:
        result = subprocess.run(
            ["ffprobe", "-hide_banner", "-v", "error", "-select_streams", "v:0",
             "-count_frames", "-show_entries",
             "stream=width,height,nb_read_frames,duration",
             "-of", "json", path],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True,
        )
        stream = json.loads(result.stdout.decode())["streams"][0]
    except (OSError, subprocess.CalledProcessError, ValueError, KeyError,
            IndexError):
        return ""
    return "\n                 %sx%s, %s frames, %.2f s" % (
        stream.get("width"), stream.get("height"),
        stream.get("nb_read_frames"), float(stream.get("duration", 0)),
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="ave2mp4",
        description="Convert Avigilon Unity Export (.ave) recordings to MP4 "
                    "without re-encoding.",
        epilog="Example: ave2mp4 *.ave --output-dir converted/",
    )
    parser.add_argument("files", nargs="*", metavar="FILE",
                        help="one or more .ave files")
    parser.add_argument("-o", "--output-dir", metavar="DIR",
                        help="directory for the MP4 files (default: next to "
                             "the .ave file)")
    parser.add_argument("-f", "--force", action="store_true",
                        help="overwrite existing MP4 files")
    parser.add_argument("-i", "--info", action="store_true",
                        help="only print what is inside the export")
    parser.add_argument("--dump-stream", metavar="PATH",
                        help="also write the raw elementary stream (debugging)")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="only print errors")
    parser.add_argument("--reencode", action="store_true",
                        help="transcode instead of copying (lossy); required "
                             "for exports that use B-frames")
    parser.add_argument("--version", action="version",
                        version="ave2mp4 %s" % __version__)
    args = parser.parse_args(argv)

    if not args.files:
        parser.print_help()
        return 1

    if args.output_dir and not os.path.isdir(args.output_dir):
        os.makedirs(args.output_dir, exist_ok=True)

    status = 0
    for path in args.files:
        try:
            if args.info:
                print(AveFile(path).describe())
                continue
            check_tools()
            output = None
            if args.output_dir:
                output = os.path.join(
                    args.output_dir,
                    os.path.splitext(os.path.basename(path))[0] + ".mp4",
                )
            convert(path, output=output, force=args.force,
                    dump_stream=args.dump_stream, reencode=args.reencode,
                    verbosity=0 if args.quiet else 1)
        except AveError as error:
            print("ave2mp4: %s: %s" % (path, error), file=sys.stderr)
            status = 1
        except OSError as error:
            print("ave2mp4: %s: %s" % (path, error), file=sys.stderr)
            status = 1
        except KeyboardInterrupt:
            return 130
    return status


if __name__ == "__main__":
    sys.exit(main())
