# fqt — fountain QR transfer: design

Goal: a screen→camera file transfer tool with a **higher goodput ceiling** than
decimen-optical-transfer (~129 KB/s observed, ~144 KB/s theoretical), built on
the same concept: animated QR frames + fountain coding, no back-channel.

Design inputs: [`decimen-review.md`](decimen-review.md) (full technical map of
the parent) and [`research-survey.md`](research-survey.md) (3-researcher
adversarially-verified survey). This doc records only the decisions.

## Language: Python (with every hot path in native code)

Measured on this machine before committing (see survey §2.5: published decoder
benchmarks are unusable; measure your own workload):

- `zxing-cpp` 3.1.1 wheel decodes a clean 2,900-byte QR embedded in a
  1280×960 gray frame in **1.1 ms** (~900 fps single-thread), and **releases
  the GIL** — 3.36× scaling on 4 threads.
- The same wheel *creates* a v40 QR from raw bytes in **3.7 ms** (C++, no
  pure-Python QR encoding anywhere).
- OpenCV 5.0 wheel provides AVFoundation camera capture and display.

So the decoder core is the same zxing-cpp C++ everyone else uses (survey §3.3:
the decode core is identical across stacks; any gap is capture path and
threading). Rust was the a-priori favorite but there is no Rust toolchain or
cmake on this machine, and the survey's conclusion — *the binding constraint is
frame yield, not language* — removes the reason to install one. Go was
rejected: no maintained zxing-cpp binding, camera capture requires cgo+OpenCV
anyway.

## What actually raises the ceiling (from the survey)

1. **Frame yield first** (§2.2: libcimbar wastes ~53% of displayed frames).
   → Receiver instruments captured / decoded / new-symbol rates (Appendix C #1)
   so tuning targets the real loss term. Decode pool drops frames rather than
   queueing (stale frames are worthless); capture thread never blocks on decode.
2. **Reader options off** (review §4.4: decimen leaves tryRotate/tryInvert/
   tryDownscale on). → `try_rotate=False, try_invert=False, try_downscale=False`.
3. **Grayscale end-to-end** (review §5.5: RGBA capture is 4× wasted bandwidth).
   → camera BGR→gray once, numpy view handed to zxing-cpp zero-copy.
4. **Multi-code grid** (the parent experiment's 186 KB/s used stacked codes).
   → N×M QRs per displayed frame, each an independent fountain packet.
   2×2 @ 30 fps × 2,929 B = **351 KB/s raw ceiling**, 2.4× decimen's.
5. **Systematic fountain** (review §2.8: LT overhead is 15–40% at realistic k,
   not the asymptotic 1.15). → seq < k emits source block `seq` directly
   (degree-1); seq ≥ k emits robust-soliton LT repair. In good conditions
   overhead → ~0; under loss it degrades to plain LT, never worse.
   RaptorQ was considered and rejected: survey §2.3 shows codec choice is not a
   throughput lever (both are 100× faster than the channel), and the `raptorq`
   wheel doesn't exist for macOS/arm64 — not worth a Rust toolchain install.
6. **ECC L / high-rate in-frame code** (survey §2.8: every 100+ KB/s system
   runs rate ≥ 0.8; the fountain layer handles erasures cheaper than parity).
7. **zstd before the fountain** (survey §2.8: compression is a real multiplier;
   goodput is reported in *original file bytes* to avoid libcimbar's
   post-compression reporting hazard).
8. **tx_fps ≤ refresh/2** (review §5.2: frames must own ≥2 refresh cycles).
   Default 30 fps for 60 Hz screens; `--fps 60` for 120 Hz ProMotion.

## Wire format v1 (deliberately NOT decimen-compatible)

Breaking format frees us from the float-CDF hazard class and the 20-byte
header's missing version field. Little-endian throughout.

**Frame** (one per QR code): 24-byte header + block.

| off | type | field |
|---|---|---|
| 0 | u8×2 | magic `4F 58` "OX" |
| 2 | u8 | version = 1 |
| 3 | u8 | reserved 0 |
| 4 | u32 | sessionId (random ≠0) |
| 8 | u32 | seq — `< k`: source block seq; `≥ k`: LT repair |
| 12 | u16 | k (source block count) |
| 14 | u16 | blockLen |
| 16 | u32 | totalLen (container length) |
| 20 | u32 | FNV-1a of container |

Stream identity = (sessionId, k, blockLen, totalLen, fnv) — any change resets
the receiver (decimen's collision lesson, review §1.2).

**Container**: `4F 58 43 31` "OXC1" | u8 flags (bit0=zstd) | u16 nameLen |
u32 origSize | u32 sentSize | 32B SHA-256(original) | name | payload.

**Fountain determinism**: splitmix32 PRNG + xxhash-style frame seed (ported
from decimen, integer-only). Robust soliton (c=0.1, δ=0.5) built with the
deterministic `dlog` (21-term atanh series) — `math.log` is libm-dependent and
the CDF is wire format (review §2.4). Golden vectors pin all of it.

## Architecture

```
fqt/
  protocol.py   frame header + OXC1 container + fnv1a/splitmix32/dlog
  fountain.py   systematic LT encoder/decoder (numpy uint64 XOR, peeling)
  qr.py         zxing-cpp QR create/read wrappers, grid compose/scan
  send.py       producer thread (frames ahead) → cv2 window, absolute-time pacing
  recv.py       capture thread → drop-don't-queue decode pool → LTDecoder → verify/save
  bench.py      camera-less loopback benchmark + synthetic degradation
  cli.py        fqt send / fqt recv / fqt bench
tests/          golden vectors, loss/reorder round-trips, QR loopback
```

Sender pacing: absolute schedule (`next += interval`, 3-interval catch-up
clamp) — decimen's drift-free loop (review §3.5). Producer keeps a small
lookahead queue; generation counter invalidates on restart.

Receiver: progress is **frame-driven**, never block-driven (LT peeling
back-loads; review §2.7). 100% only after SHA-256 verifies.

## Defaults

| knob | default | why |
|---|---|---|
| bytes/code | 2,929 (v40-L minus 24B header) | close-range phone ceiling |
| `--profile far` | 1,441 (v27-L) | arbitrary monitors / webcams |
| tx fps | 30 | 60 Hz screens own each frame ×2 refreshes |
| grid | 1×1 (`--grid 2x2` for dense) | yield before density (survey §2.2) |
| ECC | L | fountain handles erasure; parity is wasted modules |
| workers | 3 | decode is ~0.3 ms/frame threaded; camera-bound anyway |

Known receiver-side reality check: a Logitech C270 (this machine's USB cam)
caps at 30 fps / 720p / fixed focus → sender should run `--fps 15
--profile far` against it. A phone camera at 60 fps is the benchmark target.
