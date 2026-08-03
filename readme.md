# fqt

[中文說明](readme.zh.md)

Transfer files with a screen and a camera — one side plays an animated QR
stream, the other films it. No network, no pairing.

Measured: **~42 KB/s** on a Logitech C270 (a $20 fixed-focus webcam,
1280×960 @ 30 fps), **~195 KB/s** with an iPhone as camera
([details](docs/results.md)).

The iPhone number isn't rigorously benchmarked yet and should go higher — the
camera only delivered 24–30 fps in those runs; at its 60 fps the ceiling doubles.

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

Prebuilt wheels only, no compiler. macOS / Windows / Linux.

## Use

Sender (example pairs a 40 fps stream with a 30 fps camera — adjust fps to
your camera, rule of thumb in the tips below):

```bash
fqt send yourfile.pdf --fps 40
```

Keys while running: `[` `]` fps · `g` codes per frame · `p` bytes per code · `q` quit.

Receiver:

```bash
fqt recv --fps 30 --preview --out received/
```

Aim with the preview window, then watch the status line: `sharp` is focus
(100+ usable, 300+ crisp), `yield` is decode rate. File is SHA-256 verified
before saving.

Tips that matter:

- Prop the camera; handheld kills throughput
- Run the sender fps **offset from** the camera fps (camera 30 → sender 40)
- More light = shorter exposure = fewer wasted frames

Find the best config for your hardware automatically:

```bash
fqt sweep --camera 0 --kb 300 --configs "40:2x1:far,40:2x2:far,36:2x2:far"
```

## Theoretical ceilings by camera

Ceiling = payload per frame × camera fps. Resolution decides how big a QR grid
fits (each module needs ~3 camera pixels); fps decides how many frames per
second you can collect:

| camera | grid that fits | ceiling | 10 MB takes (theor.) | measured |
|---|---|---|---|---|
| 480p @ 30fps | 1×1 far | ~42 KB/s | ~4 min | — |
| 720p @ 30fps | 2×1 far | ~84 KB/s | ~2 min | — |
| C270 (1280×960 @ 30fps) | 2×2 far | ~169 KB/s | ~1 min | 42 KB/s |
| 1080p @ 30fps | 3×2 far | ~253 KB/s | ~40 s | — |
| iPhone (1920×1440 @ 30fps) | 3×2 close | ~515 KB/s | ~20 s | ~195 KB/s |
| same at a full 60fps | 3×2 close | ~1 MB/s | ~10 s | untested |
| 4K @ 60fps | 4×3 close | ~2 MB/s | ~5 s | untested |

Real-world lands at 20–50% of ceiling — the gap is yield (exposures straddling
frame flips), light, and focus.

## Limits

- Sweet spot ≤ 10 MB (seconds to minutes). 50 MB workable. **190 MB hard cap**
  (header fields: `k` u16, `totalLen` u32). Text/JSON compresses first, so it
  goes several times faster
- Whole file is reassembled in receiver RAM
- Interrupting the receiver loses progress
- Not encrypted: anyone filming your screen receives the same bytes

All liftable (v2 header widening, segmented transfers, checkpoint resume) —
after which size has no practical cap. But the speed is still ~195 KB/s, and
time is the real wall:

| file | wait (at iPhone speed) |
|---|---|
| 500 MB | ~45 min |
| 4 GB | ~6 h |
| 10 GB | ~15 h |

## How it works

### The one-way channel problem

Screen to camera is strictly one-way: the receiver can't say "block 37 again,
please". The naive scheme — loop the file's blocks — punishes every miss with
a full cycle of waiting: with 1000 blocks and one missing, you wait ~500
frames on average, and it gets worse the closer you are to done.

### Fountain coding: make every frame useful

Split the file into k blocks. Each frame is *not* one block — it's the XOR of
a pseudorandom subset of blocks, with the subset derived from the frame
number. Sender and receiver run the same derivation, so the receiver knows
what any frame contains just by reading its number — no negotiation, and
joining mid-stream works.

Decoding is solving simultaneous equations:

- A frame that mixes just one block hands you that block directly
- Every solved block gets XOR-ed out of the other frames, simplifying them;
  when a frame drops to one unknown, it yields a new block → a chain reaction
  that avalanches to completion

Mathematically you need "k frames plus a little". Order doesn't matter,
duplicates don't matter, misses don't matter — a shaky shot costs half a
second, never a corrupt byte.

### Three throughput tricks in this implementation

1. **Systematic first pass**: the first k frames ARE the raw blocks (read the
   file out once, plainly) — a clean channel pays ~zero coding overhead.
   Mixing only kicks in afterwards, to patch the holes.
2. **Multi-code grid**: each QR is an independent frame; 3×2 per screen = 6×
   the capacity.
3. **Rotated re-sweeps**: rolling-shutter cameras tend to ruin the same screen
   region every shot. If a block always sits in that region it starves
   forever — so re-sweeps rotate blocks across positions, and nothing stays
   blocked for good.

### Why QR error correction is set to minimum

QR's built-in ECC fixes "one smudged corner". Our failure mode is "the whole
shot is bad" — and a bad shot is simply discarded, the fountain absorbs it.
So in-code ECC runs at its lowest level (L) and the space goes to payload.
Two layers, two jobs: ECC handles smudges, the fountain handles losses,
neither pays for the other's insurance.

### Integrity, and where the bottleneck lives

Every frame header carries the file's fingerprint; the assembled result must
pass FNV-1a then SHA-256 — one wrong bit fails. Content is zstd-compressed
first, so text moves several times faster. The bottleneck is almost always
the camera: an exposure that straddles a display flip captures two frames
ghosted together, and that shot is garbage — which is why "bright room,
propped camera, detuned fps" beats any software optimization.

Decode is zxing-cpp (C++); Python only orchestrates. Wire format and design
decisions: [docs/design.md](docs/design.md).

MIT license.
