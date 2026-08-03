"""Continuous parameter sweep: sender window + camera receiver in ONE process.

Prop the camera once, run `fqt sweep`, walk away. Each round streams a fresh
random payload under a different (fps, grid, profile) config; the receiver
auto-locks (new session/fnv per round), and the round ends on verified
completion or timeout. Results print as a table at the end.

GUI (sender window) stays on the main thread — macOS requirement; capture and
decode run in daemon threads, same structure as recv.py.
"""

from __future__ import annotations

import os
import secrets
import threading
import time
from dataclasses import dataclass

import cv2
import numpy as np

from . import protocol
from .fountain import LTDecoder
from .fountain import LTEncoder
from .qr import read_frames
from .recv import open_camera
from .send import FrameProducer

PROFILES = {"close": 2927, "far": 1439}


@dataclass
class RoundResult:
    label: str
    ok: bool
    seconds: float
    goodput_kbs: float
    cam_fps: float
    yield_pct: float
    overhead: float
    solved: int
    k: int


@dataclass
class _Shared:
    """State shared between capture/decode threads and the main loop."""

    lock: threading.Lock
    expect_fnv: int = 0
    decoder: LTDecoder | None = None
    stream_key: tuple | None = None
    captured: int = 0
    decoded_codes: int = 0
    done: threading.Event = None  # set per round
    preview: np.ndarray | None = None  # latest camera frame; shown by main thread


def _capture_loop(cap, shared: _Shared, stop: threading.Event, workers: int):
    free = threading.Semaphore(workers)

    def job(gray):
        try:
            payloads = read_frames(gray)
            if not payloads:
                return
            with shared.lock:
                shared.decoded_codes += len(payloads)
                for raw in payloads:
                    parsed = protocol.parse_frame(raw)
                    if parsed is None:
                        continue
                    h, block = parsed
                    if h.payload_fnv != shared.expect_fnv:
                        continue  # stale frames from the previous round
                    if shared.decoder is None or shared.stream_key != h.identity:
                        shared.decoder = LTDecoder(h.k, h.block_len, h.session_id, h.total_len)
                        shared.stream_key = h.identity
                    d = shared.decoder
                    d.add_frame(h.seq, block)
                    if d.is_complete:
                        payload = d.assemble()
                        if payload is not None and protocol.fnv1a(payload) == h.payload_fnv:
                            shared.done.set()
        finally:
            free.release()

    while not stop.is_set():
        ok, frame = cap.read()
        if not ok:
            time.sleep(0.005)
            continue
        with shared.lock:
            shared.captured += 1
        shared.preview = frame
        if not free.acquire(blocking=False):
            continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        threading.Thread(target=job, args=(gray,), daemon=True).start()


def run_sweep(
    camera: int = 1,
    width: int = 1920,
    cam_fps: float = 60.0,
    kb: int = 500,
    display_px: int = 950,
    workers: int = 3,
    configs: list[tuple[float, tuple[int, int], str]] | None = None,
    timeout: float = 0.0,
    window: str = "fqt-sweep",
    show_preview: bool = True,
) -> None:
    if configs is None:
        configs = [
            (12, (1, 1), "close"),
            (12, (2, 2), "close"),
            (15, (2, 2), "close"),
            (24, (2, 2), "close"),
            (15, (2, 2), "far"),
            (30, (2, 2), "close"),
        ]

    cap = open_camera(camera)
    if not cap.isOpened():
        raise SystemExit(f"cannot open camera {camera}")
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, round(width * 3 / 4))
    cap.set(cv2.CAP_PROP_FPS, cam_fps)
    print(f"sweep: camera {camera} at {int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))}x"
          f"{int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))}@{cap.get(cv2.CAP_PROP_FPS):g}, "
          f"{kb} KB per round, {len(configs)} configs. q in the window aborts.")

    shared = _Shared(lock=threading.Lock())
    stop = threading.Event()
    cap_thread = threading.Thread(
        target=_capture_loop, args=(cap, shared, stop, workers), daemon=True
    )
    cap_thread.start()

    cv2.namedWindow(window, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(window, display_px, display_px)
    if show_preview:
        cv2.namedWindow("fqt-cam", cv2.WINDOW_NORMAL)
        cv2.resizeWindow("fqt-cam", 320, 240)

    results: list[RoundResult] = []
    preview_at = 0.0
    aborted = False
    try:
        for fps, grid, profile in configs:
            cols, rows = grid
            # profile is a named preset or a literal byte count ("1000")
            block_len = PROFILES[profile] if profile in PROFILES else int(profile)
            label = f"{fps:g}fps {cols}x{rows} {profile}"
            payload = os.urandom(kb * 1024)
            container = protocol.pack_file("sweep.bin", payload)
            fnv = protocol.fnv1a(container)
            session = secrets.randbelow(0xFFFFFFFE) + 1
            enc = LTEncoder(container, block_len, session)

            def header_of(seq, _s=session, _e=enc, _bl=block_len, _cl=len(container), _f=fnv):
                return protocol.FrameHeader(_s, seq, _e.k, _bl, _cl, _f)

            with shared.lock:
                shared.expect_fnv = fnv
                shared.decoder = None
                shared.stream_key = None
                shared.done = threading.Event()
                shared.captured = 0
                shared.decoded_codes = 0
            producer = FrameProducer(enc, header_of, grid, "L")
            producer.start()

            round_timeout = timeout or max(30.0, kb / 4.0)
            interval = 1.0 / fps
            t_start = time.perf_counter()  # timeout reference
            t0 = t_start  # goodput reference; re-based to first lock below
            locked = False
            next_at = t_start
            status_at = t_start + 2.0
            sized = False
            print(f"\n[{label}] k={enc.k} "
                  f"ceiling {cols*rows*block_len*fps/1024:.0f} KB/s, "
                  f"timeout {round_timeout:.0f}s")
            while True:
                now = time.perf_counter()
                if shared.done.is_set() or now - t_start > round_timeout:
                    break
                if not locked:
                    with shared.lock:
                        if shared.decoder is not None:
                            locked = True
                            t0 = now  # exclude AF/exposure settling from goodput
                            shared.captured = 0  # restart cam/yield stats cleanly
                            shared.decoded_codes = 0
                            print(f"  locked after {now - t_start:.1f}s")
                if now >= next_at:
                    img = producer.get()
                    if img is not None:
                        # round, not floor: a smaller code should upscale to
                        # roughly the same physical size (fatter modules), not
                        # render physically smaller
                        scale = max(1, round(display_px / img.shape[0]))
                        big = cv2.resize(img, None, fx=scale, fy=scale,
                                         interpolation=cv2.INTER_NEAREST)
                        if not sized:
                            # window matches image aspect exactly: no stretch blur
                            cv2.resizeWindow(window, big.shape[1], big.shape[0])
                            sized = True
                        cv2.imshow(window, big)
                        next_at += interval
                        if now - next_at > 3 * interval:
                            next_at = now + interval
                    else:
                        next_at = now + interval
                if now >= status_at:
                    status_at = now + 1.0
                    elapsed = now - t0
                    with shared.lock:
                        d = shared.decoder
                        cam = shared.captured / max(0.1, elapsed)
                        ypct = 100.0 * shared.decoded_codes / max(1, shared.captured)
                    if d is None:
                        line = f"  {elapsed:4.0f}s | no lock yet | cam {cam:.0f} fps"
                    else:
                        # frame-driven progress (peeling back-loads block solves)
                        target = d.k * 1.05
                        frac = min(0.99, d.frames_new / target)
                        rate = d.frames_new / max(0.1, elapsed)  # unique frames/s
                        eta = (target - d.frames_new) / rate if rate > 0 else 0
                        kbs = d.frames_new * d.block_len / 1024 / max(0.1, elapsed)
                        line = (f"  {elapsed:4.0f}s [{frac*100:5.1f}%] "
                                f"{d.frames_new}/{d.k} fr solved {d.solved_count} | "
                                f"cam {cam:.0f} fps yield {ypct:.0f}% | "
                                f"~{kbs:.0f} KB/s eta {max(0, eta):.0f}s")
                    print("\r" + line + " " * 6, end="", flush=True)
                if show_preview and now >= preview_at:
                    f = shared.preview
                    if f is not None:
                        cv2.imshow("fqt-cam", cv2.resize(f, (320, 240)))
                    preview_at = now + 0.25  # 4 Hz is plenty for aiming
                if (cv2.waitKey(1) & 0xFF) in (ord("q"), 27):
                    aborted = True
                    break
            elapsed = time.perf_counter() - t0
            producer.shutdown()
            with shared.lock:
                d = shared.decoder
                ok = shared.done.is_set()
                captured = shared.captured
                decoded = shared.decoded_codes
            results.append(RoundResult(
                label=label,
                ok=ok,
                seconds=elapsed,
                goodput_kbs=(kb / elapsed) if ok else 0.0,
                cam_fps=captured / elapsed if elapsed > 0 else 0.0,
                yield_pct=100.0 * decoded / max(1, captured),
                overhead=(d.frames_new / d.k) if d and d.frames_new else 0.0,
                solved=d.solved_count if d else 0,
                k=enc.k,
            ))
            r = results[-1]
            print(f"\n  -> {'OK  ' if r.ok else 'DNF '} {r.goodput_kbs:6.1f} KB/s | "
                  f"cam {r.cam_fps:.0f} fps | yield {r.yield_pct:.0f}% | "
                  f"overhead {r.overhead:.2f}x | solved {r.solved}/{r.k}")
            if aborted:
                break
            # brief blank between rounds so exposure resets
            cv2.imshow(window, np.full((400, 400), 255, dtype=np.uint8))
            cv2.waitKey(400)
    finally:
        stop.set()
        cap.release()
        cv2.destroyAllWindows()

    print("\n== sweep results ==")
    print(f"{'config':<20} {'result':<6} {'KB/s':>7} {'cam fps':>8} {'yield%':>7} {'ovh':>6}")
    for r in results:
        print(f"{r.label:<20} {'OK' if r.ok else 'DNF':<6} "
              f"{r.goodput_kbs:>7.1f} {r.cam_fps:>8.0f} {r.yield_pct:>7.0f} "
              f"{r.overhead:>6.2f}")
    if results:
        best = max(results, key=lambda r: r.goodput_kbs)
        if best.goodput_kbs > 0:
            print(f"\nbest: {best.label} at {best.goodput_kbs:.1f} KB/s")
