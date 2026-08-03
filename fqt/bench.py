"""Camera-less loopback benchmark: sender pipeline → (optional synthetic
degradation) → receiver pipeline. Measures the two purpose-built numbers the
survey says don't exist publicly (Appendix C #1/#2): per-frame decode cost on
the real workload, and end-to-end frame yield / goodput.
"""

from __future__ import annotations

import os
import secrets
import time

import cv2
import numpy as np

from . import protocol
from .fountain import LTDecoder, LTEncoder
from .qr import compose_grid, make_qr, read_frames


def degrade(img: np.ndarray, mode: str) -> np.ndarray:
    """Simulate the optical channel in the order it physically happens:
    display upscale (hard pixels) → perspective at display resolution →
    camera downsample to ~3 px/module → lens blur → sensor noise."""
    if mode == "none":
        return img
    if mode != "camera":
        raise ValueError(f"unknown degradation mode {mode!r}")
    display = cv2.resize(img, None, fx=6, fy=6, interpolation=cv2.INTER_NEAREST)
    h, w = display.shape
    pts_src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    j = 0.005 * w  # slight off-axis camera
    pts_dst = pts_src + np.float32([[j, j], [-j, j * 0.5], [-j * 0.5, -j], [j, -j]])
    M = cv2.getPerspectiveTransform(pts_src, pts_dst)
    display = cv2.warpPerspective(display, M, (w, h), borderValue=255)
    cam = cv2.resize(display, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)  # 3 px/module
    cam = cv2.GaussianBlur(cam, (3, 3), 0.6)
    noise = np.random.default_rng(1234).normal(0, 4, cam.shape)
    return np.clip(cam.astype(np.float32) + noise, 0, 255).astype(np.uint8)


def run_bench(
    size_kb: int = 512,
    block_len: int = 2927,
    grid: tuple[int, int] = (1, 1),
    ec_level: str = "L",
    mode: str = "camera",
    loss: float = 0.0,
) -> None:
    cols, rows = grid
    per_frame = cols * rows
    data = os.urandom(size_kb * 1024)
    container = protocol.pack_file("bench.bin", data)
    fnv = protocol.fnv1a(container)
    session = secrets.randbelow(0xFFFFFFFE) + 1
    enc = LTEncoder(container, block_len, session)
    print(f"bench: {size_kb} KB random, k={enc.k}, {cols}x{rows} grid, "
          f"{block_len} B/code, ecc {ec_level}, channel={mode}, loss={loss:.0%}")

    dec = LTDecoder(enc.k, block_len, session, len(container))
    rng = np.random.default_rng(42)

    gen_t = 0.0
    read_t = 0.0
    frames_shown = 0
    codes_found = 0
    seq = 0
    t_start = time.perf_counter()
    while not dec.is_complete:
        t0 = time.perf_counter()
        cells = []
        for s in range(seq, seq + per_frame):
            block = enc.encode(s)
            h = protocol.FrameHeader(session, s, enc.k, block_len, len(container), fnv)
            cells.append(make_qr(protocol.pack_frame(h, block), ec_level))
        seq += per_frame
        img = compose_grid(cells, cols, rows) if per_frame > 1 else cells[0]
        gen_t += time.perf_counter() - t0
        frames_shown += 1

        if loss > 0 and rng.random() < loss:
            continue
        ch = degrade(img, mode)
        t0 = time.perf_counter()
        payloads = read_frames(ch)
        read_t += time.perf_counter() - t0
        codes_found += len(payloads)
        for raw in payloads:
            parsed = protocol.parse_frame(raw)
            if parsed:
                hh, block = parsed
                dec.add_frame(hh.seq, block)
        if seq > enc.k * 40 + 400:
            raise SystemExit("bench did not converge — decoder yield too low")
    total = time.perf_counter() - t_start

    payload = dec.assemble()
    assert payload is not None and protocol.fnv1a(payload) == fnv
    unpacked = protocol.unpack_file(payload)
    assert unpacked.data == data

    sent_codes = frames_shown * per_frame
    print(f"  round trip OK (SHA-256 verified)")
    print(f"  QR generate: {gen_t/sent_codes*1e3:6.2f} ms/code "
          f"({sent_codes/gen_t:6.0f} codes/s single-thread)")
    print(f"  QR decode:   {read_t/max(1,frames_shown)*1e3:6.2f} ms/captured-frame "
          f"({codes_found/max(1e-9,read_t):6.0f} codes/s single-thread)")
    print(f"  code yield through channel: {100.0*codes_found/max(1,sent_codes):.1f}%")
    print(f"  fountain overhead: {dec.frames_new/enc.k:.3f}x "
          f"({dec.frames_new} frames for k={enc.k})")
    for fps in (30, 60):
        eff = per_frame * block_len * fps * (codes_found / max(1, sent_codes)) / (dec.frames_new / enc.k)
        print(f"  projected goodput at {fps} fps display: {eff/1024:7.1f} KB/s (container bytes)")
    print(f"  wall time (single-thread, no camera): {total:.2f} s")
