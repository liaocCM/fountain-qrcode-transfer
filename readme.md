# fqt

[中文說明](readme.zh.md)

Transfer files with a screen and a camera — one side plays an animated QR
stream, the other films it. No network, no pairing.

Measured: **~42 KB/s** on a Logitech C270, **~195 KB/s** with an iPhone as
camera ([details](docs/results.md)).

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

Prebuilt wheels only, no compiler. macOS / Windows / Linux.

## Use

Sender:

```bash
fqt send yourfile.pdf
```

Keys while running: `[` `]` fps · `g` codes per frame · `p` bytes per code · `q` quit.

Receiver:

```bash
fqt recv --preview --out received/
```

Aim with the preview window, then watch the status line: `sharp` is focus
(100+ usable, 300+ crisp), `yield` is decode rate. File is SHA-256 verified
before saving.

Tips that matter:

- Prop the camera; handheld kills throughput
- Run the sender fps **offset from** the camera fps (camera 30 → sender 40)
- More light = shorter exposure = fewer wasted frames

Find the best config for your hardware automatically — with a 30 fps camera,
start scanning around a 40 fps sender:

```bash
fqt sweep --camera 0 --kb 300 --configs "40:2x1:far,40:2x2:far,36:2x2:far"
```

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

Hence unbuilt: streaming 10 GB overnight works on paper, but a cable does it
in minutes. This channel's value is *transferring with no connection at all*,
not moving big files.

## How it works

The channel is one-way — the receiver can't ask for retransmission. Fountain
coding solves this: each frame is an XOR mix of the file's blocks, derived
from the frame number. Collect *enough distinct frames in any order* and the
file falls out. A missed frame costs a moment, never correctness.

On top of that, three throughput tricks:

- First pass sends raw blocks directly — a clean channel pays ~0 coding overhead
- Up to 3×2 QR codes per displayed frame
- Retransmissions rotate blocks across screen positions, so a camera artifact
  that always kills one region can't starve the same blocks forever

Decode is zxing-cpp (C++); Python only orchestrates. Wire format and design
decisions: [docs/design.md](docs/design.md).

MIT license.
