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
import errno
import json
import mmap
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from fractions import Fraction

__version__ = "1.1.0"

#: 90 kHz is the clock the container uses for all durations.
TICKS_PER_SECOND = 90000

#: Ratio below which a trailing fragment is considered "not a picture".
MIN_PICTURE_BYTES = 4096

#: Boxes that carry the elementary stream and the recording index.
BOX_DATA = b"datp"
BOX_RECORD_INDEX = b"rcfc"

#: Track id Avigilon uses for the video track, see docs/FORMAT.md section 5.
#: It is only a cross-check: the track is picked from the recording index, so
#: a recorder that numbers its tracks differently still works.
VIDEO_TRACK_HINT = 101

#: The start code every NAL unit is re-emitted with, see docs/FORMAT.md §7.
START_CODE = b"\x00\x00\x00\x01"

#: How much of the head of the stream codec detection looks at.  Every
#: encoder writes its parameter sets first, so this is generous.
CODEC_DETECT_BYTES = 1 << 16

#: How many votes one codec needs over the other to be accepted when both
#: are supported by the data.  Below this the two explanations are equally
#: good and the codec is reported as ambiguous instead of guessed.
CODEC_VOTE_MARGIN = 4

#: A NAL unit larger than this is not plausible, and a stream without any
#: start code in that many bytes is not an elementary stream at all.  It
#: bounds the memory the streaming splitter can use on a broken file.
MAX_CARRY_BYTES = 64 << 20
MAX_LEADING_GARBAGE = 4 << 20

#: Verification levels, see ``--verify``.
VERIFY_NONE = "none"
VERIFY_CHEAP = "cheap"
VERIFY_FULL = "full"
VERIFY_LEVELS = (VERIFY_NONE, VERIFY_CHEAP, VERIFY_FULL)


class AveError(Exception):
    """Raised for anything the converter cannot handle."""


# --------------------------------------------------------------------------
# box parsing
# --------------------------------------------------------------------------


def iter_boxes(data, start=0, end=None, allow_to_end=False):
    """Yield (offset, size, type) tuples of the boxes in ``data[start:end]``.

    ``size == 1`` is read as a 64 bit ``largesize``, as ISO-BMFF defines it.
    ``size == 0`` means "until the end of the range"; that is only accepted
    for MP4, where the format allows it, because accepting it for a damaged
    .ave file would silently swallow its tail.
    """
    if end is None:
        end = len(data)
    offset = start
    while offset + 8 <= end:
        size, box_type = struct.unpack_from(">I4s", data, offset)
        if size == 1:
            if offset + 16 > end:
                raise AveError("truncated 64 bit box size at offset %d (type %r)"
                               % (offset, box_type))
            size = struct.unpack_from(">Q", data, offset + 8)[0]
        elif size == 0 and allow_to_end:
            size = end - offset
        if size < 8 or offset + size > end:
            raise AveError(
                "corrupt box at offset %d (type %r, size %d)"
                % (offset, box_type, size)
            )
        yield offset, size, box_type
        offset += size


def payload_offset(data, offset):
    """Where the payload of the box at ``offset`` starts.

    Normally eight bytes in, but a box that uses a 64 bit ``largesize`` has a
    sixteen byte header, and a ``datp`` box that large does happen on a long
    recording.
    """
    if struct.unpack_from(">I", data, offset)[0] == 1:
        return offset + 16
    return offset + 8


def read_varint(buf, pos, end=None):
    """Read a base-128 varint, return (value, new_position)."""
    if end is None:
        end = len(buf)
    result = 0
    shift = 0
    while True:
        if pos >= end:
            raise AveError("truncated varint while parsing container index")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise AveError("varint too long while parsing container index")


#: Bytes occupied by the two fixed width protobuf wire types.
_FIXED_WIRE_SIZES = {1: 8, 5: 4}


def parse_message(buf, start=0, end=None):
    """Yield (field, wire_type, value) for every field of a protobuf message.

    Every field is bounds checked before its value is handed out, so a
    truncated or hand corrupted index is reported as a container problem
    instead of turning into a short slice or an out of range read further
    down.
    """
    pos = start
    if end is None:
        end = len(buf)
    while pos < end:
        key, pos = read_varint(buf, pos, end)
        field, wire = key >> 3, key & 7
        if field == 0:
            raise AveError("invalid protobuf field 0 while parsing container index")
        if wire == 0:
            value, pos = read_varint(buf, pos, end)
        elif wire == 2:
            length, pos = read_varint(buf, pos, end)
            if length > end - pos:
                raise AveError(
                    "protobuf field %d claims %d bytes but only %d are left "
                    "in the container index" % (field, length, end - pos)
                )
            value = bytes(buf[pos:pos + length])
            pos += length
        elif wire in _FIXED_WIRE_SIZES:
            width = _FIXED_WIRE_SIZES[wire]
            if end - pos < width:
                raise AveError(
                    "protobuf field %d is a %d byte field but only %d bytes "
                    "are left in the container index"
                    % (field, width, end - pos)
                )
            value = bytes(buf[pos:pos + width])
            pos += width
        else:
            raise AveError(
                "unsupported protobuf wire type %d in the container index "
                "(field %d)" % (wire, field)
            )
        yield field, wire, value


#: Names of the recording index fields, see docs/FORMAT.md section 5.
INDEX_FIELDS = {
    1: "track id",
    2: "picture count",
    3: "first picture",
    4: "chunk start time",
    5: "unknown",
    6: "chunk duration",
    7: "per picture index",
    8: "unknown",
}


def parse_index_message(payload):
    """Parse the protobuf of a ``sdat`` box, tolerating its 4 byte prefix."""
    for skip in (4, 0):
        try:
            fields = {}
            for field, _wire, value in parse_message(payload, skip):
                if field not in fields:
                    fields[field] = value
            if fields:
                return fields
        except AveError:
            continue
    raise AveError("cannot parse container index (unknown sdat layout)")


def index_varint(fields, number, default=0):
    """Read a varint index field, rejecting a wrongly typed one.

    The recording index is untrusted input.  A field that arrives with the
    wrong wire type used to be carried along as ``bytes`` and blew up much
    later with a bare ``TypeError`` from ``sum()``; it is refused here,
    where the index can still be described.
    """
    if number not in fields:
        return default
    value = fields[number]
    if not isinstance(value, int):
        raise AveError(
            "container index field %d (%s) is not a varint"
            % (number, INDEX_FIELDS.get(number, "unknown"))
        )
    return value


def index_bytes(fields, number, width=None):
    """Read a length delimited or fixed width index field, or return None."""
    if number not in fields:
        return None
    value = fields[number]
    if not isinstance(value, bytes):
        raise AveError(
            "container index field %d (%s) is not a byte field"
            % (number, INDEX_FIELDS.get(number, "unknown"))
        )
    if width is not None and len(value) != width:
        return None
    return value


# --------------------------------------------------------------------------
# elementary stream helpers
# --------------------------------------------------------------------------


def _start_code_positions(stream):
    """Offsets and lengths of every start code in an Annex-B byte string."""
    positions = []
    search_from = 0
    while True:
        found = stream.find(b"\x00\x00\x01", search_from)
        if found < 0:
            break
        # ``00 00 00 01`` and ``00 00 01`` are the same start code as far as
        # the NAL unit is concerned; only the reported header length differs.
        if found >= 1 and stream[found - 1] == 0:
            positions.append((found - 1, 4))
        else:
            positions.append((found, 3))
        search_from = found + 3
    return positions


def split_nals(stream):
    """Split an Annex-B stream into [(offset, header_length, nal)].

    ``bytes.find`` does the scanning, so this stays fast on a stream of
    hundreds of megabytes; a per byte loop in Python does not.
    """
    positions = _start_code_positions(stream)
    nals = []
    total = len(stream)
    for index, (start, header_len) in enumerate(positions):
        end = positions[index + 1][0] if index + 1 < len(positions) else total
        nals.append((start, header_len, stream[start + header_len:end]))
    return nals


def split_nals_stream(chunks):
    """Like :func:`split_nals`, but for a stream that arrives in pieces.

    ``chunks`` is an iterable of byte strings whose concatenation is the
    whole Annex-B stream.  A NAL unit that straddles a chunk boundary is
    still emitted as one unit, which is what the batch splitter does when it
    is handed the joined stream, but the memory used stays at one chunk plus
    one NAL unit instead of the whole recording.

    The yielded offsets are those of the *normalised* stream: every NAL unit
    is reported as if it had been re-emitted with a four byte start code and
    without its trailing zero padding (docs/FORMAT.md section 7).
    """
    carry = b""
    offset = 0
    started = False
    for chunk in chunks:
        if not chunk:
            continue
        buffer = carry + chunk if carry else chunk
        carry = b""
        positions = _start_code_positions(buffer)
        if not positions:
            carry = buffer
            if not started and len(carry) > MAX_LEADING_GARBAGE:
                raise AveError(
                    "no NAL unit start code in the first %d bytes of the video "
                    "data (password protected export?)" % len(carry)
                )
            if len(carry) > MAX_CARRY_BYTES:
                raise AveError("video data has no NAL unit start code at all")
            continue
        started = True
        for index, (start, header_len) in enumerate(positions):
            if index + 1 == len(positions):
                # The start code is kept with the NAL unit it introduces, so
                # a chunk boundary in the middle of a NAL unit - or between
                # its start code and its header byte - does not split it.
                carry = buffer[start:]
                break
            end = positions[index + 1][0]
            nal = buffer[start + header_len:end].rstrip(b"\x00")
            if nal:
                yield offset, 4, nal
                offset += len(START_CODE) + len(nal)
    if carry:
        header_len = _start_code_positions(carry)[0][1] if carry[:1] == b"\x00" else 0
        nal = carry[header_len:].rstrip(b"\x00")
        if nal:
            yield offset, 4, nal


def normalise_stream(chunks):
    """Return the normalised Annex-B stream of ``chunks`` as one bytes object.

    Only for tests and for small inputs: this materialises everything.
    """
    return b"".join(
        START_CODE + nal for _offset, _header_len, nal in split_nals_stream(chunks)
    )


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


#: H.264 NAL unit types that carry a coded slice of a picture.  The obsolete
#: slice data partitions (2, 3, 4) are excluded: they have a different syntax
#: and no encoder has produced one for many years.
H264_SLICE_TYPES = frozenset((1, 5))

#: H.264 NAL unit types that are written with ``nal_ref_idc == 0``, i.e. with a
#: header byte below 0x20.  No other type is legal there, so a header byte
#: below 0x20 that is not one of these cannot be H.264.
H264_REF0_TYPES = frozenset((6, 8, 9, 10, 12, 13, 19, 20))

#: HEVC NAL unit types that carry a coded slice of a picture.
HEVC_VCL_TYPES = frozenset(range(0, 10)) | frozenset(range(16, 22))

#: HEVC NAL unit types that are IRAP pictures, which carry one extra flag in
#: front of the slice header.
HEVC_IRAP_TYPES = frozenset(range(16, 24))

#: HEVC NAL unit types 32..35, whose payload has a fixed shape.
HEVC_PARAM_SET_TYPES = frozenset((32, 33, 34, 35))

#: Weights of the three independent kinds of evidence about a NAL unit.
VOTE_HEADER = 3
VOTE_PARAMETER_SET = 3
VOTE_SLICE = 2


def h264_slice_header(nal):
    """Return (first_mb_in_slice, slice_type) of an H.264 slice NAL.

    ``slice_type`` is the value as written, so 1 and 6 both mean a B slice
    (the two differ only in how the references are listed).
    """
    reader = BitReader(remove_emulation_prevention(nal[1:]))
    return reader.ue(), reader.ue()


#: H.265 7.4.7.1, table of NAL unit types whose slice header has no
#: ``slice_pic_order_cnt_lsb``: an IDR picture restarts the count.
HEVC_IDR_TYPES = frozenset((19, 20))


def _skip_profile_tier_level(reader, max_sub_layers_minus1):
    """Skip an H.265 profile_tier_level() structure, spec 7.3.3."""
    reader.u(8)                     # profile_space, tier_flag, profile_idc
    reader.u(32)                    # general_profile_compatibility_flag
    reader.u(48)                    # constraint flags and reserved bits
    reader.u(8)                     # general_level_idc
    sub_layer_flags = [
        (reader.u(1), reader.u(1))
        for _ in range(max_sub_layers_minus1)
    ]
    if max_sub_layers_minus1 > 0:
        for _ in range(8 - max_sub_layers_minus1):
            reader.u(1)             # reserved_zero_2bits
    for profile_present, level_present in sub_layer_flags:
        if profile_present:
            reader.u(88)
        if level_present:
            reader.u(8)


def parse_hevc_sps(nal):
    """The SPS fields needed to read an H.265 slice header, or None.

    Only the prefix of the SPS that comes before
    ``sps_pic_order_cnt_type`` is decoded; a value outside the range the
    specification allows means the parse went wrong, and None is returned so
    that the caller refuses the export instead of guessing.
    """
    reader = BitReader(remove_emulation_prevention(nal[2:]))
    try:
        reader.u(4)                          # sps_video_parameter_set_id
        max_sub_layers_minus1 = reader.u(3)  # sps_max_sub_layers_minus1
        reader.u(1)                          # sps_temporal_id_nesting_flag
        _skip_profile_tier_level(reader, max_sub_layers_minus1)
        reader.ue()                          # sps_seq_parameter_set_id
        chroma_format_idc = reader.ue()
        if chroma_format_idc == 3:
            reader.u(1)                      # separate_colour_plane_flag
        reader.ue()                          # pic_width_in_luma_samples
        reader.ue()                          # pic_height_in_luma_samples
        if reader.u(1):                      # conformance_window_flag
            for _ in range(4):
                reader.ue()
        reader.ue()                          # bit_depth_luma_minus8
        reader.ue()                          # bit_depth_chroma_minus8
        log2_max_poc_lsb_minus4 = reader.ue()
        pic_order_cnt_type = reader.ue()
    except IndexError:
        return None
    if not 0 <= log2_max_poc_lsb_minus4 <= 12 or pic_order_cnt_type not in (0, 1, 2):
        return None
    return {
        "log2_max_pic_order_cnt_lsb": log2_max_poc_lsb_minus4 + 4,
        "pic_order_cnt_type": pic_order_cnt_type,
    }


def parse_hevc_pps(nal):
    """The PPS fields needed to read an H.265 slice header, or None."""
    reader = BitReader(remove_emulation_prevention(nal[2:]))
    try:
        reader.ue()                  # pps_pic_parameter_set_id
        reader.ue()                  # pps_seq_parameter_set_id
        dependent = reader.u(1)      # dependent_slice_segments_enabled_flag
        output_flag = reader.u(1)     # output_flag_present_flag
        num_extra = reader.u(3)      # num_extra_slice_header_bits
    except IndexError:
        return None
    return {
        "dependent_slice_segments_enabled_flag": dependent,
        "output_flag_present_flag": output_flag,
        "num_extra_slice_header_bits": num_extra,
    }


def hevc_slice_header(nal, pps=None, sps=None):
    """Return (first_slice_segment_in_pic_flag, slice_type, poc_lsb).

    ``slice_type`` is None for a segment that does not start a picture: such a
    segment repeats the slice type of the first segment of the picture behind a
    ``slice_segment_address`` whose length can only be read from the SPS, so it
    carries no information this tool needs.

    ``poc_lsb`` is the picture order count low bits, or None when they are not
    present: IDR pictures restart the count and carry none, and the value can
    only be read when the SPS has been decoded.

    ``slice_type`` is returned for completeness but must not be used to decide
    whether the stream is reordered.  H.265 keeps the H.264 name "B slice" for
    what a P picture also is, and gives 1 (C) to the remaining inter slices;
    libx265 writes 1 for a plain P picture and 0 for a B one.  The value
    therefore does not distinguish the two orderings.  ``pps`` is only needed
    for ``num_extra_slice_header_bits`` and ``output_flag_present_flag``,
    which shift the picture order count by a few bits.
    """
    reader = BitReader(remove_emulation_prevention(nal[2:]))
    nal_type = (nal[0] >> 1) & 0x3F
    first = reader.u(1) == 1
    if nal_type in HEVC_IRAP_TYPES:
        reader.u(1)                      # no_output_of_prior_pics_flag
    reader.ue()                          # slice_pic_parameter_set_id
    if not first:
        return first, None, None
    num_extra = pps["num_extra_slice_header_bits"] if pps else 0
    output_flag = pps["output_flag_present_flag"] if pps else False
    reader.u(num_extra)                  # slice_reserved_flag[]
    slice_type = reader.ue()
    if output_flag:
        reader.u(1)                       # pic_output_flag
    poc_lsb = None
    if nal_type not in HEVC_IDR_TYPES and sps:
        try:
            poc_lsb = reader.u(sps["log2_max_pic_order_cnt_lsb"])
        except IndexError:
            poc_lsb = None
    return first, slice_type, poc_lsb


def poc_is_in_decode_order(pocs, poc_bits):
    """True when a run of picture order counts only ever steps forwards.

    The picture order count is what decides the order a player shows the
    pictures in, so a decode order whose counts advance by one constant step
    is by definition also the display order, and copying the stream needs no
    reordering information.  Any other step, in particular a backwards one,
    means ffmpeg has to be told the display order or the pictures end up
    shuffled.  The comparison is modular because the count is transmitted with
    a limited number of bits and wraps during a long recording.
    """
    if len(pocs) < 2:
        return True
    modulus = 1 << poc_bits
    steps = {
        (pocs[index + 1] - pocs[index]) % modulus
        for index in range(len(pocs) - 1)
    }
    return len(steps) == 1 and 0 not in steps


# --------------------------------------------------------------------------
# codec detection
# --------------------------------------------------------------------------


def _looks_like_h264_sps(nal):
    """True when the payload has the shape of an H.264 SPS.

    The SPS payload is ``profile_idc``, one byte of constraint flags and
    ``level_idc``, then ``seq_parameter_set_id``.  Reading the SPS in an HEVC
    stream is impossible because the header byte 0x67 means NAL unit type 51,
    which is reserved.
    """
    if len(nal) < 5:
        return False
    level_idc = nal[3]
    if not 10 <= level_idc <= 62:
        return False
    reader = BitReader(remove_emulation_prevention(nal[4:]))
    try:
        return 0 <= reader.ue() <= 31
    except IndexError:
        return False


def _looks_like_h264_pps(nal):
    """True when the payload has the shape of an H.264 PPS.

    ``pic_parameter_set_id`` and ``seq_parameter_set_id`` are exp-Golomb 0,
    i.e. a single one bit each, and ``num_slice_groups_minus1`` is another
    one bit, so bits 7, 6 and 3 of the first payload byte are always set.
    """
    return len(nal) >= 4 and 0x68 <= nal[1] <= 0x7F


def _looks_like_hevc_sps(nal):
    """True when the payload has the shape of an HEVC SPS.

    The two byte HEVC NAL unit header is followed by four bits of
    ``sps_video_parameter_set_id``, so the first payload byte is 0x00..0x0F.
    An H.264 payload byte is a ``profile_idc``, and the lowest defined value
    is 44, so the two cannot be confused.
    """
    return len(nal) >= 3 and nal[2] <= 0x0F


def _looks_like_hevc_pps(nal):
    """True when the payload has the shape of an HEVC PPS.

    ``pps_pic_parameter_set_id`` and ``pps_seq_parameter_set_id`` are
    exp-Golomb 0, so bits 7 and 6 of the first payload byte are set.
    """
    return len(nal) >= 3 and (nal[2] & 0xC0) == 0xC0


def _looks_like_hevc_vps(nal):
    """True when the payload has the shape of an HEVC VPS.

    ``vps_video_parameter_set_id`` is 0 and ``nuh_layer_id`` is 0 in every
    single layer stream, and ``nuh_temporal_id_plus1`` is 1..7 by
    specification, which already excludes most bytes an H.264 payload can
    start with.
    """
    if len(nal) < 4:
        return False
    if (nal[1] & 0x3C) != 0 or (nal[1] & 0x07) == 0:
        return False
    return (nal[2] >> 4) == 0


def _looks_like_hevc_aud(nal):
    """True when the NAL unit has the length of an HEVC access unit delimiter.

    An AUD is the two byte NAL unit header plus ``primary_pic_type`` and the
    rbsp trailing bits: three bytes in total, and nothing else.
    """
    return len(nal) == 3


def _looks_like_hevc_slice(nal):
    """True when the NAL unit parses as an H.265 slice header.

    This is a plausibility test for codec detection, not a reading of the
    slice type: the parameter sets are not available here, so it only checks
    that the exp-Golomb codes in front of ``slice_type`` are well formed.
    """
    if len(nal) < 4:
        return False
    try:
        _first, slice_type, _poc = hevc_slice_header(nal)
    except IndexError:
        return False
    return slice_type is not None and 0 <= slice_type <= 2


def _looks_like_h264_slice(nal):
    """True when the NAL unit parses as an H.264 slice header."""
    if len(nal) < 3:
        return False
    try:
        first_mb, slice_type = h264_slice_header(nal)
    except IndexError:
        return False
    return first_mb <= 1023 and 0 <= slice_type <= 9


def nal_codec_evidence(nal):
    """How strongly one NAL unit supports H.264 and how strongly HEVC.

    Returns ``{"h264": votes, "hevc": votes}``.  Three independent kinds of
    evidence are used, because the two NAL unit header spaces overlap:

    * which of the two type spaces the header byte falls into, including the
      types each specification reserves and therefore forbids,
    * whether the NAL unit parses as a parameter set of that codec,
    * whether it parses as a slice header of that codec.

    A NAL unit that supports neither codec contributes nothing, which is what
    lets an SEI or a filler unit sit in either stream without deciding it.
    """
    votes = {"h264": 0, "hevc": 0}
    if not nal or nal[0] & 0x80:
        return votes                       # forbidden_zero_bit: not a header
    header = nal[0]
    h264_type = header & 0x1F
    hevc_type = (header >> 1) & 0x3F

    # A header byte below 0x20 means nal_ref_idc == 0 in H.264, where only a
    # handful of types are legal.  Everywhere else in that range the H.265
    # reading is a valid VCL NAL unit type and the H.264 one is not.
    if header < 0x20:
        if h264_type in H264_REF0_TYPES:
            if _looks_like_h264_slice(nal) and h264_type in H264_SLICE_TYPES:
                votes["h264"] += VOTE_SLICE
            elif h264_type == 7 and _looks_like_h264_sps(nal):
                votes["h264"] += VOTE_PARAMETER_SET
            elif h264_type == 8 and _looks_like_h264_pps(nal):
                votes["h264"] += VOTE_PARAMETER_SET
            elif h264_type in (6, 9, 10, 12, 13, 19, 20):
                # SEI, AUD, filler, SPS extension: none of these exist at
                # these header values in H.265.
                votes["h264"] += VOTE_HEADER
        elif hevc_type in HEVC_VCL_TYPES and _looks_like_hevc_slice(nal):
            votes["hevc"] += VOTE_SLICE + VOTE_HEADER
        return votes

    # 0x40..0x47 is H.265 VPS/SPS/PPS/AUD against the H.264 types 0..7.  The
    # H.264 slice headers 0x41..0x45 therefore also read as H.265 parameter
    # sets, which is exactly how an H.264 export used to be read as H.265 and
    # had most of its video thrown away.  The parameter set payload decides.
    if 0x40 <= header <= 0x47:
        if hevc_type == 32 and _looks_like_hevc_vps(nal):
            votes["hevc"] += VOTE_PARAMETER_SET
        elif hevc_type == 33 and _looks_like_hevc_sps(nal):
            votes["hevc"] += VOTE_PARAMETER_SET
        elif hevc_type == 34 and _looks_like_hevc_pps(nal):
            votes["hevc"] += VOTE_PARAMETER_SET
        elif hevc_type == 35 and _looks_like_hevc_aud(nal):
            votes["hevc"] += VOTE_PARAMETER_SET
        if h264_type in H264_SLICE_TYPES and _looks_like_h264_slice(nal):
            votes["h264"] += VOTE_SLICE
        elif h264_type == 7 and _looks_like_h264_sps(nal):
            votes["h264"] += VOTE_PARAMETER_SET
        elif h264_type == 8 and _looks_like_h264_pps(nal):
            votes["h264"] += VOTE_PARAMETER_SET
        elif h264_type == 0:
            # NAL unit type 0 is not defined in H.264 at all.
            votes["hevc"] += VOTE_HEADER
        return votes

    # 0x20..0x3F is H.265 VCL against H.264 types 0..31, and 0x48..0x5F is
    # the H.265 filler/SEI range against H.264 types 8..31.  Both sides have
    # legal types here, so only the structure of the NAL unit is allowed to
    # decide, and a reserved type on one side counts against it.
    if header < 0x60:
        if h264_type in H264_SLICE_TYPES and _looks_like_h264_slice(nal):
            votes["h264"] += VOTE_SLICE
        elif h264_type == 7 and _looks_like_h264_sps(nal):
            votes["h264"] += VOTE_PARAMETER_SET
        elif h264_type == 8 and _looks_like_h264_pps(nal):
            votes["h264"] += VOTE_PARAMETER_SET
        elif h264_type in (6, 9, 10, 12, 13, 19, 20):
            votes["h264"] += VOTE_HEADER
        if hevc_type in HEVC_VCL_TYPES and _looks_like_hevc_slice(nal):
            votes["hevc"] += VOTE_SLICE
        elif hevc_type in HEVC_PARAM_SET_TYPES and nal[1:2] == b"\x01":
            # nuh_layer_id == 0 and nuh_temporal_id_plus1 == 1, which an H.264
            # payload byte satisfies by accident in a quarter of the cases.
            votes["hevc"] += VOTE_HEADER
        return votes

    # 0x60..0x67: the H.265 reading is a reserved NAL unit type, and every
    # H.264 type in that range is legal.
    if header <= 0x67 and h264_type != 0:
        votes["h264"] += VOTE_HEADER
        if h264_type in H264_SLICE_TYPES and _looks_like_h264_slice(nal):
            votes["h264"] += VOTE_SLICE
        elif h264_type == 7 and _looks_like_h264_sps(nal):
            votes["h264"] += VOTE_PARAMETER_SET
    return votes


def detect_codec(stream, limit=CODEC_DETECT_BYTES):
    """Return ``"h264"`` or ``"hevc"`` for an Annex-B elementary stream.

    A single NAL unit header byte is not enough: the H.264 and the H.265
    header spaces overlap, so every H.264 slice header 0x41..0x45 also reads
    as a valid H.265 VPS, SPS, PPS or AUD header.  The head of the stream is
    therefore scored against both codecs and only a codec with no support at
    all for the other, or with a clear margin, is accepted.  Anything else is
    an error, because a wrong guess silently truncates the stream.
    """
    totals = {"h264": 0, "hevc": 0}
    seen = 0
    for _offset, _header_len, nal in split_nals(stream[:limit]):
        if not nal:
            continue
        seen += 1
        votes = nal_codec_evidence(nal)
        totals["h264"] += votes["h264"]
        totals["hevc"] += votes["hevc"]
    if not seen:
        raise AveError(
            "no recognizable video NAL units found (encrypted export?)"
        )
    h264_votes, hevc_votes = totals["h264"], totals["hevc"]
    if hevc_votes == 0 and h264_votes > 0:
        return "h264"
    if h264_votes == 0 and hevc_votes > 0:
        return "hevc"
    if h264_votes == 0 and hevc_votes == 0:
        raise AveError(
            "cannot tell whether the video is H.264 or H.265: none of the "
            "first %d NAL units parse as either (encrypted export?)" % seen
        )
    winner = "h264" if h264_votes > hevc_votes else "hevc"
    loser = "hevc" if winner == "h264" else "h264"
    if totals[winner] - totals[loser] >= CODEC_VOTE_MARGIN:
        return winner
    raise AveError(
        "cannot tell whether the video is H.264 or H.265: the NAL units in "
        "the first %d bytes of the stream support both codecs (H.264 %d, "
        "H.265 %d). Refusing to guess, because the wrong codec would "
        "truncate the video" % (min(limit, len(stream)), h264_votes, hevc_votes)
    )


# --------------------------------------------------------------------------
# stream analysis
# --------------------------------------------------------------------------


def _new_analysis():
    return {
        "nal_units": 0,
        "pictures": 0,
        "has_b_frames": False,
        "reordering": "no",
        "unparsed_slices": 0,
        "last_vcl_end": 0,
        "first_unparsed": None,
        # H.265 only: the picture order counts seen so far, split into runs
        # that an IDR picture restarts, plus whether any of them was unreadable.
        "poc_runs": [[]],
        "poc_unknown": 0,
        "poc_bits": None,
        "hevc_pps": None,
    }


def _accumulate(analysis, offset, header_len, nal, codec):
    """Fold one NAL unit into the running stream analysis."""
    analysis["nal_units"] += 1
    if not nal or nal[0] & 0x80:
        return
    if codec == "h264":
        nal_type = nal[0] & 0x1F
        if nal_type not in H264_SLICE_TYPES:
            return
    else:
        nal_type = (nal[0] >> 1) & 0x3F
        if nal_type == 32:                     # VPS, not needed
            return
        if nal_type == 33:                     # SPS
            sps = parse_hevc_sps(nal)
            if sps and analysis["poc_bits"] is None:
                analysis["poc_bits"] = sps["log2_max_pic_order_cnt_lsb"]
            return
        if nal_type == 34:                     # PPS
            analysis["hevc_pps"] = parse_hevc_pps(nal)
            return
        if nal_type not in HEVC_VCL_TYPES:
            return
    if len(nal) < 2:
        # A NAL unit is a header byte plus at least one payload byte.  The
        # start code is not part of the NAL unit and its length says nothing
        # about this.
        return
    try:
        if codec == "h264":
            first_mb, slice_type = h264_slice_header(nal)
            if not 0 <= slice_type <= 9:
                raise IndexError("implausible slice_type")
        else:
            pps = analysis.get("hevc_pps")
            first_slice, slice_type, poc = hevc_slice_header(
                nal, pps, {"log2_max_pic_order_cnt_lsb": analysis["poc_bits"]}
                if analysis["poc_bits"] else None
            )
            if slice_type is None:
                if first_slice:
                    raise IndexError("unreadable first slice segment")
                return
            if not 0 <= slice_type <= 2:
                raise IndexError("implausible slice_type")
    except IndexError:
        analysis["unparsed_slices"] += 1
        if analysis["first_unparsed"] is None:
            analysis["first_unparsed"] = offset
        return
    analysis["last_vcl_end"] = offset + header_len + len(nal)
    if codec == "h264":
        if first_mb == 0:
            analysis["pictures"] += 1
        if slice_type in (1, 6) and analysis["reordering"] == "no":
            # A B slice can only exist if the pictures are decoded in a
            # different order than they are displayed.
            analysis["reordering"] = "yes"
            analysis["has_b_frames"] = True
    else:
        if first_slice:
            analysis["pictures"] += 1
            # The slice type is deliberately not used to decide this.  H.265
            # calls both P and B pictures a "B slice" and reserves 1 (C) for
            # the rest, and libx265 writes 1 for a plain P picture and 0 for a
            # B one, so the value says nothing about reordering.  The picture
            # order counts below do.
            if nal_type in HEVC_IDR_TYPES:
                # An IDR restarts the picture order count at zero.
                if analysis["poc_runs"][-1]:
                    analysis["poc_runs"].append([])
            elif poc is None:
                analysis["poc_unknown"] += 1
            else:
                analysis["poc_runs"][-1].append(poc)


def _finish(analysis, codec):
    if not analysis["pictures"]:
        raise AveError("no pictures found in the elementary stream")
    if (analysis["first_unparsed"] is not None
            and analysis["first_unparsed"] < analysis["last_vcl_end"]):
        # Everything after the last readable picture is thrown away, so a
        # picture that could not be parsed in the middle of the recording
        # silently removes everything behind it.  Refuse instead.
        raise AveError(
            "the elementary stream could not be parsed at byte %d of %d, so "
            "%d byte of video in the middle of the recording would be dropped. "
            "The export looks damaged; refusing to write a file with missing "
            "pictures"
            % (analysis["first_unparsed"], analysis["last_vcl_end"],
               analysis["last_vcl_end"] - analysis["first_unparsed"])
        )
    if codec == "hevc" and analysis["reordering"] == "no":
        _finish_hevc_reordering(analysis)
    analysis["stream_end"] = analysis["last_vcl_end"]
    analysis["codec"] = codec
    return analysis


def _finish_hevc_reordering(analysis):
    """Decide from the picture order counts whether H.265 is reordered.

    The order the pictures are shown in is the order of their picture order
    counts, so a decode order whose counts advance by one constant step is
    also the display order.  Anything else needs a display order that
    ffmpeg's raw H.265 demuxer does not work out, and copying the stream then
    shuffles the pictures.  When the counts cannot be read at all, for
    instance because the export has no SPS, the order is reported as unknown
    and the export is refused rather than converted on a guess.
    """
    if not analysis["poc_bits"]:
        if analysis["pictures"] > 1:
            analysis["reordering"] = "unknown"
        return
    if analysis["poc_unknown"]:
        analysis["reordering"] = "unknown"
        return
    for run in analysis["poc_runs"]:
        if not poc_is_in_decode_order(run, analysis["poc_bits"]):
            analysis["reordering"] = "yes"
            analysis["has_b_frames"] = True
            return


def analyse_nals(nals, codec):
    """Analyse an iterable of (offset, header_len, nal) tuples."""
    analysis = _new_analysis()
    for offset, header_len, nal in nals:
        _accumulate(analysis, offset, header_len, nal, codec)
    return _finish(analysis, codec)


def analyse_stream(stream, codec):
    """Count pictures, detect reordering and locate the trailing fragment."""
    return analyse_nals(split_nals(stream), codec)


def scan_stream(chunks, sink=None):
    """Detect the codec and analyse the stream in a single pass.

    ``chunks`` is an iterable of byte strings making up the whole Annex-B
    stream, ``sink`` (if given) a writable file that receives the normalised
    stream.  Returns ``(codec, analysis)``.

    The head of the stream is held back until the codec is known, which needs
    at most :data:`CODEC_DETECT_BYTES`, so nothing large is ever buffered.
    """
    analysis = _new_analysis()
    pending = []
    held = 0
    codec = None

    def accumulate(item):
        _accumulate(analysis, item[0], item[1], item[2], codec)

    for item in split_nals_stream(chunks):
        if codec is None:
            pending.append(item)
            held += len(START_CODE) + len(item[2])
        if sink is not None:
            sink.write(START_CODE + item[2])
        if codec is None:
            if held >= CODEC_DETECT_BYTES:
                codec = detect_codec(
                    b"".join(START_CODE + held_nal for _o, _h, held_nal in pending)
                )
                for held_item in pending:
                    accumulate(held_item)
                pending = []
            continue
        accumulate(item)
    if codec is None:
        codec = detect_codec(
            b"".join(START_CODE + held_nal for _o, _h, held_nal in pending)
        )
        for held_item in pending:
            accumulate(held_item)
    return codec, _finish(analysis, codec)


# --------------------------------------------------------------------------
# container
# --------------------------------------------------------------------------

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
    if ticks is None:
        return "unknown"
    seconds = ticks / TICKS_PER_SECOND
    return "%d ticks (%.3f s)" % (ticks, seconds)


def format_timestamp(nanoseconds):
    import datetime

    try:
        moment = datetime.datetime.fromtimestamp(
            nanoseconds / 1e9, datetime.timezone.utc
        )
    except (OverflowError, OSError, ValueError):
        return "out of range"
    return moment.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def format_seconds(seconds):
    return "unknown" if seconds is None else "%.2f s" % seconds


#: How many bytes of video data are read into memory at a time.  The container
#: stores the stream in ``datp`` boxes, and a recorder that writes a box per
#: 30 seconds of video produces boxes of many megabytes, so boxes are read in
#: windows of this size.  A NAL unit that straddles a window boundary is
#: carried over, which is what :func:`split_nals_stream` is for, so the window
#: size changes the memory a conversion needs and nothing else: measured on the
#: 57.7 MB reference export, parsing and extracting used 23 MB of resident
#: memory with a 1 MiB window and 29 MB with an 8 MiB one, for the same wall
#: clock time.
MAX_CHUNK_BYTES = 1 << 20


class AveFile:
    """A parsed Avigilon Unity Export file.

    The container is never copied into memory: it is memory mapped where the
    platform allows it, and the video is read one ``datp`` box at a time, so
    the peak memory of a conversion does not grow with the size of the
    recording.  Use it as a context manager, or call :meth:`close`.
    """

    def __init__(self, path):
        self.path = path
        self._handle = None
        self._mapping = None
        self._closed = False
        try:
            self._handle = open(path, "rb")
        except OSError as error:
            raise AveError("cannot read %s: %s" % (path, error.strerror or error))
        size = os.fstat(self._handle.fileno()).st_size
        if size < 16:
            self.close()
            raise AveError("file is too small to be an Avigilon export")
        try:
            # A read only mapping keeps the page cache in charge: the box
            # headers are read, the megabytes of video payload are not.
            self._mapping = mmap.mmap(self._handle.fileno(), 0,
                                      access=mmap.ACCESS_READ)
            self.data = self._mapping
        except (ValueError, OSError):
            # 32 bit builds cannot map a large file, and a few exotic
            # filesystems refuse; fall back to reading it.
            self.data = self._handle.read()
        if self.data[4:8] != b"avfs":
            self.close()
            raise AveError(
                "not an Avigilon export: missing 'avfs' signature "
                "(if this file was renamed, restore the .ave extension)"
            )
        self.size = size
        self._chunk_header_len = None
        self.tracks = []
        self.secondary_tracks = []
        self.track_id_note = None
        #: One entry per datp box: the track ids the index says it holds.
        self.chunk_tracks = []
        self.indexed = True
        try:
            self.boxes = list(iter_boxes(self.data))
            self._parse()
        except Exception:
            self.close()
            raise

    # -- resource handling ------------------------------------------------

    def close(self):
        """Release the memory map.  Idempotent."""
        if self._closed:
            return
        self._closed = True
        if self._mapping is not None:
            try:
                self._mapping.close()
            except (BufferError, ValueError):
                pass
            self._mapping = None
        if self._handle is not None:
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_exception):
        self.close()
        return False

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    # -- parsing ---------------------------------------------------------

    def _parse(self):
        """Read the box list, the recording index and the track mapping."""
        data_spans = []
        entries = []
        pending = []                     # datp boxes not yet attributed
        for offset, size, box_type in self.boxes:
            if box_type == BOX_DATA:
                head = payload_offset(self.data, offset)
                data_spans.append((head, size - (head - offset)))
                pending.append(len(data_spans) - 1)
            elif box_type == BOX_RECORD_INDEX:
                tracks = self._read_index(offset, size)
                entries.extend(tracks)
                if pending:
                    # One rcfc box follows the datp box it describes.
                    target = pending.pop()
                    self.chunk_tracks.append(
                        (target, [t["track_id"] for t in tracks if t["frames"]])
                    )
                else:
                    # An index with no chunk in front of it.  It still belongs
                    # to the recording, but it attributes no data.
                    self.chunk_tracks.append((None, []))
        if not data_spans:
            raise AveError("no video data ('datp' boxes) found in the export")
        self.data_spans = data_spans
        self.tracks = entries
        if not self.chunk_tracks:
            self.indexed = False
        self._select_video_track(data_spans)

    def _read_index(self, offset, size):
        """Return the track entries of one ``rcfc`` box, in file order."""
        entries = []
        head = payload_offset(self.data, offset)
        for inner_off, inner_size, inner_type in iter_boxes(
            self.data, head, offset + size
        ):
            if inner_type != b"tkfc":
                continue
            header_id = None
            sdat_payload = None
            inner_head = payload_offset(self.data, inner_off)
            for leaf_off, leaf_size, leaf_type in iter_boxes(
                self.data, inner_head, inner_off + inner_size
            ):
                if leaf_type == b"tkfh":
                    # docs/FORMAT.md section 5: the track id is written again
                    # at offset 2 of the 22 byte fixed track header.  It is
                    # only a cross check of the protobuf, never the source of
                    # the value: the layout is observed, not specified.
                    if leaf_size - (payload_offset(self.data, leaf_off)
                                    - leaf_off) >= 6:
                        header_id = struct.unpack_from(">I", self.data,
                                                       leaf_off + 10)[0]
                elif leaf_type == b"sdat":
                    leaf_head = payload_offset(self.data, leaf_off)
                    sdat_payload = self.data[leaf_head:leaf_off + leaf_size]
            if sdat_payload is None:
                continue
            index = parse_index_message(sdat_payload)
            track_id = index_varint(index, 1)
            frames = index_varint(index, 2)
            timestamp = index_bytes(index, 4, width=8)
            entry = {
                "track_id": track_id,
                "frames": frames,
                "first_frame": index_varint(index, 3),
                "timestamp_ns": struct.unpack("<Q", timestamp)[0]
                if timestamp else None,
                "duration_ticks": index_varint(index, 6),
                "header_track_id": header_id,
            }
            if header_id is not None and header_id != track_id:
                # Not fatal: the protobuf is structured and self describing,
                # the fixed header layout is only observed.  Report it so a
                # format change cannot pass unnoticed.
                entry["header_mismatch"] = True
            entries.append(entry)
        return entries

    def _select_video_track(self, data_spans):
        """Decide which ``datp`` boxes make up the video, and fail if unclear.

        The recording index names the tracks that contributed pictures to the
        chunk before it, which is the only deterministic mapping between an
        index entry and the bytes it describes.  Concatenating every ``datp``
        box instead mixes a second track into the video and doubles the
        pictures, so the mapping is resolved here, once, with an explicit error
        whenever it is not unambiguous:

        * exactly one track holds pictures: that track is the video and its
          chunks are extracted,
        * no track holds pictures at all: there is no index to map the boxes
          with, so all of them are used and a warning says so,
        * more than one track, a chunk that claims two tracks, or a chunk the
          index does not describe: nothing says which track is the video, so
          the export is refused rather than concatenated.
        """
        attributed = {}
        for chunk_index, track_ids in self.chunk_tracks:
            if chunk_index is None:
                continue
            attributed.setdefault(chunk_index, set()).update(track_ids)

        if not any(attributed.values()):
            # No index at all: there is nothing to map the boxes with.  The
            # original behaviour is kept for that case, loudly, because
            # refusing outright would break exports whose index the converter
            # never needed.
            self.indexed = False
            self.video_spans = list(data_spans)
            self.video_track_id = None
            return

        # A data box is attributed only if the index behind it names a track
        # that actually holds pictures.  An index that names no track at all
        # leaves the box unattributed, which is as unusable as a missing
        # index entry: it cannot be told apart from a second track.
        described = {index for index, ids in attributed.items() if ids}
        missing = sorted(set(range(len(data_spans))) - described)
        if missing:
            raise AveError(
                "chunk %d of %d carries data that the recording index does "
                "not describe, so it cannot be told apart from a second track. "
                "Refusing to concatenate video and non video data"
                % (missing[0] + 1, len(data_spans))
            )

        mixed = sorted(
            chunk_index for chunk_index, ids in attributed.items() if len(ids) > 1
        )
        if mixed:
            raise AveError(
                "chunk %d of %d claims pictures of more than one track (%s), so "
                "its data cannot be separated into a video and a second track. "
                "Refusing to write a file that would contain pictures of both"
                % (mixed[0] + 1, len(data_spans),
                   ", ".join(str(t) for t in sorted(attributed[mixed[0]])))
            )

        video_ids = set()
        for ids in attributed.values():
            video_ids.update(ids)
        if len(video_ids) == 1:
            self.video_track_id = video_ids.pop()
        else:
            self._choose_video_track_by_content(video_ids, data_spans,
                                                attributed)

        self.video_spans = [
            data_spans[index] for index in sorted(attributed)
            if self.video_track_id in attributed[index]
        ]
        for index, ids in sorted(attributed.items()):
            if ids and self.video_track_id not in ids:
                self.secondary_tracks.append(
                    {"track_id": sorted(ids)[0], "spans": [data_spans[index]]}
                )

    def _choose_video_track_by_content(self, video_ids, data_spans, attributed):
        """Pick the video track by what its data actually contains.

        More than one track claims pictures, and nothing in the index says
        which of them is the video.  The data itself does: a video track's
        boxes hold an H.264 or H.265 Annex-B stream and an audio track's hold
        something else, so each candidate is parsed and only the one that
        really carries a video stream is used.  If that does not single a
        track out, the export is refused - picking by track number would be a
        guess, and the track numbers come from one Avigilon version.
        """
        candidates = {}
        for index, ids in attributed.items():
            for track_id in ids:
                candidates.setdefault(track_id, []).append(data_spans[index])
        video_like = sorted(track_id for track_id, spans in candidates.items()
                            if self._holds_video_stream(spans))
        if len(video_like) != 1:
            raise AveError(
                "the recording index describes %d tracks with pictures (%s), and "
                "%d of them hold something that parses as an H.264/H.265 "
                "stream, so there is no way to tell which one carries the "
                "video. Only the video track is converted; refusing to guess"
                % (len(candidates),
                   ", ".join(str(t) for t in sorted(candidates)),
                   len(video_like))
            )
        self.video_track_id = video_like[0]
        if self.video_track_id != VIDEO_TRACK_HINT:
            self.track_id_note = (
                "the video track is %s here, not the %s Avigilon usually uses"
                % (self.video_track_id, VIDEO_TRACK_HINT)
            )

    def _holds_video_stream(self, spans):
        """Whether the data boxes of one track hold a video elementary stream.

        Reads a few kilobytes of the first box, so the cost does not depend on
        the size of the recording.
        """
        for offset, size in spans:
            prefix = bytes(self.data[offset:offset + 8192])
            header = detect_chunk_header([prefix])
            if header is None:
                return False
            try:
                detect_codec(prefix[header:])
            except AveError:
                return False
        return bool(spans)

    # -- video data ------------------------------------------------------

    def iter_chunks(self):
        """Yield the video track of every ``datp`` box, chunk header removed.

        One window at a time, so the memory used does not depend on the size
        of the recording, and the mapped pages of a window are handed back as
        soon as it has been copied out.  The windows of a box concatenate into
        the elementary stream it holds, so the header is removed from the
        first window of the box only, never from the ones after it.
        """
        header_len = self.chunk_header_len
        if header_len is None:
            raise AveError(
                "video boxes do not contain a raw H.264/H.265 stream "
                "(password protected export?)"
            )
        for offset, size in self.video_spans:
            position = offset
            end_of_box = offset + size
            while position < end_of_box:
                end = min(position + MAX_CHUNK_BYTES, end_of_box)
                piece = bytes(self.data[position:end])
                if position == offset:
                    piece = piece[header_len:]
                yield piece
                self._release(position, end - position)
                position = end

    def _release(self, offset, size):
        """Ask the kernel to drop the mapped pages of a range just read.

        Without this a memory mapped file stays resident once it has been
        walked, and the peak memory of a conversion ends up being the size of
        the recording after all - as clean, evictable page cache rather than
        as anonymous memory, but still as resident pages.  Best effort: where
        the hint does not exist the pages simply stay, which costs neither
        speed nor correctness.
        """
        if self._mapping is None:
            return
        advice = getattr(mmap, "MADV_DONTNEED", None)
        madvise = getattr(self._mapping, "madvise", None)
        if advice is None or madvise is None:
            return
        try:
            page = mmap.ALLOCATIONGRANULARITY
            start = offset - offset % page
            madvise(advice, start, (offset + size) - start)
        except (OSError, ValueError, OverflowError):
            pass

    def detect_chunk_header(self):
        """Length of the header in front of the stream in every video box.

        Only the first few kilobytes of each box are read, and they are read
        straight out of the mapping, so this does not copy the recording.
        """
        prefixes = [bytes(self.data[offset:offset + 8192])
                     for offset, _size in self.video_spans]
        return detect_chunk_header(prefixes) if prefixes else None

    @property
    def chunk_header_len(self):
        """Bytes in front of the elementary stream in every ``datp`` box."""
        if self._chunk_header_len is None:
            self._chunk_header_len = self.detect_chunk_header()
        return self._chunk_header_len

    def iter_nals(self):
        """Yield (offset, header_len, nal) for the normalised video stream.

        The offsets are those of the stream produced by :attr:`stream`: every
        NAL unit re-emitted with a four byte start code, trailing zero
        padding removed, and everything after the last picture dropped.
        """
        return split_nals_stream(self.iter_chunks())

    @property
    def stream(self):
        """The whole normalised elementary stream as one bytes object.

        Convenient for tests and for small exports; a conversion of a large
        export goes through :meth:`iter_nals` instead so that nothing this
        size is ever built.
        """
        return normalise_stream(self.iter_chunks())

    def scan(self, sink=None):
        """Detect the codec and analyse the whole recording in one pass.

        Returns ``(codec, analysis)``; ``sink`` receives the normalised
        elementary stream while it is read.
        """
        return scan_stream(self.iter_chunks(), sink=sink)

    # -- derived information ---------------------------------------------

    @property
    def video_tracks(self):
        """All index entries that actually carry frames."""
        return [track for track in self.tracks if track["frames"]]

    def video_chunks(self):
        """Chunks of the primary video track, in recording order."""
        if not self.tracks:
            raise AveError("the export contains no indexed video frames")
        # The main video track is the one the datp boxes were attributed to.
        # The old fallback - the track with the most frames - is kept for an
        # export without any usable index.
        if self.video_track_id is not None:
            best = [track for track in self.video_tracks
                    if track["track_id"] == self.video_track_id]
            if not best:
                raise AveError(
                    "the recording index does not describe any picture of "
                    "track %s, which the video data belongs to"
                    % self.video_track_id
                )
            return best
        best = max(
            {track["track_id"] for track in self.video_tracks},
            key=lambda track_id: sum(
                t["frames"] for t in self.video_tracks if t["track_id"] == track_id
            ),
        )
        return [track for track in self.video_tracks if track["track_id"] == best]

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

    def total_ticks(self):
        return sum(chunk["duration_ticks"] for chunk in self.video_chunks())

    def describe(self):
        """Human readable summary used by --info.

        The whole stream is read, because the picture count and whether the
        pictures are reordered are properties of the whole recording: a
        summary computed from the first few kilobytes would report a picture
        count of one and could not tell a reordered H.265 export from a safe
        one at all.
        """
        codec, analysis = self.scan()
        chunks = self.video_chunks() if self.tracks else []
        lines = ["file            : %s" % self.path]
        lines.append("size            : %.1f MB" % (self.size / 1e6))
        lines.append("codec           : %s" % codec)
        index_frames = sum(c["frames"] for c in chunks)
        lines.append("pictures        : %d in the stream, %d in the index"
                     % (analysis["pictures"], index_frames))
        lines.append("reordering      : %s" % {
            "no": "no (no B-frames, a lossless copy is safe)",
            "yes": "yes (B-frames: --reencode, or a lossless path is missing)",
            "unknown": "unknown (an unreadable slice header, refusing to copy)",
        }[analysis["reordering"]])
        try:
            rate, note = self.frame_rate()
        except AveError as error:
            lines.append("frame rate      : unknown (%s)" % error)
        else:
            lines.append("frame rate      : %s fps (%s)" % (float(rate), note))
        lines.append("chunk header    : %s bytes"
                     % (self.chunk_header_len
                        if self.chunk_header_len is not None
                        else "undetermined"))
        stamp = self.first_timestamp() if chunks else None
        if stamp:
            lines.append("starts at       : %s (UTC)" % format_timestamp(stamp))
        lines.append("duration        : %s"
                     % format_duration(self.total_ticks() if chunks else 0))
        lines.append("video track     : %s" % (
            self.video_track_id if self.video_track_id is not None
            else "unknown (no usable index)"))
        if self.track_id_note:
            lines.append("note            : %s" % self.track_id_note)
        elif self.video_track_id not in (None, VIDEO_TRACK_HINT):
            lines.append("note            : the video track is %s, not the %s "
                         "Avigilon usually uses"
                         % (self.video_track_id, VIDEO_TRACK_HINT))
        if not self.indexed:
            lines.append("note            : the recording index does not "
                         "describe the video data, so all %d video boxes were "
                         "concatenated" % len(self.data_spans))
        for secondary in self.secondary_tracks:
            lines.append("note            : track %s also has %d data chunk(s) "
                         "in this export; they are not converted (only video "
                         "is supported)"
                         % (secondary["track_id"], len(secondary["spans"])))
        lines.append("tracks          :")
        primary = self.video_chunks() if chunks else []
        for index, track in enumerate(self.tracks):
            if track in primary:
                kind = "primary video"
            elif not track["frames"]:
                kind = "unused"
            else:
                kind = "additional stream, not converted"
            line = ("  [%d] id=%-4s frames=%-5d first=%-5d duration=%s"
                    % (index, track["track_id"], track["frames"],
                       track["first_frame"],
                       format_duration(track["duration_ticks"])))
            if track.get("header_mismatch"):
                line += "  (tkfh says track %s)" % track["header_track_id"]
            lines.append("%s  (%s)" % (line, kind))
        return "\n".join(lines)


# --------------------------------------------------------------------------
# conversion
# --------------------------------------------------------------------------


FFMPEG_HELP = (
    "ffmpeg was not found. Install it with one of:\n"
    "  Windows : winget install Gyan.FFmpeg      (then reopen the terminal)\n"
    "  macOS   : brew install ffmpeg\n"
    "  Debian  : sudo apt install ffmpeg\n"
    "  Fedora  : sudo dnf install ffmpeg\n"
    "or install a private copy with: pip install imageio-ffmpeg\n"
    "or point this program at an existing binary: --ffmpeg PATH"
)

FFMPEG_TOO_OLD = (
    "%s is ffmpeg %s, but this tool needs ffmpeg 5.0 or newer: the 'setts' "
    "bitstream filter, which gives every copied picture its own timestamp, "
    "was added in 5.0. Install a current ffmpeg, or use the standalone "
    "ave2mp4.exe, which bundles one."
)


def _bundled(names):
    """Files that ship next to a frozen copy of this program."""
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return []
    return [os.path.join(base, name) for name in names]


def _windows_locations(names):
    """Where a Windows user ends up with ffmpeg after the usual installs."""
    if os.name != "nt":
        return []
    candidates = []
    for variable in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
        root = os.environ.get(variable)
        if not root:
            continue
        for name in names:
            candidates.append(
                os.path.join(root, "Microsoft", "WinGet", "Links", name)
            )
            candidates.append(os.path.join(root, "chocolatey", "bin", name))
            candidates.append(os.path.join(root, "ffmpeg", "bin", name))
    return candidates


def _imageio_ffmpeg():
    """The ffmpeg of the ``imageio-ffmpeg`` wheel, when it is installed."""
    try:
        import imageio_ffmpeg
    except ImportError:
        return None
    try:
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _first_existing(candidates):
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def find_ffmpeg(explicit=None):
    """Locate ffmpeg: explicit path, bundle, PATH, imageio-ffmpeg, Windows."""
    if explicit:
        if not os.path.isfile(explicit):
            raise AveError("no ffmpeg at %s (from --ffmpeg)" % explicit)
        return explicit
    names = ("ffmpeg.exe", "ffmpeg") if os.name == "nt" else ("ffmpeg",)
    found = _first_existing(_bundled(names))
    if not found:
        found = shutil.which("ffmpeg")
    if not found:
        found = _first_existing([_imageio_ffmpeg()])
    if not found:
        found = _first_existing(_windows_locations(names))
    return found


def find_ffprobe(ffmpeg=None):
    """Locate ffprobe. It is optional: only the closing summary uses it."""
    names = ("ffprobe.exe", "ffprobe") if os.name == "nt" else ("ffprobe",)
    found = _first_existing(_bundled(names)) or shutil.which("ffprobe")
    if not found and ffmpeg:
        found = _first_existing(
            os.path.join(os.path.dirname(ffmpeg), name) for name in names
        )
    return found


def require_ffmpeg(explicit=None):
    ffmpeg = find_ffmpeg(explicit)
    if not ffmpeg:
        raise AveError(FFMPEG_HELP)
    return ffmpeg


class Ffmpeg:
    """An ffmpeg binary, together with the capabilities this tool relies on.

    The list is asked for once per binary per process: the copy path needs
    the ``setts`` bitstream filter (ffmpeg 5.0 and newer) and ``--reencode``
    needs libx264, so a private ffmpeg that is too old is reported up front
    instead of after a conversion has already started.
    """

    _cache = {}

    def __init__(self, path):
        self.path = path
        self._version = None
        self._bsfs = None
        self._encoders = None

    @classmethod
    def get(cls, path):
        if path not in cls._cache:
            cls._cache[path] = cls(path)
        return cls._cache[path]

    def _list(self, flag, cache_name):
        """Every name ffmpeg lists for ``-bsfs`` / ``-encoders``.

        The layout of that list is not stable: ffmpeg 4 prints flags and a
        description next to the name, ffmpeg 8 prints the bare names.  Every
        word of every line is therefore collected and a capability is looked
        up as a whole word, which does not depend on the column it lands in.
        """
        cached = getattr(self, cache_name)
        if cached is not None:
            return cached
        try:
            result = subprocess.run(
                [self.path, "-hide_banner", flag], stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError as error:
            raise AveError("cannot run %s: %s" % (self.path, error.strerror or error))
        names = set()
        if result.returncode == 0:
            for line in result.stdout.decode("utf-8", "replace").splitlines():
                line = line.strip()
                if not line or line.endswith(":"):
                    continue          # the "Bitstream Filters:" style heading
                for word in line.split():
                    if word and not word.strip(".").isupper():
                        names.add(word)
        value = frozenset(names)
        setattr(self, cache_name, value)
        return value

    def bitstream_filters(self):
        return self._list("-bsfs", "_bsfs")

    def encoders(self):
        return self._list("-encoders", "_encoders")

    def has_filter(self, name):
        return name in self.bitstream_filters()

    def has_encoder(self, name):
        return name in self.encoders()

    def version(self):
        if self._version is None:
            self._version = (0, 0)
            try:
                result = subprocess.run(
                    [self.path, "-hide_banner", "-version"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                )
            except OSError:
                return self._version
            first = result.stdout.decode("utf-8", "replace").splitlines()
            if first and first[0].startswith("ffmpeg version"):
                parts = first[0].split()
                if len(parts) > 2:
                    digits = []
                    for piece in parts[2].split("."):
                        if not piece.isdigit():
                            break
                        digits.append(int(piece))
                    self._version = tuple(digits + [0, 0])[:3]
        return self._version

    def require(self, *needed):
        """Check the capabilities a conversion needs, with a clear message."""
        if not self.has_filter("setts"):
            raise AveError(FFMPEG_TOO_OLD % (self.path,
                                             ".".join(map(str, self.version()))))
        for encoder in needed:
            if not self.has_encoder(encoder):
                raise AveError(
                    "%s has no %s encoder, which --reencode needs. Install a "
                    "full ffmpeg build (the distributions ship libx264), or "
                    "convert without --reencode." % (self.path, encoder)
                )


# --------------------------------------------------------------------------
# output validation and atomic replacement
# --------------------------------------------------------------------------


def mp4_box_types(path):
    """Return the top level box types of an MP4 file, in order.

    Used to tell "ffmpeg wrote a video" from "ffmpeg left a stub" without
    needing ffprobe, which the frozen Windows build does not ship.
    """
    types = []
    try:
        with open(path, "rb") as handle:
            offset = 0
            size = os.fstat(handle.fileno()).st_size
            while offset + 8 <= size and len(types) < 64:
                handle.seek(offset)
                header = handle.read(8)
                if len(header) < 8:
                    break
                box_size, box_type = struct.unpack(">I4s", header)
                if box_size == 1:
                    extra = handle.read(8)
                    if len(extra) < 8:
                        break
                    box_size = struct.unpack(">Q", extra)[0]
                elif box_size == 0:
                    box_size = size - offset
                if box_size < 8 or offset + box_size > size:
                    break
                types.append(box_type)
                offset += box_size
    except OSError as error:
        raise AveError("cannot read %s: %s" % (path, error.strerror or error))
    return types


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def probe_video(path, ffprobe, count_frames=False):
    """Return ``(properties, error)`` for the video stream of ``path``.

    ``properties`` is a dict with ``width``, ``height``, ``frames`` and
    ``duration``; any of them is None when the container does not say.  With
    ``count_frames`` the frames are counted by decoding the file, which
    proves that every picture survives a decode and costs far more than
    reading the sample table.
    """
    command = [ffprobe, "-hide_banner", "-v", "error", "-select_streams", "v:0"]
    if count_frames:
        command.append("-count_frames")
        entries = "stream=width,height,nb_read_frames,duration,codec_name"
    else:
        entries = "stream=width,height,nb_frames,duration,codec_name"
    command += ["-show_entries", entries, "-of", "json", path]
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
    except OSError as error:
        return None, "cannot run %s: %s" % (ffprobe, error.strerror or error)
    if result.returncode != 0:
        return None, result.stderr.decode("utf-8", "replace").strip() or (
            "ffprobe exited with %d" % result.returncode
        )
    try:
        streams = json.loads(result.stdout.decode("utf-8", "replace"))["streams"]
    except (ValueError, KeyError, TypeError) as error:
        return None, "cannot read the ffprobe output (%s)" % error
    if not streams:
        return None, "no video stream in the file"
    stream = streams[0]
    properties = {
        "width": _as_int(stream.get("width")),
        "height": _as_int(stream.get("height")),
        "frames": _as_int(stream.get("nb_read_frames" if count_frames
                                     else "nb_frames")),
        "duration": _as_float(stream.get("duration")),
        "codec": stream.get("codec_name"),
    }
    return properties, ""


def validate_output(path, expected_pictures, ffprobe=None, verify=VERIFY_CHEAP):
    """Check a freshly written MP4 before it replaces the destination.

    Returns ``(properties, notes)``.  Raises :class:`AveError` when the file
    must not be committed, which is what keeps a failed conversion from
    destroying a good MP4 or from leaving a truncated one behind.
    """
    notes = []
    try:
        if os.path.getsize(path) == 0:
            raise AveError("ffmpeg left an empty file behind")
    except OSError as error:
        raise AveError("ffmpeg did not produce %s: %s"
                       % (path, error.strerror or error))
    types = mp4_box_types(path)
    for required in (b"ftyp", b"mdat", b"moov"):
        if required not in types:
            raise AveError(
                "the file ffmpeg produced is not a usable MP4 (no %s box, found "
                "%s). It has been discarded and the existing file, if any, was "
                "left alone"
                % (required.decode("ascii"),
                   " ".join(t.decode("ascii", "replace") for t in types) or "nothing")
            )
    if not ffprobe or verify == VERIFY_NONE:
        return None, notes

    count_frames = verify == VERIFY_FULL
    properties, error = probe_video(path, ffprobe, count_frames=count_frames)
    if properties is None:
        raise AveError(
            "ffprobe cannot read the MP4 that was just written: %s. It has "
            "been discarded and the existing file, if any, was left alone"
            % error
        )
    frames = properties["frames"]
    if not frames:
        raise AveError(
            "the MP4 that was just written reports no frames. It has been "
            "discarded and the existing file, if any, was left alone"
        )
    if not properties["width"] or not properties["height"]:
        raise AveError(
            "the MP4 that was just written reports no picture size. It has "
            "been discarded and the existing file, if any, was left alone"
        )
    if count_frames:
        if expected_pictures and frames != expected_pictures:
            raise AveError(
                "the MP4 that was just written decodes to %d pictures but the "
                "export holds %d. It has been discarded and the existing "
                "file, if any, was left alone" % (frames, expected_pictures)
            )
    elif expected_pictures and frames < expected_pictures:
        raise AveError(
            "the MP4 that was just written holds %d pictures but the export "
            "holds %d, so it is incomplete. It has been discarded and the "
            "existing file, if any, was left alone"
            % (frames, expected_pictures)
        )
    elif expected_pictures and frames > expected_pictures:
        notes.append("%d pictures in the MP4, %d parsed from the export"
                     % (frames, expected_pictures))
    return properties, notes


def _remove_quietly(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _sync_file(path):
    """Best effort flush, so a committed file survives a power cut."""
    try:
        handle = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(handle)
    except OSError:
        pass
    finally:
        os.close(handle)


def _sync_directory(path):
    if os.name == "nt":
        return
    try:
        handle = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(handle)
    except OSError:
        pass
    finally:
        os.close(handle)


def commit_output(temporary, output):
    """Move a validated temporary file onto its destination.

    ``os.replace`` is atomic wherever the filesystem supports it, so a reader
    of ``output`` sees either the old file or the new one and never a partly
    written mixture.  On Windows it fails if another program has the
    destination open, for instance a player that is still using it; that is
    reported instead of silently falling back to a destructive copy.
    """
    _sync_file(temporary)
    try:
        os.replace(temporary, output)
    except OSError as error:
        if os.name == "nt" and error.errno in (errno.EACCES, errno.EPERM):
            raise AveError(
                "cannot replace %s: it is open in another program. Close it "
                "and run the conversion again" % output
            )
        raise AveError("cannot write %s: %s" % (output, error.strerror or error))
    _sync_directory(os.path.dirname(os.path.abspath(output)))


def write_exclusively(path, chunks, force=False):
    """Write ``chunks`` to ``path``, refusing to clobber without ``force``.

    The data goes to a temporary file next to the destination and is moved
    into place afterwards, so an interrupted or failing write cannot leave a
    truncated file where a previous dump was.  Without ``force`` the
    destination has to be absent, which is checked by creating it exclusively
    rather than by looking first.
    """
    directory = os.path.dirname(os.path.abspath(path))
    ensure_output_directory(directory)
    if not force and os.path.exists(path):
        raise AveError("%s already exists (use --force to overwrite)" % path)
    try:
        handle, temporary = tempfile.mkstemp(
            dir=directory, prefix=".%s.ave2mp4-" % os.path.basename(path)[:48],
            suffix=".part")
    except OSError as error:
        raise AveError("cannot write into %s: %s" % (directory,
                                                     error.strerror or error))
    try:
        with os.fdopen(handle, "wb") as sink:
            for chunk in chunks:
                sink.write(chunk)
        if not force and os.path.exists(path):
            # Somebody created it while the dump was being written.
            raise AveError(
                "%s already exists (use --force to overwrite)" % path
            )
        commit_output(temporary, path)
        temporary = None
    finally:
        if temporary:
            _remove_quietly(temporary)


def describe_output(path, verbosity=1, ffprobe=None):
    """Append a short verification of the produced file.

    Skipped silently when ffprobe is not available: it is convenient, not
    required, and the frozen Windows build ships without it.
    """
    if verbosity < 1 or not ffprobe:
        return ""
    properties, _error = probe_video(path, ffprobe)
    if properties is None:
        return ""
    frames = properties["frames"]
    return "\n                 %sx%s, %s frames, %s" % (
        properties["width"], properties["height"],
        frames if frames is not None else "an unknown number of",
        format_seconds(properties["duration"]),
    )


def ensure_output_directory(path):
    """Create the directory an MP4 is about to be written into."""
    if not path:
        return
    if os.path.isdir(path):
        return
    try:
        os.makedirs(path)
    except OSError as error:
        raise AveError("cannot create the output directory %s: %s"
                       % (path, error.strerror or error))
    if not os.path.isdir(path):
        raise AveError("%s is not a directory" % path)


def convert(path, output=None, force=False, dump_stream=None, verbosity=1,
            reencode=False, ffmpeg_path=None, verify=VERIFY_CHEAP,
            temp_dir=None):
    """Convert a single .ave file; returns the path of the written MP4."""
    def say(message, level=1):
        if verbosity >= level:
            print(message)

    if verify not in VERIFY_LEVELS:
        raise AveError("unknown --verify level %r" % verify)

    with AveFile(path) as ave:
        return _convert(ave, path, output, force, dump_stream, verbosity,
                        reencode, ffmpeg_path, verify, temp_dir, say)


def _convert(ave, path, output, force, dump_stream, verbosity, reencode,
             ffmpeg_path, verify, temp_dir, say):
    ffmpeg = require_ffmpeg(ffmpeg_path)
    tool = Ffmpeg.get(ffmpeg)
    tool.require()
    if reencode:
        tool.require("libx264")
    ffprobe = find_ffprobe(ffmpeg) if verify != VERIFY_NONE else None

    # Resolve the destination before any work is done, so a bad path is
    # reported before the recording is read.
    if output is None:
        output = os.path.splitext(path)[0] + ".mp4"
    output_directory = os.path.dirname(os.path.abspath(output))
    ensure_output_directory(output_directory)
    if os.path.exists(output) and not force:
        raise AveError("%s already exists (use --force to overwrite)" % output)

    temp_root = temp_dir or None
    if temp_root is not None and not os.path.isdir(temp_root):
        raise AveError("no such temporary directory: %s" % temp_root)
    workspace = tempfile.mkdtemp(prefix="ave2mp4-", dir=temp_root)
    elementary = os.path.join(workspace, "stream.h264")
    pending_output = None
    try:
        # One pass over the export: the normalised stream is written to a
        # temporary file while it is analysed, so nothing the size of the
        # recording is ever held in memory.
        with open(elementary, "wb+") as handle:
            codec, analysis = ave.scan(sink=handle)
            full_length = handle.tell()
            handle.truncate(analysis["stream_end"])
        if analysis["reordering"] == "unknown" and not reencode:
            # The picture order could not be established, so a copy might
            # shuffle the pictures.  A transcode does not copy the picture
            # order at all, so it is safe.
            raise AveError(
                "the picture order of at least one picture in this %s export "
                "could not be read from the bitstream, so it cannot be "
                "guaranteed that copying the stream keeps the pictures in "
                "display order. Re-run with --reencode to transcode instead "
                "(lossy), or report this export with the --info output."
                % codec.upper()
            )
        if analysis["reordering"] == "yes" and not reencode:
            # ffmpeg has to be told the display order of a reordered stream,
            # and the raw demuxers do not derive it: a plain remux shuffles
            # the pictures.  Never do that silently.  This is decided before
            # ffmpeg is even looked for, so the complaint is about the real
            # problem.
            raise AveError(
                "this export uses B-frames, and copying such a stream without "
                "its display order would shuffle the pictures. Re-run with "
                "--reencode to transcode instead (lossy), or report this "
                "export with the --info output so a lossless path can be "
                "added."
            )

        rate, rate_note = ave.frame_rate()
        index_frames = sum(chunk["frames"] for chunk in ave.video_chunks())
        short_by = index_frames - analysis["pictures"]
        # A chunk can be cut in the middle of its last picture, so one picture
        # per chunk may be missing from the stream.  More than that means the
        # stream lost video, whatever the reason.
        allowed = max(len(ave.data_spans), 2)
        if short_by > allowed:
            raise AveError(
                "the recording index says the export holds %d pictures but only "
                "%d could be read from the video data. Refusing to write a "
                "file that silently drops %d pictures" % (
                    index_frames, analysis["pictures"], short_by)
            )

        usable = analysis["stream_end"]
        say("%s" % path)
        say("  ffmpeg       : %s" % ffmpeg)
        say("  codec        : %s" % codec)
        say("  frame rate   : %s fps (%s)" % (float(rate), rate_note))
        say("  pictures     : %d in the stream, %d in the index"
            % (analysis["pictures"], index_frames))
        if not ave.indexed:
            say("  warning      : the recording index does not describe the "
                "video data, so all %d video boxes were concatenated"
                % len(ave.data_spans), level=0)
        for secondary in ave.secondary_tracks:
            say("  note         : track %s has %d data chunk(s) in this "
                "export; they are not converted (only video is supported)"
                % (secondary["track_id"], len(secondary["spans"])))
        for track in _extra_tracks(ave):
            say("  note         : track id %s holds %d indexed pictures that "
                "are not converted (only video is supported)"
                % (track["track_id"], track["frames"]), level=0)
        if analysis["pictures"] != index_frames:
            say("  warning      : stream and index disagree about the frame "
                "count", level=0)
        if reencode:
            say("  warning      : re-encoding (%s) - the result is no longer "
                "bit exact" % {
                    "yes": "the export uses B-frames",
                    "unknown": "the picture order could not be read",
                    "no": "as asked for",
                }[analysis["reordering"]], level=0)
        elif analysis["reordering"] == "no":
            say("  note         : no B-frames, ignoring the bogus reorder "
                "delay advertised in the bitstream")
        # Everything after the last picture is a partial frame cut off by the
        # export; feeding it to ffmpeg only produces a decoder error.
        trailing = full_length - usable
        if trailing:
            say("  note         : dropping %d trailing bytes (%.1f%% of the "
                "stream) that do not form a picture"
                % (trailing, 100.0 * trailing / max(usable, 1)))
            if trailing > MIN_PICTURE_BYTES:
                say("  warning      : that fragment is large enough to be a "
                    "real frame; the export may be truncated", level=0)

        if dump_stream:
            write_exclusively(
                dump_stream, _read_range(elementary, usable), force=force
            )
            say("  elementary   : %s (%d bytes)" % (dump_stream, usable))

        pending_output = _temporary_output(output_directory,
                                           os.path.basename(output))
        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
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
            pending_output,
        ]
        result = subprocess.run(command, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        if result.returncode != 0:
            raise AveError(
                "ffmpeg failed:\n%s"
                % result.stderr.decode("utf-8", "replace").strip()
            )

        properties, notes = validate_output(
            pending_output, analysis["pictures"], ffprobe, verify
        )
        for note in notes:
            say("  note         : %s" % note)
        commit_output(pending_output, output)
        pending_output = None
    except BaseException:
        if pending_output:
            _remove_quietly(pending_output)
        raise
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    summary = describe_output(output, verbosity, ffprobe)
    if not summary and verbosity >= 1 and not ffprobe:
        # Nothing to verify with (the frozen Windows build has no ffprobe),
        # so at least report what the recording index promised, labelled as
        # what it is instead of pretending it was checked.
        summary = "\n                 %d frames, %.2f s (from the index)" % (
            analysis["pictures"], float(analysis["pictures"] / rate))
    say("  written      : %s%s" % (output, summary))
    return output


def _extra_tracks(ave):
    """Indexed tracks with pictures that are not the converted video track."""
    if not ave.tracks:
        return []
    primary_ids = {track["track_id"] for track in ave.video_chunks()}
    return [track for track in ave.tracks
            if track["frames"] and track["track_id"] not in primary_ids]


def _read_range(path, length):
    """Yield ``length`` bytes of ``path`` in bounded pieces."""
    with open(path, "rb") as handle:
        remaining = length
        while remaining > 0:
            chunk = handle.read(min(1 << 20, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def _temporary_output(directory, name):
    """A unique path next to the destination, named so ffmpeg picks MP4.

    The file is created empty and exclusively, so nothing else can guess the
    name and nothing has to be removed before ffmpeg runs.  It lives in the
    destination directory because ``os.replace`` is only atomic within one
    filesystem, and because the temporary file must not fill up a different
    volume than the final result.
    """
    stem = name.rsplit(".", 1)[0][:64] or "output"
    try:
        handle, path = tempfile.mkstemp(
            dir=directory, prefix=".%s.ave2mp4-" % stem, suffix=".mp4"
        )
    except OSError as error:
        raise AveError("cannot write into %s: %s" % (directory,
                                                     error.strerror or error))
    os.close(handle)
    return path


# --------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------


def use_utf8_output():
    """Do not crash on non-ASCII paths when the output is redirected.

    An interactive Windows console is left alone on purpose: Python already
    talks to it as UTF-16, and reconfiguring it would garble the output.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        isatty = getattr(stream, "isatty", None)
        if reconfigure is None or (isatty is not None and isatty()):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def pause_if_own_console():
    """Keep the console window open when a frozen build was double clicked.

    Windows closes the window of a program that owns its console as soon as
    the program exits, which makes drag and drop useless: the log would be
    gone before it can be read.  When no shell is attached to the console,
    the process was started from the Explorer and has to wait.
    """
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        attached = (ctypes.c_uint * 4)()
        if kernel32.GetConsoleProcessList(attached, 4) > 1:
            return
    except Exception:
        return
    if not sys.stdin.isatty():
        return
    try:
        input("\nPress Enter to close ...")
    except (EOFError, KeyboardInterrupt):
        pass


def build_parser():
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
                        help="overwrite existing MP4 and --dump-stream files")
    parser.add_argument("-i", "--info", action="store_true",
                        help="only print what is inside the export")
    parser.add_argument("--dump-stream", metavar="PATH",
                        help="also write the raw elementary stream (debugging)")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="only print errors and the result line")
    parser.add_argument("--reencode", action="store_true",
                        help="transcode instead of copying (lossy); required "
                             "for exports that use B-frames")
    parser.add_argument("--verify", choices=VERIFY_LEVELS, default=VERIFY_CHEAP,
                        help="how thoroughly the finished MP4 is checked "
                             "before it replaces the destination: 'cheap' "
                             "(default) reads the sample table, 'full' also "
                             "decodes every picture, 'none' only checks that "
                             "the file is a readable MP4")
    parser.add_argument("--temp-dir", metavar="DIR",
                        help="where to put the temporary elementary stream "
                             "(default: the system temporary directory; on "
                             "Linux that may be a RAM backed tmpfs, so a "
                             "large export may need a real directory here)")
    parser.add_argument("--ffmpeg", metavar="PATH",
                        help="ffmpeg binary to use (default: a bundled one if "
                             "present, otherwise the one in PATH)")
    parser.add_argument("--version", action="version",
                        version="ave2mp4 %s" % __version__)
    return parser


def main(argv=None):
    use_utf8_output()
    try:
        return _run(argv)
    finally:
        pause_if_own_console()


def _run(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.files:
        parser.print_help()
        return 1

    status = 0
    for path in args.files:
        try:
            if args.info:
                # --info must not touch the filesystem: it reports on a file,
                # it does not prepare one.
                with AveFile(path) as ave:
                    print(ave.describe())
                continue
            output = None
            if args.output_dir:
                ensure_output_directory(args.output_dir)
                output = os.path.join(
                    args.output_dir,
                    os.path.splitext(os.path.basename(path))[0] + ".mp4",
                )
            convert(path, output=output, force=args.force,
                    dump_stream=args.dump_stream, reencode=args.reencode,
                    ffmpeg_path=args.ffmpeg, verify=args.verify,
                    temp_dir=args.temp_dir,
                    verbosity=0 if args.quiet else 1)
        except AveError as error:
            print("ave2mp4: %s: %s" % (path, error), file=sys.stderr)
            status = 1
        except MemoryError:
            print("ave2mp4: %s: out of memory. The export is read in bounded "
                  "pieces, so this points at the machine rather than at the "
                  "file size; try closing other programs." % path,
                  file=sys.stderr)
            status = 1
        except OSError as error:
            print("ave2mp4: %s: %s" % (path, error), file=sys.stderr)
            status = 1
        except KeyboardInterrupt:
            return 130
    return status


if __name__ == "__main__":
    sys.exit(main())
