# txqr (divan/txqr) — technical review

Cloned to `/Users/tex/code/github/txqr`. Go, MIT, ~2,774 lines of Go across library, three commands,
and a GopherJS test harness. **Last commit 2019-01-10; effectively unmaintained.**

Companion to [decimen-review.md](./decimen-review.md). This is the 2018 project decimen's README
cites as prior art. It is the same idea — animated QR plus fountain codes — reached independently
and five years earlier, and it is roughly **6× slower**. The interesting part is *why*, because two
of the three reasons are implementation artifacts rather than anything intrinsic to the channel.

Two blog posts document the design and the measurements:
- <https://divan.dev/posts/animatedqr/> — the original repetition-code version
- <https://divan.dev/posts/fountaincodes/> — the LT-code rewrite and results

---

## 1. Headline comparison

| | txqr (2018) | decimen (2024) |
|---|---|---|
| Peak measured goodput | **~20 KiB/s** (10 KiB in 501 ms) | **~129 KB/s** (2 MB image) |
| Best config | 12 fps, 1850 bytes/frame, ECC L | 60 fps, 2953 bytes/frame, ECC L |
| Payload encoding | base64 text (**0.75 efficiency**) | raw bytes, QR byte mode (1.0) |
| Frame header | ASCII `blockCode/chunkLen/total\|` | 20-byte binary, little-endian |
| Fountain block set | finite, `k × 2.0`, looped | unbounded stream |
| Degree distribution | **ideal** soliton | **robust** soliton (C=0.1, delta=0.5) |
| Decoder algorithm | **sparse Gaussian elimination** | belief-propagation peeling |
| Measured overhead @ k=100 | **1.09** | 1.31 |
| Integrity check | none | FNV-1a per stream + SHA-256 per file |
| Metadata (filename/type) | none | DCF2 container |
| Session identity | none | 16-bit id + 5-field stream identity |
| Renderer | pre-baked animated GIF | live canvas, integer-scaled |

Decomposing the ~6× gap: **5× from frame rate** (12 vs 60), **2.1× from payload per frame**
(1850 base64 chars = 1388 real bytes vs 2933 raw bytes), partially offset by **0.92× because
txqr's fountain layer is genuinely more efficient** than decimen's. Net predicted ~9.7×; observed
~6.3×, the difference being that txqr's 501 ms record is a best case measured first-frame-to-last
while decimen's 129 KB/s is a sustained whole-file figure.

**The fountain layer is the one place txqr is ahead, and it is ahead by a lot.** Everything else is
where the time goes.

---

## 2. Architecture

Four files hold the entire protocol — `doc.go`, `encode.go`, `fountain.go`, `decode.go`, about 290
lines total. The rest is commands and the test harness.

```
txqr/
├── doc.go                45   protocol description
├── encode.go             61   Encoder
├── fountain.go           30   ideal soliton CDF + id helper
├── decode.go            155   Decoder
├── txqr_test.go         250   round-trip + erasure tests + benchmarks
├── qr/qr.go              68   encode via skip2/go-qrcode, decode via gozxing
├── mobile/decode.go     122   gomobile binding for the iOS reader
└── cmd/
    ├── txqr-ascii        71   print animated QR to the terminal
    ├── txqr-gif          83   render an animated GIF
    └── txqr-tester     ~1500  GopherJS/vecty parameter-sweep harness
```

Dependencies that matter: `github.com/google/gofountain` (the LT codec),
`github.com/skip2/go-qrcode` (encode), `github.com/makiuchi-d/gozxing` (decode).

---

## 3. Wire format

### 3.1 The frame is ASCII text

`encode.go:51-53`:

```go
func (e *Encoder) frame(blockCode int64, total int, data []byte) string {
	return fmt.Sprintf("%d/%d/%d|%s", blockCode, e.chunkLen, total, string(data))
}
```

So a frame is `blockCode/chunkLen/totalLen|<payload>` — decimal ASCII, variable-length header
(~10–18 chars), single `|` delimiter. Parsing is `strings.IndexByte(chunk, '|')` then
`fmt.Sscanf(header, "%d/%d/%d", ...)` (`decode.go:44-62`). The payload may itself contain `|`
safely, since only the first one is used as the split point.

Compare decimen's fixed 20-byte binary header. txqr's costs a few more bytes and a `Sscanf` per
frame, but the real cost is the next point.

### 3.2 The channel is text-only, so binary pays a 33% tax

`doc.go:23-24` states the constraint plainly:

> All data should be within alphanumeric space.
> No error correction is implemented, as QR code layer already has one.

Every binary consumer therefore base64s first. `cmd/txqr-gif/main.go:49` and
`cmd/txqr-tester/app/gif.go:15` both do:

```go
str := base64.StdEncoding.EncodeToString(data)
```

**This is a straight 33% inflation of everything transferred**, and it is the single largest
avoidable loss in the project. Decimen sends raw bytes through QR byte mode and pays nothing.

Worth knowing for any text-constrained design: base64 is the *worst* of the safe options here,
because its mixed case forces QR **byte mode** at 8 bits/char to carry 6 bits of data (75%
efficient). Uppercase base32 stays inside QR's **alphanumeric mode** at 5.5 bits/char to carry
5 bits (**91% efficient**). Raw byte mode is of course 100%. I confirmed the mode selection
empirically — an all-uppercase payload of the same length encodes to a QR version several steps
lower than the equivalent base64.

### 3.3 No session, no integrity, no metadata

There is no session id, no checksum, no filename, no media type, and no version field. The decoder
takes `total` from whichever frame arrives first and never re-validates it. Consequences:

- Pointing a live decoder at a second, different stream silently mixes blocks from both into one
  matrix. Only a manual `Reset()` (`decode.go:136-143`) separates transfers.
- A successful decode is never verified. gofountain's `determined()` only reports that the matrix
  has full rank; nothing checks the reconstructed bytes.
- The receiver has no idea what it received — the iOS reader app treats everything as a string.

Decimen's `streamIdentity()`, `payloadFnv` and per-file SHA-256 all exist to close exactly these
gaps, and its doc comments read like they were written after being bitten by them.

Duplicate suppression is a `map[string]struct{}` keyed on the header text (`decode.go:147-155`),
with the comment "continuous QR reading often sends the same chunk in a row" — the same observation
decimen encodes as `framesDup`.

---

## 4. Fountain coding — the part txqr gets right

### 4.1 Finite block set, looped

`encode.go:26-44`:

```go
numChunks := numberOfChunks(len(str), e.chunkLen)
codec := fountain.NewLubyCodec(numChunks, rand.New(fountain.NewMersenneTwister(200)), solitonDistribution(numChunks))
idsToEncode := ids(int(float64(numChunks) * e.redundancyFactor))
lubyBlocks := fountain.EncodeLTBlocks(msg, idsToEncode, codec)
```

`Encode()` returns a **finite slice** of `k × redundancyFactor` frames (default 2.0,
`encode.go:20`), which callers then loop. This is not a fountain stream; it is a fixed set replayed.
The author is explicit about why in the blog post: the tester pre-renders an animated GIF to
guarantee frame timing in a browser, and `Encode()`'s API returns a concrete slice, so he chose a
redundancy factor over breaking both. He calls it "sub-optimal in general case, but for my task and
controlled environment it was good enough," and notes that the correct usage is to call
`EncodeLTBlocks` in a loop with advancing id ranges.

It works because replaying lets the receiver eventually catch blocks it missed, but the marginal
value of each extra loop falls off, and a channel worse than the assumed ~20% loss degrades badly.
Decimen's unbounded `seq` has no such regime.

### 4.2 Determinism without floating-point hazards

Both ends construct the codec identically, seeded by a constant:

```go
fountain.NewLubyCodec(n, rand.New(fountain.NewMersenneTwister(200)), solitonDistribution(n))
```

Two observations:

1. **The seed 200 is decorative.** `lubyCodec.PickIndices` (gofountain `luby.go:139-143`) begins
   with `c.random.Seed(codeBlockIndex)`, so the constructor seed is overwritten before it is ever
   used. Any value gives identical output. (It also means `PickIndices` mutates shared state and
   the codec is not safe for concurrent use.)
2. **txqr has no equivalent of decimen's `dlog` problem.** The ideal soliton CDF
   (`fountain.go:14-21`) is built from `1/float64(n)` and `1/(float64(i)*float64(i-1))` — IEEE-754
   division only, which is exactly specified, so every platform agrees bit-for-bit. Combined with
   an integer PRNG (MT19937), the whole derivation is portable by construction. This is direct
   evidence for the recommendation in the decimen review: an exact-arithmetic degree distribution
   removes an entire class of cross-platform desync risk. Note that gofountain's *robust* soliton
   (`util.go:54-76`) does call `math.Log` and would reintroduce it.

### 4.3 Gaussian elimination, not peeling — and it matters

This is the most important technical difference between the two projects. gofountain's decoder is
**not** the belief-propagation peeling decoder decimen uses. `sparseMatrix.addEquation`
(`block.go:177-196`) maintains a triangular sparse matrix online, following Bioglio, Grangetto and
Gaeta, and `determined()` reports success as soon as every row is populated — that is, as soon as
**k linearly independent equations** have arrived, regardless of whether a degree-1 cascade is
available. `reduce()` then completes Gaussian elimination.

Peeling needs a lucky degree structure; Gaussian elimination needs only rank. So the overhead is
close to the information-theoretic floor. I measured it directly, replicating txqr's exact codec
configuration (ideal soliton, MT19937, 60 trials per k, feeding blocks until `determined()`):

| k | 5 | 25 | 50 | 100 | 200 | 400 | 800 | 1600 |
|---|---|---|---|---|---|---|---|---|
| **txqr p50 overhead** | 1.20 | 1.12 | 1.10 | **1.09** | 1.07 | 1.09 | 1.07 | 1.04 |
| decimen p50 overhead | 1.40 | 1.44 | 1.38 | **1.31** | 1.26 | 1.22 | 1.18 | 1.15 |
| txqr p90 overhead | 1.80 | 1.48 | 1.32 | 1.44 | 1.33 | 1.32 | 1.32 | 1.21 |
| decimen p90 overhead | 2.20 | 1.92 | 1.76 | 1.52 | 1.38 | 1.30 | 1.24 | 1.19 |

**txqr needs 10–25% fewer frames than decimen at the median, and the advantage is largest at small
k — which is where real transfers live.** A 300 KB file at 2953 bytes/frame is only k≈100.

The tails invert at large k: decimen's robust soliton is designed to bound failure probability, so
its p90 is tighter from k≈400 up, while the ideal soliton's variance stays high. The obvious
synthesis for a next-generation design is **robust soliton for the tail plus Gaussian elimination
for the median** — neither project runs that combination.

The cost is asymptotic complexity. Peeling is near-linear; `addEquation` is O(k·d) per equation
with an O(k²) `reduce()` at the end, and `reduce()` (`block.go:214-228`) is written as a naive
double loop. Fine at k in the hundreds, a real consideration at k=65535.

### 4.4 A latent correctness note

`generateLubyTransformBlock` (`luby.go:156-166`) silently skips indices past the end of the source
array (`if i < len(source)`). `sampleUniform` (`util.go:128-149`) also returns *all* indices when
`num >= max` without consuming randomness. Both are defensive rather than wrong, but they mean a
mismatched k between encoder and decoder degrades quietly instead of failing loudly — and since
txqr has no stream identity to detect a mismatch, there is nothing upstream to catch it either.

---

## 5. QR layer

`qr/qr.go` is 68 lines. Encoding is `qrcode.New(data, level).Image(size)`; decoding is gozxing with
one hint:

```go
hints[gozxing.DecodeHintType_PURE_BARCODE] = true   // qr/qr.go:46
```

`PURE_BARCODE` tells zxing the image is a clean, aligned, generated barcode and skips the detector's
search. That is right for the tester's synthetic pipeline and wrong for a camera frame — but note
that txqr's *real* receiver is the separate iOS app (`divan/txqr-reader`), which uses the platform
scanner, so this path is only used by the harness. It is still an instructive contrast with
decimen, which leaves *all* of zxing's expensive options (`tryHarder`, `tryRotate`, `tryInvert`,
`tryDownscale`) enabled on the hot path.

### 5.1 The "dead zone" at 1400–1700 bytes is a renderer bug

The fountain-codes post reports a genuinely puzzling result:

> there were a lot of decoding timeouts with chunk sizes between 1400 and 1700, but 1800-2000 bytes
> actually showed one of the best results so far

That is not a property of the optical channel. It is integer truncation in go-qrcode's scaler.
`qrcode.go:286-289`:

```go
pixelsPerModule := size / realSize
offset := (size - realSize*pixelsPerModule) / 2
```

The tester always requests a fixed 500 px image (`app.go:125`, `AnimatedGif(a.testData, 500, setup)`),
so as the chunk size pushes the QR version up, `pixelsPerModule` drops in integer steps and the
drawn symbol abruptly shrinks inside its 500 px box. I reproduced it with base64 payloads at ECC L,
matching the tester exactly:

| chunk | version | modules | px/module | drawn px | fill |
|---|---|---|---|---|---|
| 1200 | v25 | 117 | 4 | 500 | **100%** |
| 1300 | v26 | 121 | 3 | 387 | **77%** |
| 1400 | v27 | 125 | 3 | 399 | 80% |
| 1500 | v28 | 129 | 3 | 411 | 82% |
| 1600 | v29 | 133 | 3 | 423 | 85% |
| 1700 | v30 | 137 | 3 | 435 | 87% |
| 1800 | v31 | 141 | 3 | 447 | 89% |
| 1850 | v32 | 145 | 3 | 459 | 92% |
| 2000 | v33 | 149 | 3 | 471 | 94% |

Between 1200 and 1300 bytes the code loses a quarter of its module size *and* 23% of its linear
extent in one step. The reported dead zone sits exactly in the trough right after that cliff, and
the reported recovery at 1800–2000 is simply the symbol growing back toward the edge of the box at
the same 3 px/module. The record-setting 1850-byte configuration was drawing at 92% fill; 2100
bytes would have drawn at 97%.

**So one of txqr's headline empirical findings is an artifact of its renderer, not a channel
effect.** Decimen sidesteps this entirely by deriving the integer scale from the actual module
count and sizing the canvas to `total × scale` rather than forcing a fixed pixel box
(`send/main.ts:297-303`), with `image-rendering: pixelated` and smoothing disabled. Any rewrite
should treat "never let the renderer choose a non-integer or shrinking module size" as a hard rule,
and should sweep chunk sizes *by QR version*, not by round decimal numbers.

---

## 6. Measurements and the test harness

`cmd/txqr-tester` is the most reusable idea in the repo: a GopherJS/vecty browser app that sweeps
encoder parameters automatically against a real phone on a tripod, driven over a WebSocket, and
exports results to CSV.

Default sweep (`app/session.go:124-137`): FPS 2→15, chunk size 50→1000 step 50, all four ECC levels
= 1,120 runs. Payload is 10 KiB of random bytes (`app/app.go:141-148`), base64'd to ~13.3 KB.

Published results:

- **Record: 13.3 KB of base64 (10 KiB real) in 501 ms** at 12 fps, 1850 bytes/frame, ECC Low.
- Error-correction level had a "negligible effect" — consistent with decimen's reasoning that
  in-frame ECC and the fountain layer solve different problems, so paying for ECC is wasted.
- FPS effect was also "negligible" across 2–15, which is the clearest sign that the system was
  **not** frame-rate-bound at those rates. Decimen's whole design lives at 60 fps, where the
  binding constraint (each frame must own two display refreshes) finally bites.
- Fountain codes hugely reduced *variance* versus the earlier repetition code, which is the
  qualitative claim both projects agree on.

The 15 fps ceiling was a GIF/browser limitation, not a channel one: `fpsToGifDelay` returns
`100/fps` in centiseconds (`cmd/txqr-gif/main.go:76-83`), so the delay is integer-quantized and
above ~12 fps the requested and actual rates diverge (100/12 = 8 cs = 12.5 fps, 100/15 = 6 cs =
16.7 fps). Pre-rendering to GIF also forces the finite block set of §4.1. **Both of txqr's biggest
architectural compromises trace back to the choice to pre-bake an animated GIF.**

---

## 7. State of the code

I ran the test suite (Go 1.25.2). Results:

```
ok      github.com/divan/txqr          6.245s
ok      github.com/divan/txqr/qr       0.713s
FAIL    github.com/divan/txqr/mobile   0.396s
FAIL    github.com/divan/txqr/cmd/txqr-tester/app  [build failed]
```

- **`mobile/` tests fail.** All three (`TestDecode`, `TestInvalidDecode`, `TestProgress`) error with
  `invalid header: unexpected EOF (0/101)`. They were written for the *old* two-field header
  `offset/total|data` and never updated: `mobile/decode_test.go` was last touched 2018-11-25, four
  days before "Switch to fountain codes" (2018-11-29) changed the header to three fields. This is
  the gomobile package actually shipped to the iOS reader app.
- **`mobile/` progress and speed are permanently zero.** `Decoder.Read()` and `Decoder.Length()`
  return literal `0` with `// TODO: remove` (`decode.go:113-123`), but the mobile wrapper computes
  from them (`mobile/decode.go:99-102`):

  ```go
  d.speed = d.Read() * int(time.Second) / int(time.Since(d.start))
  d.progress = 100 * d.Read() / d.Total()
  ```

  Both are always 0. The switch to fountain codes removed the notion of contiguous bytes-received
  without updating the consumers — which is precisely the problem decimen's `shared/progress.ts`
  exists to solve properly, and its comment about peeling back-loading the solve cascade explains
  why "bytes received" stops being meaningful under a fountain code at all.
- **The tester app no longer builds** under modern vet (printf-directive errors, a wrong-type
  `Fatalf` arg). Trivial to fix, but nobody has.
- The core library round-trips and erasure tests pass, though note they exercise encoder → decoder
  in memory and **never go through the QR layer**, so the text-only constraint of §3.2 is entirely
  untested — `txqr_test.go:243-250` feeds raw random bytes straight through as a Go string, which
  would not survive a real QR round trip.

---

## 8. What to take, and what to avoid

**Take:**

1. **Gaussian elimination over peeling.** The measured 10–25% overhead advantage at realistic k is
   free goodput, and it is the one dimension where the older project beats the newer one.
2. **Exact-arithmetic degree distributions.** The ideal soliton built from plain IEEE divisions is
   portable by construction. Pair it with an integer PRNG and the cross-platform desync class
   disappears without needing decimen's hand-rolled `dlog`.
3. **The automated parameter-sweep harness.** Sweeping fps × chunk size × ECC against a real phone
   on a tripod, with CSV export, is the right way to find an operating point. Worth rebuilding —
   but sweeping *QR versions* rather than round byte counts.
4. **The framing of the problem** in `doc.go:8-12` — sender and receiver capabilities are unknown
   and asymmetric, so the protocol must adapt — remains the correct design premise.

**Avoid:**

1. **Any text encoding of the payload.** base64 costs 33% for nothing. Raw byte mode is free.
2. **Pre-rendering the frame sequence.** It forced both the finite block set and the fps ceiling.
   Generate frames live with a small lookahead queue, as decimen does.
3. **Fixed-pixel QR rendering.** Integer module scaling against a fixed box produced a fake
   empirical result that shaped the project's chosen operating point.
4. **Shipping without integrity checks or stream identity.** Both are cheap; both are load-bearing
   the moment more than one transfer happens in a session.
5. **Deriving progress from "bytes received."** Under a fountain code the quantity does not exist;
   the mobile wrapper's dead progress bar is the direct consequence.
