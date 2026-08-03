"""fqt test suite: wire-format pins, fountain round-trips under loss/reorder,
container integrity, and full QR loopback."""

import os
import random

import numpy as np
import pytest

from fqt import protocol
from fqt.fountain import LTDecoder, LTEncoder, frame_indices, soliton_cdf
from fqt.qr import compose_grid, make_qr, read_frames

# --- deterministic primitives -----------------------------------------------


def test_fnv1a_known_vectors():
    assert protocol.fnv1a(b"") == 0x811C9DC5
    assert protocol.fnv1a(b"a") == 0xE40C292C
    assert protocol.fnv1a(b"foobar") == 0xBF9CF968


def test_splitmix32_deterministic():
    r1 = protocol.splitmix32(12345)
    r2 = protocol.splitmix32(12345)
    seq1 = [r1() for _ in range(10)]
    seq2 = [r2() for _ in range(10)]
    assert seq1 == seq2
    assert all(0 <= v <= 0xFFFFFFFF for v in seq1)


def test_dlog_close_to_math_log():
    import math

    for x in [0.5, 0.75, 1.0, 1.5, 2.0, 10.0, 716.0, 65535.0]:
        assert abs(protocol.dlog(x) - math.log(x)) < 1e-14 * max(1, abs(math.log(x)))


def test_soliton_cdf_well_formed():
    for k in (1, 2, 17, 179, 716):
        cdf = soliton_cdf(k)
        assert cdf.shape == (k,)
        assert cdf[-1] == 1.0
        assert np.all(np.diff(cdf) >= 0)
        assert cdf[0] > 0  # degree-1 mass exists or peeling never starts


def test_frame_indices_systematic_and_repair():
    k = 179
    cdf = soliton_cdf(k)
    for seq in range(k):  # pass 0: identity sweep
        assert frame_indices(k, cdf, 4242, seq) == [seq]
    for seq in range(k, 2 * k):  # pass 1: soliton repair, valid subsets
        idx = frame_indices(k, cdf, 4242, seq)
        assert 1 <= len(idx) <= k
        assert len(set(idx)) == len(idx)
        assert all(0 <= i < k for i in idx)
    for i in range(k):  # pass 2: re-sweep rotated by 1
        assert frame_indices(k, cdf, 4242, 2 * k + i) == [(i + 1) % k]
    for i in range(k):  # pass 4: rotated by 2
        assert frame_indices(k, cdf, 4242, 4 * k + i) == [(i + 2) % k]
    # session separation in the repair pass
    assert any(
        frame_indices(k, cdf, 1, s) != frame_indices(k, cdf, 2, s)
        for s in range(k, k + 20)
    )


# --- frame header ------------------------------------------------------------


def test_frame_pack_parse_roundtrip():
    h = protocol.FrameHeader(0xDEADBEEF, 0x01020304, 0x0111, 6, 0x00FEDCBA, 0x89ABCDEF)
    wire = protocol.pack_frame(h, bytes([1, 2, 3, 4, 5, 6]))
    assert len(wire) == protocol.FRAME_HEADER_LEN + 6
    parsed = protocol.parse_frame(wire)
    assert parsed is not None
    h2, block = parsed
    assert h2 == h
    assert block == bytes([1, 2, 3, 4, 5, 6])


def test_frame_rejects_garbage():
    h = protocol.FrameHeader(1, 0, 4, 8, 100, 5)
    wire = bytearray(protocol.pack_frame(h, bytes(8)))
    assert protocol.parse_frame(bytes(wire[:10])) is None  # truncated
    bad = bytes([0xFF]) + bytes(wire[1:])
    assert protocol.parse_frame(bad) is None  # magic
    assert protocol.parse_frame(bytes(wire) + b"x") is None  # length
    assert protocol.parse_frame(os.urandom(100)) is None


# --- container ---------------------------------------------------------------


def test_container_roundtrip_compressible():
    data = b"hello fountain " * 1000
    c = protocol.pack_file("greeting.txt", data)
    u = protocol.unpack_file(c)
    assert u.name == "greeting.txt"
    assert u.data == data
    assert u.was_compressed
    assert len(c) < len(data)


def test_container_roundtrip_incompressible():
    data = os.urandom(4096)
    c = protocol.pack_file("noise.bin", data)
    u = protocol.unpack_file(c)
    assert u.data == data
    assert not u.was_compressed


def test_container_rejects_tamper():
    c = bytearray(protocol.pack_file("x.bin", os.urandom(2048)))
    c[-1] ^= 0xFF
    with pytest.raises(ValueError):
        protocol.unpack_file(bytes(c))


def test_safe_file_name():
    assert protocol.safe_file_name("../../etc/passwd") == "passwd"
    assert protocol.safe_file_name("C:\\evil\\x.exe") == "x.exe"
    assert protocol.safe_file_name("..") == "transfer.bin"
    assert protocol.safe_file_name("  \x00\x1f  ") == "transfer.bin"


# --- fountain round trips ----------------------------------------------------


def _roundtrip(payload: bytes, block_len: int, loss: float, seed: int) -> int:
    """Returns frames consumed."""
    session = 777
    enc = LTEncoder(payload, block_len, session)
    dec = LTDecoder(enc.k, block_len, session, len(payload))
    rng = random.Random(seed)
    seq = 0
    used = 0
    while not dec.is_complete:
        block = enc.encode(seq)
        if rng.random() >= loss:
            dec.add_frame(seq, block)
            used += 1
        seq += 1
        assert seq < enc.k * 50 + 200, "did not converge"
    assert dec.assemble() == payload
    return used


def test_roundtrip_exact_no_loss():
    payload = os.urandom(50 * 1024)
    enc_k = -(-len(payload) // 1441)
    used = _roundtrip(payload, 1441, 0.0, 1)
    # systematic: zero loss means exactly k frames, zero overhead
    assert used == enc_k


def test_roundtrip_sizes():
    for size in (7, 1441, 50_000, 300_000):
        payload = os.urandom(size)
        _roundtrip(payload, 1441, 0.0, 2)


def test_roundtrip_with_loss():
    payload = os.urandom(200 * 1024)
    used = _roundtrip(payload, 2929, 0.3, 3)
    k = -(-len(payload) // 2929)
    assert used < k * 1.7  # unique-frame overhead stays sane under 30% loss


def test_roundtrip_out_of_order():
    payload = os.urandom(80 * 1024)
    session = 99
    block_len = 1441
    enc = LTEncoder(payload, block_len, session)
    seqs = list(range(enc.k * 2))
    random.Random(4).shuffle(seqs)
    dec = LTDecoder(enc.k, block_len, session, len(payload))
    for s in seqs:
        if dec.is_complete:
            break
        dec.add_frame(s, enc.encode(s))
    assert dec.is_complete
    assert dec.assemble() == payload


def test_duplicates_harmless():
    payload = os.urandom(30 * 1024)
    enc = LTEncoder(payload, 1441, 5)
    dec = LTDecoder(enc.k, 1441, 5, len(payload))
    for s in range(enc.k):
        dec.add_frame(s, enc.encode(s))
        dec.add_frame(s, enc.encode(s))
    assert dec.is_complete
    assert dec.frames_dup == enc.k
    assert dec.assemble() == payload


def test_encoder_decoder_cross_session_differs():
    payload = os.urandom(30 * 1024)
    e1 = LTEncoder(payload, 1441, 1)
    e2 = LTEncoder(payload, 1441, 2)
    k = e1.k
    assert any(e1.encode(k + s) != e2.encode(k + s) for s in range(10))


# --- QR loopback -------------------------------------------------------------


def test_qr_binary_roundtrip():
    payload = os.urandom(2929 + protocol.FRAME_HEADER_LEN - 24) + bytes(24)
    img = make_qr(payload[:2929], "L")
    found = read_frames(img)
    assert found == [payload[:2929]]


def test_grid_roundtrip():
    payloads = [os.urandom(1441) for _ in range(4)]
    cells = [make_qr(p, "L") for p in payloads]
    img = compose_grid(cells, 2, 2)
    found = read_frames(img)
    assert sorted(found) == sorted(payloads)


def test_full_pipeline_loopback():
    """File → container → fountain → QR grid → decode → reassemble → verify."""
    data = os.urandom(64 * 1024)
    container = protocol.pack_file("pipeline.bin", data)
    fnv = protocol.fnv1a(container)
    session = 31337
    block_len = 1441
    enc = LTEncoder(container, block_len, session)
    dec = LTDecoder(enc.k, block_len, session, len(container))
    seq = 0
    while not dec.is_complete:
        cells = []
        for s in range(seq, seq + 4):
            h = protocol.FrameHeader(session, s, enc.k, block_len, len(container), fnv)
            cells.append(make_qr(protocol.pack_frame(h, enc.encode(s)), "L"))
        seq += 4
        for raw in read_frames(compose_grid(cells, 2, 2)):
            parsed = protocol.parse_frame(raw)
            assert parsed is not None
            hh, block = parsed
            dec.add_frame(hh.seq, block)
        assert seq < enc.k + 100
    payload = dec.assemble()
    assert protocol.fnv1a(payload) == fnv
    u = protocol.unpack_file(payload)
    assert u.data == data
    assert u.name == "pipeline.bin"
