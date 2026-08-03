"""Systematic LT fountain code.

seq < k  → the frame IS source block `seq` (degree 1). A receiver with decent
frame yield finishes the first pass with ~0% overhead — this recovers the
15–40% overhead plain LT pays at realistic k (see docs/design.md §5).

seq >= k → classic LT repair: XOR of a pseudorandom block subset, degree drawn
from a robust-soliton distribution, everything derived deterministically from
(session_id, seq). Any distinct frames eventually decode via peeling.

Block XOR runs on numpy uint64 views; blocks are padded to 8-byte multiples.
"""

from __future__ import annotations

import math

import numpy as np

from .protocol import dlog, frame_seed, splitmix32

SOLITON_C = 0.1
SOLITON_DELTA = 0.5


def soliton_cdf(k: int) -> np.ndarray:
    """Robust-soliton CDF over degrees 1..k as float64. Wire format: built
    with dlog, not math.log."""
    cdf = np.zeros(k, dtype=np.float64)
    if k == 1:
        cdf[0] = 1.0
        return cdf
    # math.sqrt is correctly rounded per IEEE-754 (unlike pow(x, 0.5))
    R = max(1.0, SOLITON_C * dlog(k / SOLITON_DELTA) * math.sqrt(k))
    spike = min(k, math.ceil(k / R))
    total = 0.0
    for d in range(1, k + 1):
        rho = 1.0 / k if d == 1 else 1.0 / (d * (d - 1))
        tau = 0.0
        if d < spike:
            tau = R / (d * k)
        elif d == spike:
            tau = R * max(0.0, dlog(R / SOLITON_DELTA)) / k
        total += rho + tau
        cdf[d - 1] = total
    cdf /= total
    cdf[k - 1] = 1.0
    return cdf


def repair_indices(k: int, cdf: np.ndarray, session_id: int, seq: int) -> list[int]:
    """Block subset for repair frame seq (seq >= k). Deterministic on both
    ends; never called for source frames."""
    rnd = splitmix32(frame_seed(session_id, seq))
    u = rnd() * 2.0**-32
    lo, hi = 0, k - 1
    while lo < hi:
        mid = (lo + hi) >> 1
        if cdf[mid] >= u:
            hi = mid
        else:
            lo = mid + 1
    d = min(k, lo + 1)
    if d > k >> 3:
        scratch = list(range(k))
        out = []
        for i in range(d):
            j = i + rnd() % (k - i)
            scratch[i], scratch[j] = scratch[j], scratch[i]
            out.append(scratch[i])
        return out
    seen: dict[int, None] = {}
    while len(seen) < d:
        seen.setdefault(rnd() % k)
    return list(seen)


def frame_indices(k: int, cdf: np.ndarray, session_id: int, seq: int) -> list[int]:
    """Pass 0 (seq < k): systematic sweep. After that, odd passes are soliton
    repair and even passes are ROTATED systematic re-sweeps: pass 2p shifts
    blocks by p positions, so grid-position-correlated loss (rolling shutter
    killing the same cell every frame) starves different blocks each cycle
    instead of the same ones forever."""
    if seq < k:
        return [seq]
    p, i = divmod(seq, k)
    if p % 2 == 0:
        return [(i + (p >> 1)) % k]
    return repair_indices(k, cdf, session_id, seq)


def _pad_words(block_len: int) -> int:
    return (block_len + 7) // 8


class LTEncoder:
    def __init__(self, payload: bytes, block_len: int, session_id: int):
        self.block_len = block_len
        self.session_id = session_id
        self.k = max(1, -(-len(payload) // block_len))
        self.words = _pad_words(block_len)
        # each block gets an 8-byte-aligned slot; tail padding is zeros
        self.blocks = np.zeros((self.k, self.words), dtype=np.uint64)
        blk_bytes = self.blocks.view(np.uint8).reshape(self.k, self.words * 8)
        src = np.zeros(self.k * block_len, dtype=np.uint8)
        src[: len(payload)] = np.frombuffer(payload, dtype=np.uint8)
        blk_bytes[:, :block_len] = src.reshape(self.k, block_len)
        self.cdf = soliton_cdf(self.k)

    def encode(self, seq: int) -> bytes:
        idx = frame_indices(self.k, self.cdf, self.session_id, seq)
        if len(idx) == 1:
            out = self.blocks[idx[0]]
        else:
            out = np.bitwise_xor.reduce(self.blocks[idx], axis=0)
        return out.tobytes()[: self.block_len]


class _Pending:
    __slots__ = ("idx", "words")

    def __init__(self, idx: set[int], words: np.ndarray):
        self.idx = idx
        self.words = words


class LTDecoder:
    def __init__(self, k: int, block_len: int, session_id: int, total_len: int):
        self.k = k
        self.block_len = block_len
        self.session_id = session_id
        self.total_len = total_len
        self.words = _pad_words(block_len)
        self.cdf = soliton_cdf(k)
        self.solved: list[np.ndarray | None] = [None] * k
        self.by_block: dict[int, set[_Pending]] = {}
        self.seen: set[int] = set()
        self.solved_count = 0
        self.frames_new = 0
        self.frames_dup = 0

    @property
    def is_complete(self) -> bool:
        return self.solved_count >= self.k

    def add_frame(self, seq: int, block: bytes) -> bool:
        """Returns True when the frame contributed anything new."""
        if seq in self.seen:
            self.frames_dup += 1
            return False
        self.seen.add(seq)
        self.frames_new += 1
        if self.is_complete:
            return False
        idx = set(frame_indices(self.k, self.cdf, self.session_id, seq))
        buf = np.zeros(self.words * 8, dtype=np.uint8)
        buf[: self.block_len] = np.frombuffer(block[: self.block_len], dtype=np.uint8)
        words = buf.view(np.uint64)
        for b in list(idx):
            s = self.solved[b]
            if s is not None:
                words ^= s
                idx.discard(b)
        if not idx:
            return True
        if len(idx) == 1:
            self._resolve(idx.pop(), words)
            return True
        pf = _Pending(idx, words)
        for b in idx:
            self.by_block.setdefault(b, set()).add(pf)
        return True

    def _resolve(self, b0: int, w0: np.ndarray) -> None:
        stack = [(b0, w0)]
        while stack:
            b, w = stack.pop()
            if self.solved[b] is not None:
                continue
            self.solved[b] = w
            self.solved_count += 1
            waiting = self.by_block.pop(b, None)
            if not waiting:
                continue
            for pf in waiting:
                pf.words ^= w
                pf.idx.discard(b)
                if len(pf.idx) == 1:
                    r = next(iter(pf.idx))
                    peers = self.by_block.get(r)
                    if peers is not None:
                        peers.discard(pf)
                    if self.solved[r] is None:
                        stack.append((r, pf.words))

    def assemble(self) -> bytes | None:
        if not self.is_complete:
            return None
        out = bytearray(self.total_len)
        for b in range(self.k):
            start = b * self.block_len
            n = min(self.block_len, self.total_len - start)
            if n > 0:
                out[start : start + n] = self.solved[b].view(np.uint8)[:n].tobytes()
        return bytes(out)
