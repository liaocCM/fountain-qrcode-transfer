# fqt — Fountain QR Code Transfer

Send a file between two devices with a **screen and a camera** — no network,
no pairing. The concept comes from
[decimen-optical-transfer](https://github.com/bashalarmistalt/decimen-optical-transfer)
(~129 KB/s observed); this is a from-scratch implementation tuned for a higher
throughput ceiling. Design rationale and all measured evidence:
[`docs/design.md`](docs/design.md), [`docs/decimen-review.md`](docs/decimen-review.md),
[`docs/research-survey.md`](docs/research-survey.md).

## What's different (and why it's faster)

| lever | decimen | fqt |
|---|---|---|
| fountain code | LT only (15–40% overhead at real k) | **systematic LT**: source blocks first → ~0% overhead on a good channel, LT repair under loss |
| codes per frame | 1 | **N×M grid** (`--grid 2x2` = 4× payload/frame) |
| decoder options | zxing defaults (rotate/invert/downscale on) | all off — the code is upright and screen-rendered |
| pixel path | RGBA throughout | **grayscale end-to-end**, zero-copy into zxing-cpp |
| QR generation | JS, ~ms | zxing-cpp C++ (2.9 ms/code, GIL-free threads) |
| stack | browser JS + WASM | Python orchestration, every hot path native (zxing-cpp / OpenCV / numpy / zstd) |

Loopback benchmark through a synthetic camera channel (perspective + 3 px/module
downsample + blur + noise), Apple Silicon, single thread:

```
1x1 grid, 2927 B/code: 100% yield, 1.000x overhead → 171 KB/s projected @ 60 fps
2x2 grid, 2927 B/code: 100% yield, 1.000x overhead → 686 KB/s projected @ 60 fps
2x2 far profile, 30% frame loss: 1.35x overhead    → 177 KB/s projected @ 60 fps
```

Real-world numbers will be lower (camera fps and frame yield bind first — see
the survey: that's exactly where libcimbar loses 53%). The receiver prints
captured/decoded/yield rates live so you can see which term is limiting.

## Install

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
```

All dependencies are prebuilt wheels (numpy, opencv-python, zxing-cpp,
zstandard) — no compiler needed.

## Use

```bash
# sending machine (put the window on your brightest screen):
fqt send myfile.pdf                     # close-range phone default: v40, 30 fps
fqt send myfile.pdf --grid 2x2 --fps 60 # dense mode: 120 Hz screen, good camera
fqt send myfile.pdf --profile far       # monitors / webcams / distance

# receiving machine:
fqt recv                                # camera 0, saves into cwd
fqt recv --camera 1 --workers 3 --preview

# no camera needed — measure the pipeline:
fqt bench --kb 512 --grid 2x2 --loss 0.2
```

The receiver locks onto any stream it sees mid-flight, deduplicates, peels,
then verifies FNV-1a + SHA-256 before writing the file. Restarting the sender
resets the receiver automatically (new session id).

Practical notes
- tx fps should stay ≤ half your screen's refresh rate (30 on a 60 Hz panel).
- Prop the receiving phone/camera; autofocus hunting is the #1 yield killer.
- A fixed-focus webcam (e.g. Logitech C270: 720p/30fps, ~40 cm focus) wants
  `--profile far --fps 15` on the sender.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

20 tests: wire-format pins, fountain round-trips under loss/reorder/duplication,
container tamper rejection, and a full file→QR-grid→decode→verify loopback.

## Wire format

v1, deliberately not decimen-compatible (see design doc): 24-byte frame header
(`"OX"` magic, version, session, seq, k, blockLen, totalLen, FNV-1a) + block,
carried as QR byte-mode payload; OXC1 container inside (zstd when it wins,
SHA-256 of original bytes, sanitized filename). `seq < k` frames ARE the source
blocks (systematic); `seq ≥ k` are robust-soliton LT repair, derived
deterministically from (session, seq) with integer-only PRNG and a
deterministic log — no libm on the wire path.
