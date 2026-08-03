"""Receiver: camera → grayscale → threaded zxing-cpp decode → LT peeling →
verify → save.

Frame-yield instrumentation is first-class (survey Appendix C #1): the status
line reports captured fps, decoded fps, and new-symbol fps, because the yield
term — not decode speed — is where throughput is usually lost.

Frames are DROPPED when all decode workers are busy: a stale frame is worth
less than the next one, and the fountain absorbs the loss.
"""

from __future__ import annotations

import sys
import threading
import time
from collections import Counter, deque
from pathlib import Path

import cv2


def open_camera(index: int) -> cv2.VideoCapture:
    """Platform-appropriate capture backend: AVFoundation on macOS,
    MSMF→DirectShow on Windows (DSHOW catches virtual webcams like Camo/iVCam
    that MSMF sometimes misses), default (V4L2) elsewhere."""
    if sys.platform == "darwin":
        return cv2.VideoCapture(index, cv2.CAP_AVFOUNDATION)
    if sys.platform.startswith("win"):
        cap = cv2.VideoCapture(index, cv2.CAP_MSMF)
        if cap.isOpened():
            return cap
        return cv2.VideoCapture(index, cv2.CAP_DSHOW)
    return cv2.VideoCapture(index)

from . import protocol
from .fountain import LTDecoder
from .qr import read_frames


class Stats:
    def __init__(self):
        self.captured = 0
        self.submitted = 0
        self.decoded_codes = 0
        self.new_symbols = 0
        self.sharpness = 0.0
        self.codes_per_frame = Counter()  # histogram: how many QRs each capture yielded
        self.t0 = time.perf_counter()
        self.recent_new = deque(maxlen=120)  # (t, new_count) for ETA

    def rate(self, n: int) -> float:
        dt = time.perf_counter() - self.t0
        return n / dt if dt > 0 else 0.0


def run_receiver(
    camera: int = 0,
    width: int = 1280,
    fps: float = 60.0,
    workers: int = 3,
    out_dir: str = ".",
    show_preview: bool = False,
    dump_dir: str | None = None,
) -> None:
    cap = open_camera(camera)
    if not cap.isOpened():
        raise SystemExit(f"cannot open camera {camera}")
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, round(width * 3 / 4))
    cap.set(cv2.CAP_PROP_FPS, fps)
    got_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    got_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    got_fps = cap.get(cv2.CAP_PROP_FPS)
    print(f"fqt recv: camera {camera} at {got_w}x{got_h}@{got_fps:g} "
          f"(asked {width}x{round(width*3/4)}@{fps:g}), {workers} decode workers")

    stats = Stats()
    state_lock = threading.Lock()
    decoder: LTDecoder | None = None
    stream_key = None
    done = threading.Event()
    result_container: list[bytes] = []

    free_slots = threading.Semaphore(workers)

    def ingest(payloads: list[bytes]) -> None:
        nonlocal decoder, stream_key
        for raw in payloads:
            parsed = protocol.parse_frame(raw)
            if parsed is None:
                continue
            h, block = parsed
            with state_lock:
                stats.decoded_codes += 1
                if decoder is None or stream_key != h.identity:
                    decoder = LTDecoder(h.k, h.block_len, h.session_id, h.total_len)
                    stream_key = h.identity
                    stats.recent_new.clear()
                    print(f"\nlocked stream {h.session_id:08x}: k={h.k}, "
                          f"{h.block_len} B/block, {h.total_len/1024:.1f} KB container")
                if decoder.add_frame(h.seq, block):
                    stats.new_symbols += 1
                    stats.recent_new.append((time.perf_counter(), stats.new_symbols))
                if decoder.is_complete and not done.is_set():
                    payload = decoder.assemble()
                    if payload is not None and protocol.fnv1a(payload) == h.payload_fnv:
                        result_container.append(payload)
                        done.set()
                    else:
                        print("\nchecksum FAILED on assembled container; resetting")
                        decoder = None
                        stream_key = None

    if dump_dir:
        Path(dump_dir).mkdir(parents=True, exist_ok=True)

    def decode_job(gray) -> None:
        try:
            payloads = read_frames(gray)
            with state_lock:
                stats.codes_per_frame[len(payloads)] += 1
                n = stats.submitted
            # every 5th capture: save the REAL camera view + how many codes it
            # yielded, so decode preprocessing can be tuned offline
            if dump_dir and n % 5 == 0:
                cv2.imwrite(f"{dump_dir}/{n:06d}_n{len(payloads)}.png", gray)
            if payloads:
                ingest(payloads)
        finally:
            free_slots.release()

    threads: list[threading.Thread] = []
    preview_frame: list = [None]  # latest frame; GUI must run on the main thread (macOS)

    def capture_loop():
        while not done.is_set():
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.005)
                continue
            stats.captured += 1
            if show_preview:
                preview_frame[0] = frame
            if not free_slots.acquire(blocking=False):
                continue  # all workers busy: drop, don't queue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            if stats.submitted % 15 == 0:
                # focus metric on the center crop: Laplacian variance.
                # <50 is mush, 100+ workable, 300+ crisp.
                h0, w0 = gray.shape
                c = gray[h0 // 4 : 3 * h0 // 4, w0 // 4 : 3 * w0 // 4]
                stats.sharpness = float(cv2.Laplacian(c, cv2.CV_64F).var())
            stats.submitted += 1
            t = threading.Thread(target=decode_job, args=(gray,), daemon=True)
            t.start()
            threads.append(t)
            if len(threads) > 64:
                threads[:] = [x for x in threads if x.is_alive()]

    cap_thread = threading.Thread(target=capture_loop, daemon=True)
    cap_thread.start()

    try:
        last = ""
        next_status = time.perf_counter()
        while not done.is_set():
            if show_preview:
                f = preview_frame[0]
                if f is not None:
                    cv2.imshow("fqt-recv", f)
                cv2.waitKey(30)  # also services the window event loop
            else:
                time.sleep(0.5)
            if time.perf_counter() < next_status:
                continue
            next_status = time.perf_counter() + 0.5
            with state_lock:
                d = decoder
                if d is None:
                    line = (f"waiting for signal… captured {stats.rate(stats.captured):.0f} fps, "
                            f"decoded 0 codes, sharpness {stats.sharpness:.0f}")
                else:
                    # progress driven by frames, not solved blocks (peeling back-loads)
                    frac = min(0.99, d.frames_new / max(1, round(d.k * 1.05)))
                    yield_pct = 100.0 * stats.decoded_codes / max(1, stats.captured)
                    goodput = stats.new_symbols * d.block_len / 1024 / max(0.1, time.perf_counter() - stats.t0)
                    line = (f"[{frac*100:5.1f}%] frames {d.frames_new}/{d.k} "
                            f"(+{d.frames_dup} dup) solved {d.solved_count}/{d.k} | "
                            f"cam {stats.rate(stats.captured):.0f} fps, "
                            f"yield {yield_pct:.0f}%, sharp {stats.sharpness:.0f}, "
                            f"~{goodput:.0f} KB/s")
            if line != last:
                print("\r" + line + " " * 8, end="", flush=True)
                last = line
    except KeyboardInterrupt:
        print("\ninterrupted")
        return
    finally:
        done.set()
        cap.release()
        if show_preview:
            cv2.destroyAllWindows()

    container = result_container[0]
    elapsed = time.perf_counter() - stats.t0
    unpacked = protocol.unpack_file(container)
    out_parent = Path(out_dir)
    out_parent.mkdir(parents=True, exist_ok=True)
    out = out_parent / unpacked.name
    out.write_bytes(unpacked.data)
    print(f"\nDONE {unpacked.name}: {len(unpacked.data)/1024:.1f} KB in {elapsed:.1f} s "
          f"= {len(unpacked.data)/1024/elapsed:.1f} KB/s goodput (original bytes), "
          f"SHA-256 verified → {out}")
    hist = " ".join(f"{k}:{v}" for k, v in sorted(stats.codes_per_frame.items()))
    print(f"codes-per-capture histogram (grid diagnosis): {hist}")
