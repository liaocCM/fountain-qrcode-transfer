"""Sender: file → OXC1 container → systematic LT stream → animated QR grid.

Pacing is an absolute schedule (next += interval, with a catch-up clamp) so a
slow tick never accumulates drift and never bursts. Frames are produced ahead
of the display loop by a small thread pool — zxing-cpp releases the GIL during
QR creation, so producer threads genuinely overlap.
"""

from __future__ import annotations

import queue
import secrets
import threading
import time
from pathlib import Path

import cv2
import numpy as np

from . import protocol
from .fountain import LTEncoder
from .qr import compose_grid, make_qr

LOOKAHEAD = 4


class FrameProducer:
    """Generates composed grid images ahead of the display loop."""

    def __init__(self, encoder: LTEncoder, header_of, grid: tuple[int, int], ec_level: str):
        self.encoder = encoder
        self.header_of = header_of
        self.cols, self.rows = grid
        self.ec_level = ec_level
        self.per_frame = self.cols * self.rows
        self.q: queue.Queue[np.ndarray] = queue.Queue(maxsize=LOOKAHEAD)
        self.seq = 0
        self.stop = threading.Event()
        self.threads = [threading.Thread(target=self._run, daemon=True) for _ in range(2)]
        self.lock = threading.Lock()

    def start(self):
        for t in self.threads:
            t.start()

    def _next_seqs(self) -> list[int]:
        with self.lock:
            first = self.seq
            self.seq += self.per_frame
        return list(range(first, first + self.per_frame))

    def _run(self):
        while not self.stop.is_set():
            seqs = self._next_seqs()
            cells = []
            for s in seqs:
                block = self.encoder.encode(s)
                payload = protocol.pack_frame(self.header_of(s), block)
                cells.append(make_qr(payload, self.ec_level))
            img = compose_grid(cells, self.cols, self.rows) if self.per_frame > 1 else cells[0]
            while not self.stop.is_set():
                try:
                    self.q.put(img, timeout=0.25)
                    break
                except queue.Full:
                    continue

    def get(self) -> np.ndarray | None:
        try:
            return self.q.get_nowait()
        except queue.Empty:
            return None

    def shutdown(self):
        self.stop.set()
        for t in self.threads:
            t.join(timeout=1.0)


FPS_STEPS = [5, 10, 12, 15, 20, 24, 30, 60]
GRID_STEPS = [(1, 1), (2, 1), (2, 2), (3, 2)]
PROFILE_STEPS = [("close", 2927), ("far", 1439)]


def _hud(img: np.ndarray, text: str, scale: int) -> np.ndarray:
    """Append a status bar BELOW the code (outside the quiet zone)."""
    big = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
    bar = np.full((34, big.shape[1]), 255, dtype=np.uint8)
    cv2.putText(bar, text, (6, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.55, 0, 1, cv2.LINE_AA)
    return np.vstack([big, bar])


def run_sender(
    path: str,
    fps: float = 30.0,
    block_len: int = 2927,
    grid: tuple[int, int] = (1, 1),
    ec_level: str = "L",
    display_px: int = 900,
    window: str = "fqt-send",
) -> None:
    p = Path(path)
    data = p.read_bytes()
    container = protocol.pack_file(p.name, data)
    payload_fnv = protocol.fnv1a(container)

    fps_i = min(range(len(FPS_STEPS)), key=lambda i: abs(FPS_STEPS[i] - fps))
    grid_i = GRID_STEPS.index(grid) if grid in GRID_STEPS else 0
    prof_i = 0 if block_len >= 2000 else 1
    if block_len not in (v for _, v in PROFILE_STEPS):
        PROFILE_STEPS.insert(0, ("custom", block_len))
        prof_i = 0

    producer: FrameProducer | None = None
    session_id = 0
    enc: LTEncoder | None = None

    def restart() -> str | None:
        """(Re)build encoder+producer for current settings. New session id →
        the receiver resets itself. Returns an error string on bad config."""
        nonlocal producer, session_id, enc
        bl = PROFILE_STEPS[prof_i][1]
        k = max(1, -(-len(container) // bl))
        if k > protocol.MAX_SOURCE_BLOCKS:
            return f"k={k} exceeds {protocol.MAX_SOURCE_BLOCKS}: file too big for {bl} B blocks"
        if producer is not None:
            producer.shutdown()
        session_id = secrets.randbelow(0xFFFFFFFF - 1) + 1
        enc = LTEncoder(container, bl, session_id)

        def header_of(seq: int, _s=session_id, _e=enc, _bl=bl):
            return protocol.FrameHeader(_s, seq, _e.k, _bl, len(container), payload_fnv)

        producer = FrameProducer(enc, header_of, GRID_STEPS[grid_i], ec_level)
        producer.start()
        cols, rows = GRID_STEPS[grid_i]
        print(f"stream: {cols}x{rows} x {bl} B @ {FPS_STEPS[fps_i]} fps = "
              f"{cols*rows*bl*FPS_STEPS[fps_i]/1024:.0f} KB/s ceiling "
              f"(k={enc.k}, session {session_id:08x})")
        return None

    err = restart()
    if err:
        raise SystemExit(err)

    print(f"fqt send: {p.name} {len(data)/1024:.1f} KB "
          f"(container {len(container)/1024:.1f} KB)")
    print("keys: [ ] fps | g grid | p profile | r restart | q quit")

    cv2.namedWindow(window, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(window, display_px, display_px + 40)

    next_at = time.perf_counter()
    shown = 0
    try:
        while True:
            interval = 1.0 / FPS_STEPS[fps_i]
            now = time.perf_counter()
            if now >= next_at:
                img = producer.get()
                if img is not None:
                    scale = max(1, round(display_px / img.shape[0]))
                    cols, rows = GRID_STEPS[grid_i]
                    hud = (f"{FPS_STEPS[fps_i]}fps {cols}x{rows} "
                           f"{PROFILE_STEPS[prof_i][0]} "
                           f"{cols*rows*PROFILE_STEPS[prof_i][1]*FPS_STEPS[fps_i]/1024:.0f}KB/s "
                           f"[ ]=fps g=grid p=profile q=quit")
                    cv2.imshow(window, _hud(img, hud, scale))
                    shown += 1
                    next_at += interval
                    if now - next_at > 3 * interval:  # fell behind: don't burst
                        next_at = now + interval
                else:
                    next_at = now + interval  # producer starved; skip slot
            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), 27):
                break
            elif key == ord("["):
                fps_i = max(0, fps_i - 1)
            elif key == ord("]"):
                fps_i = min(len(FPS_STEPS) - 1, fps_i + 1)
            elif key == ord("g"):
                grid_i = (grid_i + 1) % len(GRID_STEPS)
                err = restart()
                if err:
                    print(err)
                    grid_i = (grid_i - 1) % len(GRID_STEPS)
                    restart()
            elif key == ord("p"):
                prof_i = (prof_i + 1) % len(PROFILE_STEPS)
                err = restart()
                if err:
                    print(err)
                    prof_i = (prof_i - 1) % len(PROFILE_STEPS)
                    restart()
            elif key == ord("r"):
                restart()
    finally:
        if producer is not None:
            producer.shutdown()
        cv2.destroyAllWindows()
        print(f"stopped after {shown} frames")
