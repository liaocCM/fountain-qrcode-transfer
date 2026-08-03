# Measured results (2026-08-04, first real-hardware session)

Sender: MacBook Pro display. Receiver: two cameras, same `fqt` build.
All transfers SHA-256 verified. Goodput = original file bytes / wall time.

## Camera envelopes

| receiver | best config | best measured | notes |
|---|---|---|---|
| Logitech C270 (USB2, 1280×960@30, fixed focus, MJPEG) | sender 40 fps, 2×2 grid, far (1439 B) | **41.9 KB/s** (yield 149%) | started the night at 0.7 KB/s — 60× from tuning alone |
| iPhone via Continuity Camera (1920×1440 @ 24–30 delivered) | sender 30 fps, 3×2 grid, close (2927 B) | **~195 KB/s mid-run pace**; 49.8 KB/s full-run before the schedule fix | final confirmed number still pending one clean run |

Reference points: decimen-optical-transfer README claims 129 KB/s observed
(browser, phone-to-phone); its parent experiment ~186 KB/s propped.

## Findings not in the literature we surveyed

1. **Detune sender fps from camera fps.** With sender = camera rate (30/30),
   the camera samples at a FIXED phase relative to display flips: the whole
   run is either clean (yield 103%) or straddled (yield 34%) — a per-run coin
   flip. Offset rates (40 vs 30) rotate the phase continuously and give stable
   mid-high yield. Never run at the camera's rate or an integer multiple.
2. **Sender above camera rate wins.** Zero duplicate captures (every capture
   sees a fresh frame) beats straddle losses once the fountain layer makes
   losses cheap. 30 fps sender on a 24 fps iPhone was the best iPhone config;
   40 on the 30 fps C270 the best C270 config. The old `tx ≤ refresh/2` rule
   is dead in this architecture.
3. **Rotated systematic re-sweeps fix grid-positional loss.** Rolling-shutter
   straddle kills the same grid CELL every frame; with block→cell assignment
   fixed, the same blocks starve forever and the LT tail dominates (49.8 KB/s
   run: cruised at 114, spent half the wall time on the last 100 blocks).
   Fix (wire format): odd passes = soliton repair, even passes = systematic
   re-sweep rotated by pass/2, so bad positions starve different blocks each
   cycle.
4. **Straddle ghosting is unrecoverable in software.** A/B on 600 real C270
   captures: sharpen / 1.5–2× upscale / CLAHE / GlobalHistogram binarizer /
   try_downscale all within ±1% of baseline (471→472/600 codes). Failing
   frames are double-exposures of two display frames (ghosted finder
   patterns). The only levers are exposure time (ambient light; manual
   exposure on Windows DirectShow) and the fountain absorbing the loss.
5. **C270 delivers 1280×960 (4:3) via AVFoundation**, not its advertised
   720p — the extra 240 rows are what let a 2×2 far grid (270 modules,
   ~2.8 px/module) squeak through at 149% yield.
6. **Density cliff is sharp**: 2.8 px/module works (149% yield), ~2.5 fails
   (19%). Sub-far code sizes (1000–1200 B) could not be made to work on the
   C270 even with corrected display scaling — did not lock reliably.

## Diagnostic tooling that made this possible

- `fqt recv --dump DIR`: saves every 5th real capture with its decode count —
  offline preprocessing A/B without re-running hardware.
- `codes-per-capture histogram`: distinguishes straddle (spread of partials)
  from framing/focus (all-or-nothing) in one line.
- `fqt sweep`: sender+receiver in one process; per-round lock detection so
  autofocus/exposure settling doesn't pollute round 1's numbers (it did, in
  three sweeps, before the fix).
- Live `sharp` (Laplacian variance) on the receiver status line: <50 mush,
  100+ workable, 300+ crisp — turns "prop the camera" into a number.

## Open items

- One clean iPhone run for the confirmed headline number (`fqt sweep
  --camera 1 --kb 1000 --configs "30:3x2:close"`).
- Windows: `open_camera()` already selects MSMF→DSHOW; add `--exposure`
  (DirectShow supports manual exposure — should cut straddle ghosting
  significantly, the C270's dominant loss).
- iPhone 60 fps never materialized over Continuity (delivered 24–30);
  USB cable + bright scene untested.
