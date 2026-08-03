"""Wire format v1: frame header, OXC1 container, and the deterministic
primitives (fnv1a, splitmix32, dlog) that sender and receiver must agree on
bit-exactly.

Everything here is wire format. Changing any constant or operation order is a
breaking change; the golden vectors in tests/ exist to catch that.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass

import zstandard

MAGIC0 = 0x4F  # "O"
MAGIC1 = 0x58  # "X"
VERSION = 1
FRAME_HEADER_LEN = 24

CONTAINER_MAGIC = b"OXC1"
CONTAINER_HEADER_LEN = 4 + 1 + 2 + 4 + 4 + 32  # 47
FLAG_ZSTD = 1

MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_SOURCE_BLOCKS = 0xFFFF

_U32 = 0xFFFFFFFF


def fnv1a(data: bytes) -> int:
    h = 0x811C9DC5
    for b in data:
        h ^= b
        h = (h * 0x01000193) & _U32
    return h


def splitmix32(seed: int):
    """Deterministic 32-bit PRNG; integer ops only. Returns a callable
    yielding uniform u32 values."""
    s = seed & _U32

    def rnd() -> int:
        nonlocal s
        s = (s + 0x9E3779B9) & _U32
        t = s ^ (s >> 16)
        t = (t * 0x21F0AAAD) & _U32
        t ^= t >> 15
        t = (t * 0x735A2D97) & _U32
        t ^= t >> 15
        return t

    return rnd


def frame_seed(session_id: int, seq: int) -> int:
    """Mix (session, seq) into a splitmix32 seed. Integer port of the
    decimen frameSeed mixer."""
    h = ((((session_id + 1) & _U32) * 0x9E3779B1) & _U32) ^ ((seq + 0x85EBCA6B) & _U32)
    h = ((h ^ (h >> 13)) * 0xC2B2AE35) & _U32
    return (h ^ (h >> 16)) & _U32


_LN2 = 0.6931471805599453


def dlog(x: float) -> float:
    """Deterministic natural log using only exactly-specified IEEE-754 ops
    (+,-,*,/ are correctly rounded; math.log is libm-dependent and differs
    across platforms by ulps, which would desync the soliton CDF)."""
    e = 0
    m = x
    while m >= 1.5:
        m /= 2
        e += 1
    while m < 0.75:
        m *= 2
        e -= 1
    z = (m - 1) / (m + 1)
    z2 = z * z
    term = z
    total = 0.0
    for n in range(1, 22, 2):
        total += term / n
        term *= z2
    return e * _LN2 + 2 * total


@dataclass(frozen=True)
class FrameHeader:
    session_id: int
    seq: int
    k: int
    block_len: int
    total_len: int
    payload_fnv: int

    @property
    def identity(self) -> tuple:
        """Everything except seq: disagreement means a different stream."""
        return (self.session_id, self.k, self.block_len, self.total_len, self.payload_fnv)


_FRAME_STRUCT = struct.Struct("<BBBBIIHHII")


def pack_frame(h: FrameHeader, block: bytes) -> bytes:
    if len(block) != h.block_len:
        raise ValueError("block length mismatch")
    return (
        _FRAME_STRUCT.pack(
            MAGIC0, MAGIC1, VERSION, 0,
            h.session_id, h.seq, h.k, h.block_len, h.total_len, h.payload_fnv,
        )
        + block
    )


def parse_frame(data: bytes) -> tuple[FrameHeader, bytes] | None:
    """Returns (header, block) or None for anything that isn't a valid v1
    frame. The optical channel is untrusted: reject, never raise."""
    if len(data) <= FRAME_HEADER_LEN:
        return None
    m0, m1, ver, _res, session_id, seq, k, block_len, total_len, payload_fnv = (
        _FRAME_STRUCT.unpack_from(data, 0)
    )
    if m0 != MAGIC0 or m1 != MAGIC1 or ver != VERSION:
        return None
    if k == 0 or block_len == 0 or total_len == 0:
        return None
    if len(data) != FRAME_HEADER_LEN + block_len:
        return None
    h = FrameHeader(session_id, seq, k, block_len, total_len, payload_fnv)
    return h, data[FRAME_HEADER_LEN:]


def safe_file_name(name: str) -> str:
    for sep in ("\\", "/"):
        name = name.split(sep)[-1]
    name = "".join(c for c in name if ord(c) >= 0x20 and ord(c) != 0x7F).strip()
    if name in ("", ".", ".."):
        return "transfer.bin"
    return name


def pack_file(name: str, data: bytes) -> bytes:
    """Build the OXC1 container: zstd is applied only when it wins by a real
    margin, and goodput accounting elsewhere uses ORIGINAL bytes."""
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"file exceeds {MAX_FILE_BYTES} bytes")
    name_b = safe_file_name(name).encode("utf-8")
    if len(name_b) > 0xFFFF:
        name_b = name_b[:0xFFFF]
    flags = 0
    payload = data
    if len(data) >= 512:
        compressed = zstandard.ZstdCompressor(level=10).compress(data)
        if len(compressed) + 64 < len(data):
            payload = compressed
            flags |= FLAG_ZSTD
    sha = hashlib.sha256(data).digest()
    header = (
        CONTAINER_MAGIC
        + struct.pack("<BHII", flags, len(name_b), len(data), len(payload))
        + sha
    )
    return header + name_b + payload


@dataclass(frozen=True)
class UnpackedFile:
    name: str
    data: bytes
    was_compressed: bool


def unpack_file(container: bytes) -> UnpackedFile:
    if len(container) < CONTAINER_HEADER_LEN:
        raise ValueError("container too short")
    if container[:4] != CONTAINER_MAGIC:
        raise ValueError("bad container magic")
    flags, name_len, orig_size, sent_size = struct.unpack_from("<BHII", container, 4)
    if flags & ~FLAG_ZSTD:
        raise ValueError("unknown container flags")
    sha = container[15:47]
    off = CONTAINER_HEADER_LEN
    name = container[off : off + name_len].decode("utf-8")
    off += name_len
    payload = container[off : off + sent_size]
    if len(payload) != sent_size:
        raise ValueError("truncated container payload")
    if flags & FLAG_ZSTD:
        # max_output_size bounds decompression: the trailer size field of an
        # optically-received blob is untrusted.
        data = zstandard.ZstdDecompressor().decompress(payload, max_output_size=orig_size)
    else:
        data = payload
    if len(data) != orig_size:
        raise ValueError("size mismatch after decompression")
    if hashlib.sha256(data).digest() != sha:
        raise ValueError("SHA-256 verification failed")
    return UnpackedFile(safe_file_name(name), data, bool(flags & FLAG_ZSTD))
