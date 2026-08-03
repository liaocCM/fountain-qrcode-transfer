# Decimen Optical Transfer — complete technical map

Repo reviewed: `/Users/tex/code/github/decimen-optical-transfer` · v0.2.0 · TypeScript + Vite, no
runtime framework. Two dependencies that matter: `qrcode@1.5.4` (sender) and `zxing-wasm@2.2.4`
(receiver).

This document is a reimplementation-grade map of the wire format, the fountain layer, both
pipelines, and the performance envelope, written for building a faster next-generation version in
another language.

---

## 1. Wire format

### 1.1 Frame header — 20 bytes, little-endian

Defined and documented at `shared/protocol.ts:1-15`, written by `packFrame`
(`shared/protocol.ts:281-294`) and read by `parseFrame` (`shared/protocol.ts:296-313`).

| offset | type | field | notes |
|---|---|---|---|
| 0 | u8 | magic `0xD1` | `MAGIC0`, `shared/protocol.ts:27` |
| 1 | u8 | magic `0x0C` | `MAGIC1` — together "D1 0C" ≈ "decimen" |
| 2 | u16 LE | `sessionId` | random 1..65535 per sender start |
| 4 | u32 LE | `seq` | drives the fountain PRNG |
| 8 | u16 LE | `k` | source block count (hard ceiling 65535) |
| 10 | u16 LE | `blockLen` | payload bytes per frame |
| 12 | u32 LE | `totalLen` | length of the protected container |
| 16 | u32 LE | `payloadFnv` | FNV-1a over the whole container |
| 20 | … | block | exactly `blockLen` bytes |

There is **no version field**. The magic bytes are the only versioning, so any change to the
fountain derivation is a silent break — which is why the golden vectors exist (§6).

The golden hex vector, pinned at `tests/protocol.test.ts:83-114`:

```
d1 0c ef be 04 03 02 01 11 01 06 00 ba dc fe 00 ef cd ab 89 | 01 02 03 04 05 06
```

for `{sessionId: 0xbeef, seq: 0x01020304, k: 0x0111, blockLen: 6, totalLen: 0x00fedcba,
payloadFnv: 0x89abcdef}`.

Validation in `parseFrame` rejects a frame when: length ≤ 20, magic mismatch, `k == 0`,
`blockLen == 0`, `totalLen == 0`, or `bytes.length !== 20 + blockLen`
(`shared/protocol.ts:299-311`). The returned block is a **subarray view**, not a copy.

### 1.2 Session identity and reset

`sessionId` is drawn at `send/main.ts:244`:

```ts
const sessionId = (Math.floor(Math.random() * 0xffff) + 1) & 0xffff;   // 1..65535, never 0
```

The receiver does **not** key on session id alone. `streamIdentity` (`shared/protocol.ts:328-330`)
concatenates every field that must not drift:

```ts
return `${h.sessionId}:${h.k}:${h.blockLen}:${h.totalLen}:${h.payloadFnv}`;
```

Any disagreement constructs a fresh `LTDecoder` (`receive/main.ts:245-251`). The reasoning in the
doc comment (`shared/protocol.ts:315-327`) is worth carrying forward: 16-bit session ids collide
across restarts often enough to matter, and a mismatched frame fed into a live decoder corrupts it
silently, surfacing only as a checksum failure after the entire transfer has run. Including
`payloadFnv` also gives a useful property — a sender restarted on the *same* file resumes into the
*same* decoder.

### 1.3 File container ("DCF2") — 49-byte header

Built by `packFile` (`shared/protocol.ts:164-209`), parsed by `unpackFile`
(`shared/protocol.ts:211-265`). `FILE_HEADER_LEN = 49` (`shared/protocol.ts:26`).

| offset | type | field |
|---|---|---|
| 0 | 4 bytes | magic `44 43 46 32` = `"DCF2"` |
| 4 | u8 | compression: 0 = none, 1 = gzip (>1 rejected) |
| 5 | u16 LE | `nameLen` (UTF-8 bytes) |
| 7 | u16 LE | `typeLen` (UTF-8 bytes) |
| 9 | u32 LE | `originalSize` — uncompressed file length |
| 13 | u32 LE | `transmittedSize` — post-gzip length |
| 17 | 32 bytes | SHA-256 of the **original, uncompressed** bytes |
| 49 | `nameLen` | filename UTF-8 |
| 49+nameLen | `typeLen` | media type UTF-8 |
| 49+nameLen+typeLen | `transmittedSize` | payload |

Note the header is deliberately unaligned (u32 at offset 9 and 13); `DataView` handles it, but a
reimplementation must not assume alignment.

**Two independent integrity layers.** `payloadFnv` (frame header) covers the whole container
including its own SHA-256 field, and is checked first at `receive/main.ts:258`. SHA-256 covers the
original file bytes and is checked after decompression by `verifyFile`
(`shared/protocol.ts:267-270`).

**Compression policy.** gzip is attempted only when
`bytes.length >= 768 && !isPrecompressedType(type)` (`shared/protocol.ts:181`), and kept only when
it wins by a real margin:

```ts
const useGzip = compressed !== undefined && compressed.length + 64 < bytes.length;   // :186
```

`isPrecompressedType` (`shared/protocol.ts:152-162`) is a deliberate allowlist rather than a
heuristic: all `video/*`, all `image/*` except bmp/svg/tiff/ico, all `audio/*` except
wav/aiff/basic/l16, the OOXML and OpenDocument prefixes, anything ending `+zip`, plus a 14-entry
exact-match set. The stated reason (`shared/protocol.ts:138-151`) is memory, not CPU: on a 64 MB
pick the trial gzip buffer is one of five simultaneous full-size copies the sender holds.

**Hardening worth replicating.** Two receiver-side defenses because everything arrives over an
untrusted optical channel:

- `gunzipAsync` (`shared/protocol.ts:72-96`) counts bytes off the decompression stream and aborts
  past `maxBytes`, because the gzip trailer's declared size is attacker-controlled. It also
  cross-checks the trailer's ISIZE against the container's `originalSize` before inflating
  (`shared/protocol.ts:238-247`).
- `safeFileName` (`shared/protocol.ts:107-113`) runs on **both** ends — basename split on
  `[\\/]`, strip `\u0000-\u001f` and `\u007f`, trim, and fall back to `transfer.bin` for `""`,
  `"."`, `".."`.

Limits: `MAX_FILE_BYTES = 64 * 1024 * 1024` (`shared/protocol.ts:16`),
`MAX_SNIPPET_BYTES = 4 MB` (`shared/snippet.ts:14`).

**Text snippets are not a separate path.** A snippet is the same container with media type
`application/vnd.decimen.snippet` and filename `snippet.txt` (`shared/snippet.ts:9-10`); the
receiver dispatches on the media type at `receive/main.ts:324`. Decoding uses
`TextDecoder("utf-8", {fatal: true})`.

---

## 2. Fountain coding

### 2.1 The PRNG — splitmix32 (`shared/protocol.ts:342-353`)

```ts
export function splitmix32(seed: number): () => number {
  let s = seed | 0;
  return () => {
    s = (s + 0x9e3779b9) | 0;
    let t = s ^ (s >>> 16);
    t = Math.imul(t, 0x21f0aaad);
    t ^= t >>> 15;
    t = Math.imul(t, 0x735a2d97);
    t ^= t >>> 15;
    return t >>> 0;
  };
}
```

Reimplementation notes: `s` is a **signed** 32-bit wrapping accumulator; `>>>` is a logical shift
on the unsigned reinterpretation; `Math.imul` is 32-bit wrapping signed multiply keeping the low 32
bits. In Rust/Go/Zig this is straightforward `u32` arithmetic with wrapping ops — the signedness
never escapes because the output is `t >>> 0`. The comment "deterministic across JS engines
(integer ops only)" is the whole point: no floating-point anywhere in the PRNG.

### 2.2 FNV-1a (`shared/protocol.ts:332-339`)

Standard 32-bit FNV-1a, offset basis `0x811c9dc5`, prime `0x01000193`, via `Math.imul`, returned as
unsigned. Used for both the wire checksum and the test fingerprints.

### 2.3 Seed derivation — `frameSeed` (`shared/fountain.ts:80-84`)

```ts
function frameSeed(sessionId: number, seq: number): number {
  let h = (Math.imul(sessionId + 1, 0x9e3779b1) ^ (seq + 0x85ebca6b)) | 0;
  h = Math.imul(h ^ (h >>> 13), 0xc2b2ae35);
  return (h ^ (h >>> 16)) | 0;
}
```

Note `sessionId + 1` — session id 0 would otherwise degenerate. `seq + 0x85ebca6b` can exceed 2^32
as a JS double, but the `^` operator coerces to int32 first, so the result is well-defined; a
reimplementation should use wrapping `u32` addition.

### 2.4 The degree distribution

Constants: `SOLITON_C = 0.1`, `SOLITON_DELTA = 0.5` (`shared/fountain.ts:53-54`).

```
R     = max(1, 0.1 · dlog(k/0.5) · sqrt(k))
spike = min(k, ceil(k/R))
rho(d)= d==1 ? 1/k : 1/(d(d-1))
tau(d)= d<spike     ? R/(d·k)
      : d==spike    ? R·max(0, dlog(R/0.5))/k
      : 0
```

accumulated into a `Float64Array` CDF and normalized, with `cdf[k-1]` forced to exactly 1
(`shared/fountain.ts:58-78`).

The **critical** detail — and the one that will bite hardest in a port — is `dlog`
(`shared/fountain.ts:31-51`). `Math.log` is implementation-approximated by the ECMAScript spec, so
V8 (a laptop sender) and JavaScriptCore (an iPhone receiver) can differ by 1 ulp, which shifts a
CDF boundary and flips a sampled degree, silently desyncing the two ends. `dlog` is a hand-rolled
range reduction (halve while ≥1.5, double while <0.75) plus an 11-term atanh series
(`n = 1,3,…,21`) using only exactly-specified IEEE-754 operations. `tests/fountain.test.ts:52-71`
explicitly asserts `dlog !== Math.log` on at least some inputs — it will fail if anyone
"simplifies" it.

**For a non-JS port this is the single biggest compatibility hazard.** If you keep the wire format,
you must reproduce `dlog` bit-exactly (any language with IEEE-754 doubles and the same operation
order will), *not* call the platform `log`. If you're free to break the format, replace the whole
float CDF with an integer/fixed-point degree table computed once — same distribution, zero
cross-platform risk.

### 2.5 seq → block subset (`shared/fountain.ts:94-128`)

```ts
const rnd = splitmix32(frameSeed(sessionId, seq));
const u = rnd() * 2 ** -32;            // uniform [0,1)
// binary search: first index with cdf[i] >= u
const d = Math.min(k, lo + 1);
if (d > k >> 3) {
  // partial Fisher–Yates over a fresh identity Uint32Array(k)
  for (let i = 0; i < d; i++) { const j = i + (rnd() % (k - i)); swap; out[i] = scratch[i]; }
  return out;
}
const set = new Set<number>();
while (set.size < d) set.add(rnd() % k);   // rejection sampling
return [...set];
```

Three things a port must preserve exactly: the `d > k >> 3` branch threshold (the two paths consume
the PRNG differently and produce different subsets), the `rnd() % k` modulo (biased, but it *is*
the wire format), and the insertion-order iteration of the `Set` in the small-degree path — the
returned array's order is pinned by the golden vectors even though XOR is commutative.

Cost note: the Fisher–Yates path allocates a `Uint32Array(k)` per frame, but the branch is rare in
practice. For k=716 the spike degree is 37 while the threshold is 89, and the tail mass above 89 is
roughly `Σ 1/(d(d-1)) ≈ 1.1%`.

### 2.6 Encoder

`LTEncoder` (`shared/fountain.ts:134-165`) copies the payload into a single
`Uint32Array(k * ceil(blockLen/4))` up front — one full-size copy of the container, word-aligned
per block, tail zero-padded. `encode(seq)` XORs the selected blocks word-wise and returns
`new Uint8Array(out.buffer, 0, blockLen)`.

Padding is safe to truncate: only the final source block has nonzero-length padding and it is
always zeros, so the XOR of padding bytes is always zero. `tests/fountain.test.ts:189-196` pins that
every frame is exactly `blockLen` — a short tail frame would change the QR version mid-stream and
break everything after it, since the sender locks the version off frame 0.

### 2.7 Receiver peeling (`shared/fountain.ts:172-267`)

Incremental, online peeling — not a batch Gaussian elimination:

1. `seen: Set<number>` on `seq` dedupes (`framesDup++`). This set grows unbounded over a transfer.
2. Reduce the incoming frame against already-solved blocks, XOR-ing them out and shrinking the
   index set.
3. Degree 0 after reduction → fully redundant, discard. Degree 1 → `resolve()`. Degree ≥ 2 → park
   it in `byBlock: Map<blockIndex, Set<PendingFrame>>`, indexed under *every* unsolved block it
   still depends on.
4. `resolve(b0, w0)` (`shared/fountain.ts:236-256`) runs an explicit LIFO stack cascade: mark
   solved, pull the waiting set for that block, XOR the solved value into each, and push any that
   drop to degree 1.

`assemble()` (`:258-267`) concatenates the solved blocks, clipping the last to `totalLen`.

The comment at `shared/fountain.ts:232-235` is the one to carry into the new UI: **the cascade
back-loads.** Blocks-solved hockey-sticks at the end while frame arrival is linear, so a progress
bar driven by blocks looks stalled and then teleports.

### 2.8 Observed overhead

Measured empirically and recorded at `shared/progress.ts:1-20` — 200 trials per k, 128-byte blocks:

| k | 1 | 5 | 25 | 50 | 100 | 200 | 400 | 800 | 1600 | 3200 |
|---|---|---|---|---|---|---|---|---|---|---|
| p50 | 1.00 | 1.40 | 1.44 | 1.38 | 1.31 | 1.26 | 1.22 | 1.18 | 1.15 | 1.12 |
| p90 | 1.00 | 2.20 | 1.92 | 1.76 | 1.52 | 1.38 | 1.30 | 1.24 | 1.19 | 1.15 |

The model used for ETA (`shared/progress.ts:21-24`):

```ts
return Math.min(1.6, Math.max(1.15, 1.1 + 2.45 / Math.sqrt(k)));
```

The often-quoted 1.15 is asymptotic only. A 300 KB file at 2953 bytes/frame is k≈100, where true
overhead is ~1.31 — a flat constant was wrong by 15–40% for most real transfers. **This is a real
design input for the next version:** raising bytes/frame lowers k, which *raises* fractional
overhead. There is a genuine optimum, and at small k the fountain is expensive enough that a
systematic-code variant (send the k source blocks first, then fountain-code the repair stream)
would recover most of it.

---

## 3. Sender pipeline

### 3.1 Defaults and tunables (`shared/send-settings.ts`)

```ts
export const DEFAULT_TX_FPS = 60;                                       // :11
export const DEFAULT_FRAME_BYTES = 2953;                                // :12
export const NO_SIGNAL_HINT_FRAME_BYTES = 1465;                         // :8
export const NO_SIGNAL_HINT_TX_FPS = 24;                                // :9
export const TX_FPS_OPTIONS = [10, 15, 20, 24, 30, 60];                 // :15
export const FRAME_BYTES_OPTIONS = [500, 1000, 1465, 1850, 2331, 2953]; // :16-23
```

These lists are the single source of truth: `vite.config.ts:66-78` renders the `<select>` options
from them into `%TX_FPS_OPTIONS%` / `%FRAME_BYTES_OPTIONS%` tokens in `send/index.html:65,69`, and
the receiver's no-signal hint quotes the same constants (`receive/main.ts:397-398`) so advice can
never name a setting the sender doesn't offer.

Other sender controls: ECC `L|M|Q|H` (default L, `send/index.html:73-75`), display size range
300–1200 px step 50, default 900 (`send/index.html:79`).

**Verified what each frame-size option actually costs in QR terms** (by running `qrcode@1.5.4`
directly against each value):

| frameBytes | blockLen | version @ECC L | modules | that version's capacity | wasted |
|---|---|---|---|---|---|
| 500 | 480 | v15 | 77 | 520 | 20 |
| 1000 | 980 | v22 | 105 | 1003 | 3 |
| **1465** | 1445 | **v27** | 125 | 1465 | **0** |
| 1850 | 1830 | v32 | 145 | 1952 | **102** |
| 2331 | 2311 | v36 | 161 | 2431 | **100** |
| **2953** | 2933 | **v40** | 177 | 2953 | **0** |

The 1850 and 2331 options leave ~5% and ~4% of goodput on the table at ECC L for identical module
counts — 1952 and 2431 would be free wins. (2331 is exactly the v40 **ECC M** ceiling and 1663
would be v40 Q, so those numbers were likely chosen for a different ECC level than the default.)
Also note 2953 is **only** achievable at ECC L: at M/Q/H the encoder throws, which `pump()`'s
try/catch turns into a user-visible error (`send/main.ts:342-352`).

Measured max byte-mode payload at v40: L 2953, M 2331, Q 1663, H 1273.

### 3.2 QR generation

Library `qrcode@1.5.4` (node-qrcode), called at `send/main.ts:309-313`:

```ts
const qr = QRCode.create([{ data: bytes, mode: "byte" } as unknown as QRCode.QRCodeSegment], {
  errorCorrectionLevel: ecc,
  version,
  maskPattern: 4,
});
```

Three deliberate choices, all documented at `send/main.ts:3-13`:

- **Mask pattern pinned to 4.** Any declared mask is valid to a decoder, so this skips the spec's
  8-way mask penalty evaluation — the comment claims **~4× faster generation**. This is the single
  biggest sender-side win and it carries over to any language.
- **Version locked after the first frame** (`send/main.ts:314-328`). Since every frame is exactly
  `blockLen` bytes, all frames land on the same version; passing it explicitly skips capacity
  search per frame.
- **ECC L by default.** In-frame ECC and the fountain solve different problems (corruption vs
  erasure); a frame is decoded whole or discarded, so spending modules on ECC is worse than
  spending them on payload.

The library only ever produces `qr.modules` (a bit matrix); no rendering is used from it.

### 3.3 Rasterization (`shared/qr-raster.ts`)

Pure and Node-testable — no `ImageData` dependency. Writes one **u32 per pixel** into a
`Uint32Array`, `WHITE = 0xffffffff`, `BLACK = 0xff000000` (alpha in the high byte, little-endian).
One module = one pixel, quiet zone `MARGIN = 4` modules (`send/main.ts:41`). The sender then wraps
it with zero copies:

```ts
return new ImageData(new Uint8ClampedArray(raster.pixels.buffer), raster.size, raster.size);  // send/main.ts:330
```

At v40 the raster is 177 + 8 = **185×185 px = 137 KB per frame**.

### 3.4 Display sizing (`send/main.ts:280-304`, `shared/display.ts`)

```ts
const viewportBudget  = 0.9 * Math.min(viewportWidth, viewportHeight);
const containerBudget = Math.max(1, containerWidth - horizontalChrome);
return Math.max(1, Math.min(viewportBudget, containerBudget, requestedSize));
```

then in `sizeCanvas`:

```ts
scale = Math.max(1, Math.floor((cssBudget * dpr) / total));   // integer scale only — no resampling
staging.width = staging.height = total;                        // 185×185 backing store
canvas.width  = canvas.height  = total * scale;
canvas.style.width = canvas.style.height = `${(total * scale) / dpr}px`;
```

Integer scale plus `image-rendering: pixelated` (`shared/style.css:126`) plus
`imageSmoothingEnabled = false` (`send/main.ts:371`) guarantees hard module edges. At 900 px
requested on a dpr-2 display: `floor(1800/185) = 9`, so a 1665×1665 canvas. Re-run on `resize` via
the `resizeDisplay` hook (`send/main.ts:212, 318`).

### 3.5 Render loop (`send/main.ts:333-376`)

A 3-frame lookahead queue (`LOOKAHEAD = 3`, `send/main.ts:42`) refilled one frame per tick:

```ts
const interval = 1000 / txFps;
let nextAt = performance.now();
const tick = (now: number) => {
  if (gen !== generation || generatorFailed) return;
  requestAnimationFrame(tick);
  if (now < nextAt) return;
  const img = queue.shift();
  pump(1);
  if (!img) { nextAt = now + interval; return; }
  staging.getContext("2d")!.putImageData(img, 0, 0);
  const ctx = canvas.getContext("2d")!;
  ctx.imageSmoothingEnabled = false;
  ctx.drawImage(staging, 0, 0, canvas.width, canvas.height);
  nextAt += interval;
  if (now - nextAt > 3 * interval) nextAt = now + interval;   // fell behind — don't burst-catch-up
};
```

Two details worth stealing: `nextAt += interval` (absolute schedule, no drift accumulation) with a
3-interval catch-up clamp, and the lookahead amortization. The comment at `send/main.ts:333-341`
records that a self-scheduling `setTimeout(pump, 0)` cost **~250 wake-ups/second doing nothing**
once the queue was full; capping at one frame per tick keeps the amortization without the spin.

Every frame is `putImageData` into a small staging canvas then `drawImage` upscale onto the visible
one — the GPU does the scaling, the CPU only touches 185×185.

### 3.6 Restart / generation guard

`generation` (`send/main.ts:75`) is bumped on mode switch, new selection, and every settings change;
stale rAF loops and stale async pack operations check it and die
(`send/main.ts:143, 229, 238, 344, 360`). All selection paths funnel through one `startSelection()`
(`send/main.ts:133-155`) so the guard can't be subtly wrong in one copy.

Capacity pre-flight before streaming (`send/main.ts:248-261`) uses `shared/frame-capacity.ts`:
`MAX_SOURCE_BLOCKS = 0xffff`, so at 500 bytes/frame the real ceiling is ~30 MB, not 64 MB. The
error names a value **from the dropdown** via `smallestSufficientFrameSize()`, not the bare
arithmetic minimum.

---

## 4. Receiver pipeline

### 4.1 Camera constraints (`receive/main.ts:113-129`)

```ts
const base: MediaTrackConstraints = {
  facingMode: "environment",
  width:  { ideal: captureWidth },
  height: { ideal: Math.round((captureWidth * 3) / 4) },
};
try {
  stream = await getUserMedia({ audio: false, video: { ...base, frameRate: { exact: captureFps } } });
} catch {
  stream = await getUserMedia({ audio: false, video: { ...base, frameRate: { ideal: captureFps } } });
}
```

**The `exact`-then-`ideal` ladder is the iOS workaround**: `frameRate: {ideal: 60}` is silently
satisfied with 30, and only `exact` gets 60 (it works at 1280-wide). The negotiated result is always
read back with `getSettings()` and shown to the user, including a "(asked N)" note when they differ
(`receive/main.ts:169-179`).

Defaults from `receive/index.html:66-74`: capture width **1280** (options 960/1280/1920), capture
fps **60** (30/60), decode workers **2** (1/2/3). Live reconfiguration via `track.applyConstraints`
with a graceful failure path for devices that refuse it mid-stream (`receive/main.ts:181-201`).

### 4.2 Capture loop

```ts
function scheduleFrame(gen: number) {
  if (done || gen !== captureGen) return;
  const v = video as VideoRVFC;
  const next = () => { if (done || gen !== captureGen) return; captureFrame(); scheduleFrame(gen); };
  if (v.requestVideoFrameCallback) v.requestVideoFrameCallback(next);
  else requestAnimationFrame(next);
}
```

(`receive/main.ts:205-215`) — self-rechaining, with `rAF` fallback. The `captureGen` counter exists
because **rVFC chains outlive their MediaStream and resume on the next one**; without it every
stop/start leaks a zombie capture loop (`receive/main.ts:6-7`).

```ts
function captureFrame() {
  const vw = video.videoWidth, vh = video.videoHeight;
  if (!vw || !vh) return;
  captureTimes.push(performance.now());
  if (pool.busyCount === pool.size) return;              // all busy — drop it, no harm done
  if (grab.width !== vw || grab.height !== vh) { grab.width = vw; grab.height = vh; }
  const ctx = grab.getContext("2d", { willReadFrequently: true })!;
  ctx.drawImage(video, 0, 0);
  const img = ctx.getImageData(0, 0, vw, vh);
  pool.submit({ id: frameId++, buf: img.data.buffer, w: vw, h: vh }, [img.data.buffer]);
}
```

(`receive/main.ts:220-234`). The buffer is **transferred**, not copied. Note that `captureTimes`
counts every rVFC callback while `decodeTimes` counts successful decodes, so the two HUD metrics are
capture rate and decode rate respectively.

### 4.3 Worker pool (`shared/worker-pool.ts`)

Fixed-slot, one frame in flight per worker, 70 lines. The design constraint that justifies it
existing as its own module: each worker's `onmessage` closes over its slot index, so resizing must
leave surviving indices alone — hence shrinking always pops from the end
(`shared/worker-pool.ts:41-47`). `submit()` returns false when all slots are busy and the caller
**drops the frame rather than queueing** it, because a stale frame is worth less than the next one
(`:60-69`). `resize(0)` at completion reclaims each worker's ~940 KB WASM instance
(`receive/main.ts:310`).

`id === -1` is the warm-up ping and must not free a slot (`shared/worker-pool.ts:51`) — pinned by a
test.

### 4.4 The decode worker (`receive/worker.ts`, 36 lines)

```ts
prepareZXingModule({ overrides: { locateFile: (path, prefix) =>
  path.endsWith(".wasm") ? wasmUrl : prefix + path } });

ctx.onmessage = async (e) => {
  const { id, buf, w, h } = e.data;
  const img = new ImageData(new Uint8ClampedArray(buf), w, h);
  const results = await readBarcodes(img, { formats: ["QRCode"], maxNumberOfSymbols: 1 });
  const r = results.find((x) => x.isValid && x.bytes.length > 0);
  ctx.postMessage({ id, bytes: r ? r.bytes : null });
};

// warm the WASM so the first real frame doesn't pay instantiation
void readBarcodes(new ImageData(8, 8), { formats: ["QRCode"] })
  .catch(() => undefined).then(() => ctx.postMessage({ id: -1, bytes: null }));
```

zxing-cpp via WASM is used because **Safari has never shipped `BarcodeDetector`** (WebKit bug
281848), so it's the only portable option.

Decoded text → bytes is a non-issue here and worth noting for a port: they use `result.bytes` (the
raw byte-mode payload) directly, never `result.text`. Going through the string would corrupt binary
data. `isValid && bytes.length > 0` is the acceptance test.

**Significant finding: only two of zxing's reader options are overridden.** Verified defaults in
`zxing-wasm@2.2.4` (`dist/es/share.js:123-133`) — everything else is left on:

```
tryHarder: true, tryRotate: true, tryInvert: true, tryDownscale: true,
binarizer: "LocalAverage", isPure: false, downscaleFactor: 3, downscaleThreshold: 500
```

For a known-format, upright, non-inverted, screen-rendered animated QR at 60 fps this is a lot of
wasted work per frame: `tryRotate` costs up to 4× the detection passes, `tryInvert` doubles
binarization attempts, `tryDownscale` adds a pyramid level for any image ≥500 px. Turning off
rotate/invert/harder is probably the **cheapest available throughput win in the entire receiver**
and costs nothing to try.

### 4.5 Frame ingestion and stream lock (`receive/main.ts:236-261`)

```ts
const parsed = parseFrame(bytes);
if (!parsed || done) return;
const identity = streamIdentity(header);
if (!decoder || streamKey !== identity) {
  decoder = new LTDecoder(header.k, header.blockLen, header.sessionId, header.totalLen);
  streamKey = identity; startTs = performance.now(); ...
}
decoder.addFrame(header.seq, block);
if (decoder.isComplete) {
  const payload = decoder.assemble()!;
  const ok = fnv1a(payload) === header.payloadFnv;
  void finish(payload, ok, seconds);
}
```

No handshake anywhere — the receiver locks onto whatever stream it sees and resets on any identity
change. A QR code from somewhere else fails the magic check and is ignored silently.

### 4.6 Progress and ETA (`shared/progress.ts:33-81`)

Frames drive a continuously moving baseline, with actual solved blocks able to jump it forward:

- `uniqueFrames < k` → `0.86 × (frames/k)`
- `k ≤ frames ≤ expected` → `0.86 + 0.10 × (frames-k)/redundancy`
- beyond expected → `0.96 + 0.03 × (1 - e^(-extra))`, asymptotic
- `decodedFraction = 0.99 × min(1, solved/k)`; final = `min(0.99, max(frameFraction, decodedFraction))`

100% is reserved for verified completion. ETA is suppressed until
`uniqueFrames >= 3 && elapsed >= 1`, and once a stream runs long the target **steps up one
redundancy block at a time** rather than going silent (`shared/progress.ts:71-79`) — the honest
behaviour exactly when someone is wondering whether it stalled.

Goodput (`receive/main.ts:291-299`) discounts fountain overhead k-dependently:

```ts
return (decoder.framesNew * decoder.blockLen) / expectedFountainOverhead(decoder.k) / 1024 / Math.max(0.1, elapsed);
```

The comment records that a flat 1.18 **over-reported small transfers by up to 2×**.

The no-signal hint (`shared/no-signal.ts`, `receive/main.ts:386-418`) fires after 10 s with no
parsed frame, and its advice points at the *sender* — drop to 1465 bytes/frame, then to 24 fps —
because the defaults are tuned for close-range phone-to-phone and are exactly the combination that
fails on an ordinary monitor at arm's length.

---

## 5. Performance-relevant details

### 5.1 Where the numbers come from

Default configuration: 60 fps × 2933 payload bytes = **175,980 B/s = 171.9 KiB/s** of raw block
throughput. For a 2 MB file at blockLen 2933, k = 716, modelled overhead 1.19, giving a **~144 KiB/s
theoretical goodput ceiling**. The README reports 129.2 KB/s observed on that exact payload
(`README.md:22-25`), implying roughly 90% of displayed frames actually decode — i.e. the system is
running close to its own ceiling, and further gains must come from raising the ceiling, not from
decode reliability.

### 5.2 Why 60 fps

`README.md:202`: tuned for a **120 Hz ProMotion sender**, where each frame owns exactly two refresh
cycles. On a 60 Hz screen each frame owns a single refresh and camera captures straddle transitions
— which is why the advice is to drop to 24–30. `send/main.ts:10-11` states the underlying rule:
**displays need each frame shown for ≥2 refresh cycles or captures catch the transition.** That
constraint (tx_fps ≤ refresh_rate / 2) is the hard ceiling on the whole approach for a given display
and is the first thing to design around in a faster version.

### 5.3 Why 2953 bytes

It is the QR v40 ECC-L byte-mode ceiling — the absolute maximum a single standard QR code can carry.
`send/main.ts:5-7`: "1465 bytes ≈ V27 is a safe middle ground for arbitrary monitors; 2953 (V40) is
the ceiling and works phone-to-phone at close range." Density is 23,624 payload bits over 31,329
modules ≈ **0.754 useful bits per module** at ECC L, before the fountain's ~19% overhead — so the
end-to-end optical efficiency of the current design is roughly **0.63 payload bits per displayed
module**.

### 5.4 The tricks worth carrying forward

- **Pinned mask pattern** — skips 8-way penalty evaluation, ~4× faster QR generation
  (`send/main.ts:8-9`).
- **Version locked after frame 0** — no capacity search per frame.
- **Lookahead queue capped at one refill per tick** — killed ~250 idle wake-ups/sec.
- **Absolute frame scheduling with a 3-interval catch-up clamp** — no drift, no bursts.
- **Generation counters on both sides** — the sender's kills stale rAF loops and stale async packs;
  the receiver's kills zombie rVFC chains that survive a stopped MediaStream.
- **iOS `frameRate: {exact}` before `{ideal}`**, always read back with `getSettings()`.
- **Frame dropping over queueing** when all workers are busy — the fountain absorbs it and a stale
  frame is worth less than the next one.
- **WASM warm-up ping** so the first real frame doesn't pay instantiation.
- **`dlog`** — the deterministic-log lesson generalizes: any float math on the wire-format path is a
  cross-platform hazard.

### 5.5 Bottlenecks to expect in a rewrite

1. **RGBA capture bandwidth.** At 1280×960 the receiver moves 4.9 MB per captured frame through
   `getImageData`; at 60 fps that's ~295 MB/s of CPU memcpy before decoding starts, and
   `willReadFrequently: true` forces a software-backed canvas to make the readback cheap. zxing-cpp
   natively accepts an 8-bit luminance `ImageView`, so **passing grayscale instead of RGBA is a 4×
   bandwidth cut** available for free in a native port (and largely available in the browser via
   WebGL/WebGPU or `VideoFrame.copyTo` with a luminance layout).
2. **zxing reader options left at their defaults** (§4.4) — rotate/invert/downscale/tryHarder all
   on.
3. **Two workers at 60 fps** means each worker has a 33 ms budget per frame; three workers exist as
   an option but two is default.
4. **The `seen: Set<number>` in `LTDecoder`** grows unbounded for the life of a transfer.
5. **Sender memory** — for a 64 MB file the sender simultaneously holds file bytes, the gzip trial
   buffer, the container, and the encoder's `Uint32Array` block copy.
6. **Two frame-size options waste ~100 bytes each** at their own QR version (§3.1).

### 5.6 The parent experiment

**There is no code from it in this repo.** Grepping for multi-code grids, colour channels, stacking,
and 120 fps turns up only prose in `README.md:12-19` and `README.md:209-211`:

> reached **128 KB/s phone-to-phone** with denser frames, multi-code grids, and an error-corrected
> color channel
>
> The parent experiment's measured ceiling with this exact architecture plus denser frames, a
> 120 fps ProMotion sender, and stacked codes: ~128 KB/s handheld, ~186 KB/s propped.

`README.md:226-228` also points at `sz3/libcimbar` as the project that "goes past QR entirely with a
custom high-density color code purpose-built for this channel" — the obvious reference design if
you're willing to abandon QR compatibility.

---

## 6. What the golden vectors pin

`tests/fountain.test.ts:1-10` states the doctrine explicitly: **fountain.ts IS the wire format**,
standalone HTML senders and receivers are attached to releases and reused months later, so a failure
here is a breaking change requiring a header version bump, not a re-recorded constant.

| test | file:line | what it pins |
|---|---|---|
| `dlog` spot values | `fountain.test.ts:19-36` | 11 exact `dlog` outputs to full double precision |
| `dlog` exhaustive sweep | `fountain.test.ts:38-50` | FNV-1a `0x27b0f3cc` over **every** input the CDF can reach: `dlog(2k)` for k=1..65535, and `dlog(i/64)` for i=64..262143. Catches shortening the series from 21 to 19 terms, which changes only 0.2% of outputs |
| `dlog` ≠ `Math.log` | `fountain.test.ts:52-71` | within 2 ulp of native, but **must differ somewhere** — fails if anyone substitutes `Math.log` |
| soliton CDF well-formed | `fountain.test.ts:75-85` | monotonic, terminates at exactly 1, nonzero degree-1 mass (or peeling never starts) |
| soliton CDF fingerprints | `fountain.test.ts:87-115` | FNV-1a of the raw `Float64Array` bytes for k ∈ {1, 2, 17, 179, 716, 5000, 22000}. Sampling **cannot** catch a 1e-16 boundary shift; only hashing the distribution can |
| `frameIndices` subsets | `fountain.test.ts:117-137` | literal index arrays for k ∈ {1,2,17,179,716} × seq ∈ {0,1,2,41,1000} at sessionId 4242 — **including order** |
| index sanity | `fountain.test.ts:139-151` | 3000 seqs × 5 k values: distinct, in range, degree 1..k |
| session separation | `fountain.test.ts:153-160` | same seq on different sessions ⇒ different subset |
| **end-to-end stream** | `fountain.test.ts:171-187` | FNV-1a of 64 concatenated encoded frames for four (k, blockLen, sessionId) combos: `k=1/64/1 → 0xf6a115c5`, `k=23/64/7 → 0x2aafe48d`, `k=179/2933/4242 → 0x83bbd1d7`, `k=716/1445/65535 → 0x15e10360`. Covers dlog, solitonCdf, frameSeed, splitmix32, frameIndices, block padding and XOR order in one hash |
| uniform frame length | `fountain.test.ts:189-196` | every frame exactly `blockLen` — a short tail frame would change the QR version mid-stream |
| round trips | `fountain.test.ts:225-301` | exact recovery at 7 B / 2933 B / 50 KB / 512 KB / 2 MB; 30% frame loss costs time not correctness (unique-frame overhead stays < 1.6); arbitrary arrival order; duplicate frames counted but harmless; k=1 completes on frame 0 |
| **frame header bytes** | `protocol.test.ts:83-114` | the exact 26-byte hex string (§1.1) plus round-trip parse |
| frame rejection | `protocol.test.ts:217-234` | wrong magic, header-with-no-block, truncated block, `k=0` |
| `streamIdentity` | `protocol.test.ts:186-215` | every field except `seq` forces a decoder reset; separator cannot confuse `{k:1,blockLen:23}` with `{k:12,blockLen:3}` |
| container round-trip | `protocol.test.ts:15-54` | UTF-8 filenames, SHA-256 rejection of altered bytes, gzip round-trip, gzip length-bound enforcement |
| filename sanitization | `protocol.test.ts:60-81` | `../../etc/passwd` → `passwd`, Windows paths, `..`/`.`/`/`/whitespace/control chars → `transfer.bin` |
| compression policy | `protocol.test.ts:116-184` | 19 types that must skip gzip, 13 that must still try, case-insensitivity, parameter stripping |
| raster format | `qr-raster.test.ts` | margin placement, row-major truthy=dark, and that the u32s really are RGBA bytes an `ImageData` expects |
| capacity math | `frame-capacity.test.ts` | tested against the **real** `FRAME_BYTES_OPTIONS`, and that a suggested option is always offered, always sufficient, and always larger than the one that just failed |
| pool slot identity | `worker-pool.test.ts:114-134` | slots stay bound to their own worker across shrink-and-regrow |
| progress model | `progress.test.ts:15-41` | the overhead model sits **at or above** every recorded p50 and within 15% of it, is monotonic in k, clamps to 1.6 / 1.15 |

Test runner: `node --import tsx --test tests/*.test.ts` — no framework, no browser, no DOM.
Everything wire-format-critical was deliberately kept DOM-free so it can be golden-tested in Node.

---

## 7. Directory structure and line counts

```
decimen-optical-transfer/            4,922 lines total (excl. lockfile, assets)
│
├── shared/                          ← everything DOM-free and testable
│   ├── style.css                1118   both pages
│   ├── protocol.ts               353   frame header, DCF2 container, gzip, fnv1a, splitmix32
│   ├── fountain.ts               268   dlog, solitonCdf, frameIndices, LTEncoder, LTDecoder
│   ├── progress.ts                92   overhead model, progress/ETA, formatDuration
│   ├── worker-pool.ts             70   fixed-slot decode pool
│   ├── no-signal.ts               64   hint timing policy (pure, no DOM)
│   ├── frame-capacity.ts          49   u16 block-count ceiling arithmetic
│   ├── snippet.ts                 41   text-as-container
│   ├── qr-raster.ts               36   module matrix → RGBA u32 buffer
│   ├── status-line.ts             24   shared status/error affordance
│   ├── send-settings.ts           23   canonical tunables (feeds HTML + receiver hints)
│   ├── format.ts                   7
│   ├── display.ts                 11   fitQrDisplaySize
│   └── wake-lock.ts               11
│
├── send/
│   ├── main.ts                   379   pack → LTEncoder → QR → rAF render loop
│   └── index.html                 99   %TX_FPS_OPTIONS% / %FRAME_BYTES_OPTIONS% tokens
│
├── receive/
│   ├── main.ts                   471   camera, rVFC capture, pool, LTDecoder, progress, finish
│   ├── worker.ts                  36   zxing-wasm decode + warm-up ping
│   ├── worker-factory.ts          10   served build: module worker by URL
│   ├── worker-factory.inline.ts    8   standalone: base64 blob worker (file:// needs it)
│   ├── wasm-url.ts                 5   served: ?url asset
│   ├── wasm-url.inline.ts          6   standalone: data: URI
│   └── index.html                 92   settings + live diagnostics HUD
│
├── tests/                          ~1,022 across 10 files
│   fountain 309 · protocol 234 · worker-pool 140 · no-signal 85 · progress 81
│   frame-capacity 80 · snippet 55 · qr-raster 46 · format 21 · display 12
│
├── build/                          ~292  one Vite plugin per file
│   root-pwa-head 108 · standalone-csp 63 · rewrite-standalone-links 57
│   use-inline-variants 26 · inline-zxing-wasm 24 · html-tokens 22 · emit-as 16
│
├── vite.config.ts                 168   4 modes: site / demo / standalone-send / standalone-receive
├── index.html                      91   landing page
└── README.md                      235
```

**Build modes** (`vite.config.ts:83-113`): the standalone builds swap `worker-factory` and
`wasm-url` for inline variants **at resolve time**, not with a runtime branch — both inline forms
have module-scope side effects, so Rollup keeps their ~45 KB of base64 even when the branch is
provably dead (`build/use-inline-variants.ts:4-9`). `?inline` doesn't work on `.wasm` at all because
Vite claims the extension, so the base64 is produced behind a virtual module instead
(`build/inline-zxing-wasm.ts:7-11`). CI asserts the served `receive-*.js` chunk stays under 20,000
bytes to catch the inlined worker or wasm leaking into the site build
(`.github/workflows/ci.yml:30-43`).

---

## 8. Reimplementation checklist

If you keep the wire format, these must be bit-exact or the two ends desync silently and the
transfer simply never completes:

1. `dlog` — the 21-term atanh series with that exact range reduction, **not** the platform `log`.
2. `splitmix32` and `frameSeed` — 32-bit wrapping arithmetic, `Math.imul` semantics, `>>> 0` on
   output.
3. `solitonCdf` — `C=0.1`, `DELTA=0.5`, accumulate-then-normalize in `f64`, force `cdf[k-1] = 1`.
4. `frameIndices` — the `d > k >> 3` branch threshold, `rnd() % k` modulo bias included, and
   **insertion-order** output from the small-degree path.
5. The 20-byte LE frame header and the 49-byte DCF2 container exactly as tabulated.

Run the golden vectors from `tests/fountain.test.ts` and `tests/protocol.test.ts` against the new
implementation before anything else — the four end-to-end stream fingerprints at
`tests/fountain.test.ts:174-179` alone will catch essentially any mistake in items 1–4.

If you're **not** keeping the format, the three highest-leverage changes are: replace the float CDF
with an integer degree table (kills the whole class of cross-platform hazard), pass grayscale rather
than RGBA into the decoder (4× capture bandwidth), and turn off zxing's
`tryRotate`/`tryInvert`/`tryDownscale`/`tryHarder` (they're all still on today). Beyond that, the
ceiling is set by `tx_fps ≤ refresh_rate / 2` and ~0.75 payload bits per QR module — which is where
the parent experiment's colour channel and stacked codes came from.
