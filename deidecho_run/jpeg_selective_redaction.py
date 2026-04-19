"""
Selective compressed-domain redaction for baseline JPEG, with pydicom helpers.

This is a Python-native port inspired by the PixelMed JPEG selective block
redaction codec. It supports *baseline sequential Huffman DCT JPEG* (SOF0)
and is aimed at DICOM encapsulated echo frames that are already JPEG Baseline
compressed.

What it does
------------
- parses JPEG marker segments
- parses DQT/DHT/SOF0/SOS/DRI
- walks baseline entropy-coded data MCU by MCU
- decides block redaction from a supplied pixel mask in image coordinates
- rewrites only DC and AC entropy codes for redacted blocks:
    * first redacted block for a component gets DC diff = -previous_dc
    * subsequent consecutive redacted blocks get DC diff = 0
    * AC coefficients are replaced by EOB
- keeps all unredacted blocks semantically unchanged
- preserves restart markers and marker segments
- provides pydicom helpers for encapsulated single-frame and multi-frame data

What it does NOT support
------------------------
- progressive JPEG
- arithmetic coding
- lossless JPEG
- multi-scan baseline files that spread components over multiple scans
  (the vast majority of baseline echo JPEGs are single-scan)
- exotic APP/COM rewriting beyond byte-preservation
- masks that are not aligned to the decompressed image coordinate system

This is intended as a careful, readable implementation for research and
pipeline use. Validate on your data before production deployment.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

# Optional pydicom helpers; the module can still be imported without pydicom.
try:
    import pydicom
    from pydicom.encaps import encapsulate, generate_frames
except Exception:  # pragma: no cover
    pydicom = None
    encapsulate = None
    generate_frames = None


# -----------------------------------------------------------------------------
# Constants and simple utilities
# -----------------------------------------------------------------------------

SOI = 0xD8
EOI = 0xD9
SOS = 0xDA
DQT = 0xDB
DHT = 0xC4
DRI = 0xDD
SOF0 = 0xC0
RST0 = 0xD0
RST7 = 0xD7
TEM = 0x01

STANDALONE_MARKERS = {SOI, EOI, TEM, *range(RST0, RST7 + 1)}

ZIGZAG = [
    0,
    1,
    5,
    6,
    14,
    15,
    27,
    28,
    2,
    4,
    7,
    13,
    16,
    26,
    29,
    42,
    3,
    8,
    12,
    17,
    25,
    30,
    41,
    43,
    9,
    11,
    18,
    24,
    31,
    40,
    44,
    53,
    10,
    19,
    23,
    32,
    39,
    45,
    52,
    54,
    20,
    22,
    33,
    38,
    46,
    51,
    55,
    60,
    21,
    34,
    37,
    47,
    50,
    56,
    59,
    61,
    35,
    36,
    48,
    49,
    57,
    58,
    62,
    63,
]


def _be16(b: bytes) -> int:
    return (b[0] << 8) | b[1]


def _clamp_int(v: int, lo: int, hi: int) -> int:
    return lo if v < lo else hi if v > hi else v


# -----------------------------------------------------------------------------
# Huffman tables
# -----------------------------------------------------------------------------


@dataclass
class HuffmanTable:
    table_class: int  # 0=DC, 1=AC
    table_id: int
    bits: List[int]  # counts for code lengths 1..16
    values: List[int]
    mincode: List[int] = field(default_factory=list)
    maxcode: List[int] = field(default_factory=list)
    valptr: List[int] = field(default_factory=list)
    ehufco: Dict[int, int] = field(default_factory=dict)
    ehufsi: Dict[int, int] = field(default_factory=dict)
    eob_code: Optional[int] = None
    eob_size: Optional[int] = None

    def __post_init__(self) -> None:
        self._build_decoder_tables()
        self._build_encoder_tables()

    def _build_decoder_tables(self) -> None:
        huffsize: List[int] = []
        for i, count in enumerate(self.bits, start=1):
            huffsize.extend([i] * count)
        huffsize.append(0)

        huffcode: List[int] = []
        code = 0
        si = huffsize[0]
        k = 0
        while True:
            if huffsize[k] == 0:
                break
            while huffsize[k] == si:
                huffcode.append(code)
                code += 1
                k += 1
            code <<= 1
            si += 1

        self.mincode = [-1] * 17
        self.maxcode = [-1] * 17
        self.valptr = [-1] * 17
        j = 0
        for i in range(1, 17):
            if self.bits[i - 1] > 0:
                self.valptr[i] = j
                self.mincode[i] = huffcode[j]
                j += self.bits[i - 1] - 1
                self.maxcode[i] = huffcode[j]
                j += 1
            else:
                self.maxcode[i] = -1
        # sentinel like IJG/PixelMed behavior
        self.maxcode.append(1 << 32)

    def _build_encoder_tables(self) -> None:
        huffsize: List[int] = []
        for i, count in enumerate(self.bits, start=1):
            huffsize.extend([i] * count)

        huffcode: List[int] = []
        code = 0
        si = huffsize[0] if huffsize else 0
        k = 0
        while k < len(huffsize):
            while k < len(huffsize) and huffsize[k] == si:
                huffcode.append(code)
                code += 1
                k += 1
            code <<= 1
            si += 1

        self.ehufco = {}
        self.ehufsi = {}
        for symbol, c, s in zip(self.values, huffcode, huffsize):
            self.ehufco[symbol] = c
            self.ehufsi[symbol] = s
        if self.table_class == 1 and 0x00 in self.ehufco:
            self.eob_code = self.ehufco[0x00]
            self.eob_size = self.ehufsi[0x00]

    def decode(self, bitreader: "BitReader") -> int:
        code = 0
        for i in range(1, 17):
            code = (code << 1) | bitreader.read_bit()
            maxcode = self.maxcode[i]
            if maxcode >= 0 and code <= maxcode:
                j = self.valptr[i] + code - self.mincode[i]
                if j < 0 or j >= len(self.values):
                    break
                return self.values[j]
        # match safe fallback behavior
        return 0

    def encode(self, bitwriter: "BitWriter", symbol: int) -> None:
        bitwriter.write_bits(self.ehufco[symbol], self.ehufsi[symbol])


# -----------------------------------------------------------------------------
# Bit IO for entropy-coded data
# -----------------------------------------------------------------------------


class BitReader:
    def __init__(self, data: bytes):
        self.data = data
        self.i = 0
        self.cur = 0
        self.bit = 8

    def read_bit(self) -> int:
        if self.bit > 7:
            if self.i >= len(self.data):
                raise EOFError("Ran out of entropy-coded data")
            self.cur = self.data[self.i]
            self.i += 1
            self.bit = 0
        out = 1 if (self.cur & (0x80 >> self.bit)) else 0
        self.bit += 1
        return out

    def read_bits(self, n: int) -> int:
        v = 0
        for _ in range(n):
            v = (v << 1) | self.read_bit()
        return v


class BitWriter:
    def __init__(self):
        self.out = bytearray()
        self.cur = 0
        self.bit = 0

    def write_bits(self, value: int, nbits: int) -> None:
        if nbits <= 0:
            return
        for i in range(nbits - 1, -1, -1):
            if value & (1 << i):
                self.cur |= 0x80 >> self.bit
            self.bit += 1
            if self.bit == 8:
                self.out.append(self.cur)
                if self.cur == 0xFF:
                    self.out.append(0x00)
                self.cur = 0
                self.bit = 0

    def flush(self) -> None:
        if self.bit > 0:
            while self.bit < 8:
                self.cur |= 0x80 >> self.bit
                self.bit += 1
            self.out.append(self.cur)
            if self.cur == 0xFF:
                self.out.append(0x00)
            self.cur = 0
            self.bit = 0

    def getvalue(self) -> bytes:
        return bytes(self.out)


# -----------------------------------------------------------------------------
# JPEG structural data classes
# -----------------------------------------------------------------------------


@dataclass
class FrameComponent:
    component_id: int
    h: int
    v: int
    tq: int


@dataclass
class ScanComponent:
    component_id: int
    td: int
    ta: int


@dataclass
class SOF0Segment:
    precision: int
    height: int
    width: int
    components: List[FrameComponent]

    @property
    def n_components(self) -> int:
        return len(self.components)


@dataclass
class SOSSegment:
    components: List[ScanComponent]
    ss: int
    se: int
    ah_al: int


@dataclass
class JPEGState:
    sof0: Optional[SOF0Segment] = None
    sos: Optional[SOSSegment] = None
    dht_dc: Dict[int, HuffmanTable] = field(default_factory=dict)
    dht_ac: Dict[int, HuffmanTable] = field(default_factory=dict)
    dqt: Dict[int, List[int]] = field(default_factory=dict)
    restart_interval: int = 0
    marker_segments_before_sos: List[Tuple[int, Optional[bytes]]] = field(
        default_factory=list
    )
    trailer_segments: List[Tuple[int, Optional[bytes]]] = field(default_factory=list)


# -----------------------------------------------------------------------------
# JPEG parsing helpers
# -----------------------------------------------------------------------------


class JPEGParseError(ValueError):
    pass


class JPEGParser:
    def __init__(self, jpeg_bytes: bytes):
        self.buf = jpeg_bytes
        self.pos = 0
        self.state = JPEGState()

    def _read_u8(self) -> int:
        if self.pos >= len(self.buf):
            raise JPEGParseError("Unexpected EOF")
        v = self.buf[self.pos]
        self.pos += 1
        return v

    def _read_n(self, n: int) -> bytes:
        if self.pos + n > len(self.buf):
            raise JPEGParseError("Unexpected EOF")
        b = self.buf[self.pos : self.pos + n]
        self.pos += n
        return b

    def _read_marker(self) -> int:
        while True:
            b = self._read_u8()
            if b == 0xFF:
                break
        while True:
            m = self._read_u8()
            if m != 0xFF:
                return m

    def _read_segment_payload(self) -> bytes:
        length = _be16(self._read_n(2))
        if length < 2:
            raise JPEGParseError(f"Bad segment length {length}")
        return self._read_n(length - 2)

    def parse(self) -> Tuple[JPEGState, bytes, List[bytes]]:
        if self._read_marker() != SOI:
            raise JPEGParseError("Not a JPEG SOI")
        self.state.marker_segments_before_sos.append((SOI, None))

        entropy_chunks: List[bytes] = []
        found_sos = False

        while True:
            marker = self._read_marker()
            if marker == EOI:
                self.state.trailer_segments.append((EOI, None))
                break

            if marker in STANDALONE_MARKERS:
                # Outside entropy coded data these should not appear; preserve.
                if found_sos:
                    self.state.trailer_segments.append((marker, None))
                else:
                    self.state.marker_segments_before_sos.append((marker, None))
                continue

            payload = self._read_segment_payload()

            if marker == DQT:
                self._parse_dqt(payload)
            elif marker == DHT:
                self._parse_dht(payload)
            elif marker == SOF0:
                self._parse_sof0(payload)
            elif marker == DRI:
                self._parse_dri(payload)
            elif marker == SOS:
                self._parse_sos(payload)
                self.state.marker_segments_before_sos.append((marker, payload))
                entropy_chunks, next_marker = self._consume_entropy_coded_data()
                found_sos = True
                if next_marker != EOI:
                    self.state.trailer_segments.append((next_marker, None))
                    # Continue parsing any trailing marker segments until EOI.
                    while True:
                        marker = self._read_marker()
                        if marker == EOI:
                            self.state.trailer_segments.append((EOI, None))
                            break
                        if marker in STANDALONE_MARKERS:
                            self.state.trailer_segments.append((marker, None))
                        else:
                            payload = self._read_segment_payload()
                            self.state.trailer_segments.append((marker, payload))
                    break
                else:
                    self.state.trailer_segments.append((EOI, None))
                    break

            if marker != SOS:
                self.state.marker_segments_before_sos.append((marker, payload))

        if self.state.sof0 is None:
            raise JPEGParseError("Only baseline SOF0 supported")
        if self.state.sos is None:
            raise JPEGParseError("Missing SOS")
        return self.state, self.buf, entropy_chunks

    def _parse_dri(self, payload: bytes) -> None:
        if len(payload) != 2:
            raise JPEGParseError("Bad DRI length")
        self.state.restart_interval = _be16(payload)

    def _parse_sof0(self, payload: bytes) -> None:
        precision = payload[0]
        height = _be16(payload[1:3])
        width = _be16(payload[3:5])
        nc = payload[5]
        comps: List[FrameComponent] = []
        off = 6
        for _ in range(nc):
            cid = payload[off]
            hv = payload[off + 1]
            tq = payload[off + 2]
            comps.append(FrameComponent(cid, hv >> 4, hv & 0x0F, tq))
            off += 3
        self.state.sof0 = SOF0Segment(precision, height, width, comps)

    def _parse_sos(self, payload: bytes) -> None:
        ns = payload[0]
        comps: List[ScanComponent] = []
        off = 1
        for _ in range(ns):
            cid = payload[off]
            tdta = payload[off + 1]
            comps.append(ScanComponent(cid, tdta >> 4, tdta & 0x0F))
            off += 2
        ss, se, ah_al = payload[off], payload[off + 1], payload[off + 2]
        self.state.sos = SOSSegment(comps, ss, se, ah_al)
        if not (ss == 0 and se == 63 and ah_al == 0):
            raise JPEGParseError(
                "Only baseline sequential JPEG scan parameters supported"
            )

    def _parse_dqt(self, payload: bytes) -> None:
        off = 0
        while off < len(payload):
            pq_tq = payload[off]
            off += 1
            pq, tq = pq_tq >> 4, pq_tq & 0x0F
            if pq != 0:
                raise JPEGParseError("Only 8-bit DQT supported")
            q = list(payload[off : off + 64])
            if len(q) != 64:
                raise JPEGParseError("Bad DQT length")
            self.state.dqt[tq] = q
            off += 64

    def _parse_dht(self, payload: bytes) -> None:
        off = 0
        while off < len(payload):
            tc_th = payload[off]
            off += 1
            tc, th = tc_th >> 4, tc_th & 0x0F
            bits = list(payload[off : off + 16])
            off += 16
            nvals = sum(bits)
            values = list(payload[off : off + nvals])
            off += nvals
            table = HuffmanTable(tc, th, bits, values)
            if tc == 0:
                self.state.dht_dc[th] = table
            else:
                self.state.dht_ac[th] = table

    def _consume_entropy_coded_data(self) -> Tuple[List[bytes], int]:
        chunks: List[bytes] = []
        chunk = bytearray()
        while True:
            if self.pos >= len(self.buf):
                raise JPEGParseError("Unexpected EOF in entropy-coded data")
            b = self._read_u8()
            if b != 0xFF:
                chunk.append(b)
                continue
            if self.pos >= len(self.buf):
                raise JPEGParseError("Unexpected EOF after 0xFF")
            m = self._read_u8()
            if m == 0x00:
                chunk.append(0xFF)
                continue
            if RST0 <= m <= RST7:
                chunks.append(bytes(chunk))
                chunk = bytearray()
                chunks.append(bytes([0xFF, m]))  # marker token
                continue
            # next marker begins; do not include current marker in entropy bytes
            chunks.append(bytes(chunk))
            return chunks, m


# -----------------------------------------------------------------------------
# Baseline entropy-coded editor
# -----------------------------------------------------------------------------


@dataclass
class ScanGeometry:
    frame_components: List[FrameComponent]
    scan_components: List[ScanComponent]
    max_h: int
    max_v: int
    n_mcu_h: int
    n_mcu_v: int
    dc_tables: List[HuffmanTable]
    ac_tables: List[HuffmanTable]
    hs: List[int]
    vs: List[int]


class BaselineEntropyEditor:
    def __init__(self, state: JPEGState, mask: np.ndarray):
        if state.sof0 is None or state.sos is None:
            raise JPEGParseError("Need SOF0 and SOS")
        self.state = state
        self.mask = np.asarray(mask).astype(bool)
        self.geom = self._build_geometry(state)
        h, w = self.mask.shape[:2]
        if h != state.sof0.height or w != state.sof0.width:
            raise ValueError(
                f"Mask shape {self.mask.shape} does not match JPEG image size "
                f"({state.sof0.height}, {state.sof0.width})"
            )

        # Per-component target quantized DC for a flat black replacement block.
        # JPEG uses a level shift of 128 before DCT, so a constant black block
        # corresponds to a spatial value of -128 everywhere and an unquantized
        # DC coefficient of -1024. For color JPEG, assume the first component is
        # luminance-like (Y) and remaining components are chroma-like (Cb/Cr),
        # so black is Y=0, Cb=128, Cr=128. That yields a black DC target for the
        # first component and a neutral 0 target for the others.
        self._black_target_dc: List[int] = []
        ncomp = len(self.geom.frame_components)
        for i, fc in enumerate(self.geom.frame_components):
            qtbl = self.state.dqt.get(fc.tq)
            if qtbl is None or len(qtbl) == 0:
                raise JPEGParseError(
                    f"Missing quant table for component {i}, tq={fc.tq}"
                )
            q00 = int(qtbl[0])
            if ncomp == 1:
                target = int(round(-1024.0 / q00))
            else:
                target = int(round(-1024.0 / q00)) if i == 0 else 0
            self._black_target_dc.append(target)

    @staticmethod
    def _build_geometry(state: JPEGState) -> ScanGeometry:
        sof = state.sof0
        sos = state.sos
        frame_by_id = {c.component_id: c for c in sof.components}
        frame_components = [frame_by_id[sc.component_id] for sc in sos.components]
        max_h = max(c.h for c in frame_components)
        max_v = max(c.v for c in frame_components)
        n_mcu_h = math.ceil(sof.width / (8 * max_h))
        n_mcu_v = math.ceil(sof.height / (8 * max_v))
        dc_tables = [state.dht_dc[sc.td] for sc in sos.components]
        ac_tables = [state.dht_ac[sc.ta] for sc in sos.components]
        hs = [fc.h for fc in frame_components]
        vs = [fc.v for fc in frame_components]
        return ScanGeometry(
            frame_components=frame_components,
            scan_components=sos.components,
            max_h=max_h,
            max_v=max_v,
            n_mcu_h=n_mcu_h,
            n_mcu_v=n_mcu_v,
            dc_tables=dc_tables,
            ac_tables=ac_tables,
            hs=hs,
            vs=vs,
        )

    @staticmethod
    def _receive_extend(bitreader: BitReader, ssss: int) -> Tuple[int, int]:
        if ssss == 0:
            return 0, 0
        bits = bitreader.read_bits(ssss)
        if bits < (1 << (ssss - 1)):
            value = bits - ((1 << ssss) - 1)
        else:
            value = bits
        return value, bits

    @staticmethod
    def _size_and_bits(value: int) -> Tuple[int, int]:
        if value == 0:
            return 0, 0
        magnitude = abs(value)
        ssss = magnitude.bit_length()
        if value < 0:
            bits = (value - 1) & ((1 << ssss) - 1)
        else:
            bits = value
        return ssss, bits

    def _redaction_decision(
        self,
        col_mcu: int,
        row_mcu: int,
        this_h: int,
        this_v: int,
        h_idx: int,
        v_idx: int,
    ) -> bool:
        # Mirrors PixelMed's block-level decision in image coordinates.
        h_mcu_size = 8 * self.geom.max_h
        v_mcu_size = 8 * self.geom.max_v
        h_block_size = 8 * self.geom.max_h // this_h
        v_block_size = 8 * self.geom.max_v // this_v

        x0 = col_mcu * h_mcu_size + h_idx * h_block_size
        y0 = row_mcu * v_mcu_size + v_idx * v_block_size
        x1 = min(self.state.sof0.width, x0 + h_block_size)
        y1 = min(self.state.sof0.height, y0 + v_block_size)
        if x0 >= x1 or y0 >= y1:
            return False
        return bool(self.mask[y0:y1, x0:x1].any())

    def _write_eob(self, writer: BitWriter, ac_table: HuffmanTable) -> None:
        if ac_table.eob_code is None or ac_table.eob_size is None:
            raise JPEGParseError("AC table missing EOB")
        writer.write_bits(ac_table.eob_code, ac_table.eob_size)

    def _process_one_block(
        self,
        bitreader: BitReader,
        bitwriter: BitWriter,
        dc_table: HuffmanTable,
        ac_table: HuffmanTable,
        redact: bool,
        first_redaction: bool,
        was_redacting: bool,
        original_dc: int,
        component_index: int,
    ) -> int:
        # Decode the original DC coefficient difference from the source stream.
        ssss = dc_table.decode(bitreader)
        dc_diff, _dc_bits = self._receive_extend(bitreader, ssss)
        updated_original_dc = original_dc + dc_diff

        target_dc = self._black_target_dc[component_index]

        if redact:
            # On entry to a redacted run, jump the emitted predictor to the black
            # target. Thereafter keep the predictor constant across consecutive
            # redacted blocks for the component.
            new_dc_diff = (target_dc - original_dc) if first_redaction else 0
        else:
            # When leaving a redacted run, bridge from the black target predictor
            # back to the real source predictor for this block.
            new_dc_diff = (
                (updated_original_dc - target_dc) if was_redacting else dc_diff
            )

        new_ssss, new_bits = self._size_and_bits(new_dc_diff)
        dc_table.encode(bitwriter, new_ssss)
        if new_ssss > 0:
            bitwriter.write_bits(new_bits, new_ssss)

        if redact:
            self._write_eob(bitwriter, ac_table)

        # Still must consume the original AC stream to stay synchronized.
        i = 1
        while i < 64:
            symbol = ac_table.decode(bitreader)
            if symbol == 0x00:
                break
            if symbol == 0xF0:
                i += 16
                if not redact:
                    ac_table.encode(bitwriter, symbol)
                continue
            run = symbol >> 4
            ssize = symbol & 0x0F
            if ssize == 0:
                raise JPEGParseError("Invalid AC symbol with zero size")
            ac_value_bits = bitreader.read_bits(ssize)
            i += run + 1
            if not redact:
                ac_table.encode(bitwriter, symbol)
                bitwriter.write_bits(ac_value_bits, ssize)

        if not redact and symbol == 0x00:
            ac_table.encode(bitwriter, 0x00)

        return updated_original_dc

    def process_entropy_segment(
        self, ecs_bytes: bytes, mcu_count: int, mcu_offset: int
    ) -> bytes:
        bitreader = BitReader(ecs_bytes)
        bitwriter = BitWriter()

        n_components = len(self.geom.scan_components)
        original_dc = [0] * n_components
        was_redacting = [False] * n_components

        for m in range(mcu_count):
            absolute_mcu = mcu_offset + m
            row_mcu = absolute_mcu // self.geom.n_mcu_h
            col_mcu = absolute_mcu % self.geom.n_mcu_h

            for c in range(n_components):
                this_h = self.geom.hs[c]
                this_v = self.geom.vs[c]
                dc_table = self.geom.dc_tables[c]
                ac_table = self.geom.ac_tables[c]
                for v_idx in range(this_v):
                    for h_idx in range(this_h):
                        redact = self._redaction_decision(
                            col_mcu, row_mcu, this_h, this_v, h_idx, v_idx
                        )
                        first_redaction = redact and not was_redacting[c]
                        original_dc[c] = self._process_one_block(
                            bitreader,
                            bitwriter,
                            dc_table,
                            ac_table,
                            redact,
                            first_redaction,
                            was_redacting[c],
                            original_dc[c],
                            c,
                        )
                        was_redacting[c] = redact

        bitwriter.flush()
        return bitwriter.getvalue()


# -----------------------------------------------------------------------------
# High-level JPEG rewrite
# -----------------------------------------------------------------------------


def _serialize_segment(marker: int, payload: Optional[bytes]) -> bytes:
    out = bytearray([0xFF, marker])
    if marker not in STANDALONE_MARKERS:
        if payload is None:
            raise ValueError(f"Marker 0x{marker:02X} requires payload")
        out.extend((len(payload) + 2).to_bytes(2, "big"))
        out.extend(payload)
    return bytes(out)


def redact_baseline_jpeg_bytes(jpeg_bytes: bytes, mask: np.ndarray) -> bytes:
    """
    Selectively redact a baseline JPEG using a pixel mask.

    Parameters
    ----------
    jpeg_bytes:
        Full JPEG bitstream.
    mask:
        2D boolean or 0/1 ndarray in decompressed image coordinates.

    Returns
    -------
    bytes
        Full rewritten JPEG bitstream.
    """
    parser = JPEGParser(jpeg_bytes)
    state, _original, entropy_chunks = parser.parse()
    if state.sof0 is None or state.sos is None:
        raise JPEGParseError("Missing SOF0/SOS")
    if state.sof0.precision != 8:
        raise JPEGParseError("Only 8-bit baseline supported")
    if len(state.marker_segments_before_sos) == 0:
        raise JPEGParseError("Malformed JPEG")

    editor = BaselineEntropyEditor(state, mask)
    n_total_mcus = editor.geom.n_mcu_h * editor.geom.n_mcu_v
    restart_interval = state.restart_interval

    # entropy_chunks alternates between ECS-bytes and optional restart marker tokens
    rewritten_entropy = bytearray()
    mcu_offset = 0
    ecs_index = 0
    while ecs_index < len(entropy_chunks):
        ecs = entropy_chunks[ecs_index]
        if ecs.startswith(b"\xff") and len(ecs) == 2 and RST0 <= ecs[1] <= RST7:
            # should not happen first; preserve defensively
            rewritten_entropy.extend(ecs)
            ecs_index += 1
            continue

        if restart_interval > 0:
            remaining = max(0, n_total_mcus - mcu_offset)
            mcu_count = min(restart_interval, remaining)
        else:
            mcu_count = n_total_mcus - mcu_offset

        rewritten_entropy.extend(
            editor.process_entropy_segment(ecs, mcu_count, mcu_offset)
        )
        mcu_offset += mcu_count
        ecs_index += 1

        if ecs_index < len(entropy_chunks):
            token = entropy_chunks[ecs_index]
            if (
                token.startswith(b"\xff")
                and len(token) == 2
                and RST0 <= token[1] <= RST7
            ):
                rewritten_entropy.extend(token)
                ecs_index += 1

    out = bytearray()
    for marker, payload in state.marker_segments_before_sos:
        out.extend(_serialize_segment(marker, payload))
    out.extend(rewritten_entropy)
    for marker, payload in state.trailer_segments:
        out.extend(_serialize_segment(marker, payload))
    return bytes(out)


# -----------------------------------------------------------------------------
# pydicom helpers
# -----------------------------------------------------------------------------


def redact_encapsulated_baseline_jpeg_frames(ds, masks: Sequence[np.ndarray]):
    """
    Return a copy of a DICOM dataset whose encapsulated JPEG Baseline frames have
    been selectively redacted using supplied pixel masks.

    Parameters
    ----------
    ds:
        pydicom Dataset with encapsulated PixelData.
    masks:
        One mask per frame, each shaped (Rows, Columns).

    Returns
    -------
    Dataset
        Copy of ds with rewritten encapsulated PixelData.
    """
    if pydicom is None:
        raise ImportError("pydicom is required for DICOM helpers")

    out = ds.copy()
    rows = int(ds.Rows)
    cols = int(ds.Columns)
    frames = list(
        generate_frames(
            ds.PixelData, number_of_frames=int(getattr(ds, "NumberOfFrames", 1))
        )
    )
    if len(frames) != len(masks):
        raise ValueError(f"Need {len(frames)} masks, got {len(masks)}")

    redacted_frames: List[bytes] = []
    for i, (frame_bytes, mask) in enumerate(zip(frames, masks)):
        arr = np.asarray(mask)
        if arr.shape[:2] != (rows, cols):
            raise ValueError(f"Mask {i} has shape {arr.shape}, expected {(rows, cols)}")
        redacted_frames.append(
            redact_baseline_jpeg_bytes(frame_bytes, arr.astype(bool))
        )

    out.PixelData = encapsulate(redacted_frames)
    out[0x7FE00010].is_undefined_length = True
    return out


# -----------------------------------------------------------------------------
# Notebook-friendly utilities
# -----------------------------------------------------------------------------


def rectangular_mask(
    height: int, width: int, rectangles: Iterable[Tuple[int, int, int, int]]
) -> np.ndarray:
    """
    Build a boolean mask from rectangles as (x, y, w, h).
    """
    mask = np.zeros((height, width), dtype=bool)
    for x, y, w, h in rectangles:
        x0 = _clamp_int(int(x), 0, width)
        y0 = _clamp_int(int(y), 0, height)
        x1 = _clamp_int(int(x + w), 0, width)
        y1 = _clamp_int(int(y + h), 0, height)
        if x0 < x1 and y0 < y1:
            mask[y0:y1, x0:x1] = True
    return mask


def expand_mask_to_jpeg_units(mask: np.ndarray, jpeg_bytes: bytes) -> np.ndarray:
    """
    Optional convenience: expand a pixel mask to the exact block footprints used by
    the baseline selective-redaction decision. This is *not* required for the core
    algorithm, but can be useful for visualization or QA.
    """
    parser = JPEGParser(jpeg_bytes)
    state, _, _ = parser.parse()
    editor = BaselineEntropyEditor(state, mask)
    expanded = np.zeros_like(editor.mask, dtype=bool)
    for row_mcu in range(editor.geom.n_mcu_v):
        for col_mcu in range(editor.geom.n_mcu_h):
            for c, (this_h, this_v) in enumerate(zip(editor.geom.hs, editor.geom.vs)):
                h_mcu_size = 8 * editor.geom.max_h
                v_mcu_size = 8 * editor.geom.max_v
                h_block_size = 8 * editor.geom.max_h // this_h
                v_block_size = 8 * editor.geom.max_v // this_v
                for v_idx in range(this_v):
                    for h_idx in range(this_h):
                        if editor._redaction_decision(
                            col_mcu, row_mcu, this_h, this_v, h_idx, v_idx
                        ):
                            x0 = col_mcu * h_mcu_size + h_idx * h_block_size
                            y0 = row_mcu * v_mcu_size + v_idx * v_block_size
                            x1 = min(state.sof0.width, x0 + h_block_size)
                            y1 = min(state.sof0.height, y0 + v_block_size)
                            expanded[y0:y1, x0:x1] = True
    return expanded
