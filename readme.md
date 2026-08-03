# fqt — file transfer via screen and camera

[中文說明](readme.zh.md)

One machine plays an animated QR stream on its screen, another points a camera
at it, and the file comes across. No network, no pairing — the only channel
between the two devices is light.

Measured: **41.9 KB/s** on a Logitech C270 webcam, **~195 KB/s** with an
iPhone as the camera. Full numbers in [docs/results.md](docs/results.md).

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

All dependencies (numpy, opencv, zxing-cpp, zstandard) ship as prebuilt
wheels — no compiler needed. Works on macOS / Windows / Linux.

## Send

```bash
fqt send yourfile.pdf
```

A window opens and starts streaming. Put it on your brightest screen.

Live keys while it runs:

- `[` / `]` — fps down / up
- `g` — QR codes per frame (1x1 → 2x1 → 2x2 → 3x2; more is faster if the camera can resolve them)
- `p` — bytes per code (close = max density, far = for distant or weaker cameras)
- `q` — quit

The status bar below the code shows the current config and its ceiling.
Changing grid/profile restarts the transfer (the receiver follows
automatically); changing fps doesn't interrupt anything.

## Receive

```bash
fqt recv --preview --out received/
```

`--preview` opens a small camera view for aiming. Then watch the terminal:

- `sharp` — focus quality: 100+ usable, 300+ crisp. Move the camera until it peaks
- `yield` — codes decoded per captured frame; low means aim, focus, or light is the problem
- multiple cameras: `--camera 1`, `--camera 2`, …

The file is SHA-256 verified before saving. Restarting the sender mid-transfer
is fine — the receiver re-locks on its own.

Field-tested tips:

- Prop the camera on something. Handheld kills throughput
- **Don't run the sender at the camera's fps** — detune it (camera at 30 → sender at 40). Matched rates phase-lock the sampling and turn speed into a coin flip
- Fixed-focus webcams (like the C270) have one sharp distance, ~40 cm. Slide until `sharp` peaks
- More room light = shorter exposures = fewer wasted frames

## Find your setup's best config

```bash
fqt sweep --camera 0 --kb 300 --configs "30:2x1:far,40:2x1:far,40:2x2:far"
```

Runs sender + receiver in one process, tests each config as a real transfer,
prints a results table. Config format is `fps:grid:profile`; profile also
accepts a raw byte count. No camera at hand? `fqt bench --kb 512 --grid 2x2
--loss 0.2` exercises the pipeline synthetically.

## How it works

Same concept as [decimen-optical-transfer](https://github.com/bashalarmistalt/decimen-optical-transfer):
fountain coding over an animated QR stream — every frame is some XOR
combination of the file's blocks, so the receiver just needs *enough* distinct
frames, in any order. Missed frames cost a little time, never correctness.

What this implementation changes:

- **Systematic first pass** — the first k frames are the raw blocks, so a good
  channel pays ~zero coding overhead; LT repair kicks in only for what was missed
- **Multi-code grid** — up to 3x2 QR codes per displayed frame
- **Rotated re-sweeps** — rolling shutter tends to kill the same screen region
  every frame; retransmission cycles blocks through different positions so no
  block starves forever
- Decode is zxing-cpp (C++) on grayscale with unneeded options off; Python
  only orchestrates

Design decisions and wire format: [docs/design.md](docs/design.md). Background
research: [docs/research-survey.md](docs/research-survey.md),
[docs/decimen-review.md](docs/decimen-review.md).

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

## License

MIT
