# Maximizing Goodput in Screen-to-Camera Optical File Transfer

**Research question:** how to push goodput beyond a fountain-coded animated-QR system at ~128 KB/s phone-to-phone, covering academic SCC literature, rateless code choice, code-density levers, and implementation stack.

**Method note:** every claim below was adversarially debated and source-checked. Section 2 uses only claims that survived as *confirmed*. Section 3 lists *weak* claims — interesting leads that failed verification on reasoning or evidence and must not be used as a basis for design decisions without new measurement. Appendix holds *refuted* and *dropped* material.

---

## 1. Executive summary

1. **128 KB/s is not a breakthrough number; it is roughly parity with the 2020 state of the art.** libcimbar published 852 kbit/s (~106 KB/s) sustained in 2020 on a Snapdragon 625 (a 2016 SoC), and a beta mode "safely exceeding 1 Mbit/s". Any claim of a large multiple over prior art is measuring against the wrong baseline (see Appendix A).
2. **The binding constraint is frame yield, not per-frame density.** libcimbar's own numbers imply ~7,500 usable bytes/frame but only ~106 KB/s delivered — an effective yield near 47%, i.e. roughly 14 usable frames/s out of 30. Its documentation states plainly that "the camera is usually the bottleneck." Density levers (more colors, smaller tiles, denser symbologies) show sharply diminishing returns against this.
3. **Fountain coding is a solved, non-critical subsystem.** Both Wirehair and RaptorQ decode two to three orders of magnitude faster than the channel delivers. The only property that matters at 0.1–0.2 MB/s is *reception overhead* (each 1% costs ~0.5 s on a 5 MB transfer), and on that axis the mature options are indistinguishable.
4. **Academic SCC systems are not a throughput resource.** The published literature tops out an order of magnitude *below* 128 KB/s. Mine it for robustness mechanisms (frame-mixing tolerance, blur handling, control channels), not for rate.
5. **The highest-leverage unknowns are all measurement gaps, not design gaps.** No published benchmark measures per-frame decode cost on a clean, large, screen-displayed code at high fps; no published measurement isolates browser-vs-native capture on identical hardware. Both are cheap to run and both currently gate the design.

---

## 2. Conclusions grounded in confirmed claims

### 2.1 The real prior-art baseline: libcimbar at ~106 KB/s

libcimbar's `PERFORMANCE.md` reports, per mode, exact byte counts and elapsed times:

| Mode | Config | Measurement | Rate |
|---|---|---|---|
| B (recommended) | 8×8 tiles, 4-color, ecc=30/155 | 4,689,084 B in 44 s | 852 kbit/s (~106 KB/s) |
| 8C (removed in v0.6.0) | 8-color | 4,717,525 B in 40 s | 943 kbit/s (~118 KB/s) |
| 4C | 4-color | same file, 45 s | 838 kbit/s |
| S (beta) | 5×5 tiles in 6×6 grid, ecc=40/216 | — | "safely >1 Mbit/s" |

Decoded on a Qualcomm Snapdragon 625 with 4 CPU threads. Sender is either the cimbar.org WASM encoder or the `cimbar_send` CLI.

**Two caveats that must travel with these numbers.** (a) The document states explicitly that it measures "bits over the wire, e.g. data after compression is applied" — these are zstd-compressed bytes, *not* directly comparable to uncompressed-file goodput quoted elsewhere. (b) The 106 KB/s is gated on the *receiver*, which was the native Android `cfc` app, not a browser. That the encoder happened to be WASM says nothing about browser receiver performance.

Sources: <https://raw.githubusercontent.com/sz3/libcimbar/master/PERFORMANCE.md>, <https://github.com/sz3/libcimbar>

### 2.2 Per-frame budget, and the frame-yield gap that dominates it

From `DETAILS.md`: a 1024×1024 barcode with 8×8 tiles in a 9×9 grid contains 12,400 data tiles; 6-bit cimbar (4 symbol bits from 16 glyphs + 2 color bits) yields **9,300 raw bytes/image**; Reed-Solomon at ecc=30 (30 parity per 125 data, 155 total) leaves **7,500 usable bytes/frame**; the fountain header costs 6 bytes per 744 bytes of real data; Wirehair's symbol limit caps a single transfer at 33.55 MB.

The arithmetic the source does not do, and which is the most important number in this survey:

> 7,500 B/frame × 30 fps = 225 KB/s theoretical. Measured: 106 KB/s. **Effective frame yield ≈ 47%** — roughly 14 usable frames/s out of 30.

Half the channel is being thrown away at the acquisition/registration stage. That term, not per-frame density, is where the remaining headroom lives. Practical implication: **budget engineering effort on frames-per-second-actually-decoded (exposure, focus lock, blur rejection, finder-pattern robustness, capture threading) before spending it on symbology density.**

Source: <https://raw.githubusercontent.com/sz3/libcimbar/master/DETAILS.md>

### 2.3 Rateless code choice is not a throughput lever

**Wirehair** (what libcimbar ships): average overhead ≈ N + 0.02 packets; decode 342 MB/s at N=32,768 up to 1,034 MB/s at N=64 with 1,300-byte packets; explicitly *not* MDS, so occasional N+1/N+2 is expected. Its ~64k-symbol block limit is exactly what produces libcimbar's 33.55 MB file cap.

**RaptorQ** (RFC 6330, `cberner/raptorq` in Rust with PyO3 Python bindings): on a Raspberry Pi 3 B+ (Cortex-A53 1.4 GHz, 1,280-byte symbols) decode runs 67–211 Mbit/s (≈ 8.5–26 MB/s), encode 104–258 Mbit/s; on a Ryzen 9 9950X3D decode reaches 8.7 Gbit/s. RFC 6330 is systematic with K'_max = 56,403 source symbols per block.

Against a channel delivering ~0.1–0.2 MB/s, both are ~2 orders of magnitude of headroom. **Conclusion: pick on ecosystem fit and overhead, not speed.**

Three qualifications:
- Wirehair's cited MB/s are desktop x86 against a 0.1 MB/s channel — decorative, and they do not discriminate between codecs. On the metric that *does* matter (reception overhead), Wirehair's N+0.02 is not meaningfully better than RaptorQ. There is no measured reason to prefer Wirehair beyond "libcimbar shipped it."
- The published RaptorQ benchmark is a *single bulk decode of a complete symbol set*. An animated-code receiver wants incremental decode attempted repeatedly as frames arrive; that multiplies cost by the number of attempts unless the implementation is genuinely incremental. **Verify incremental-decode cost before assuming the headroom transfers.**
- At 0.1 MB/s, 1% of reception overhead ≈ 0.5 s on a 5 MB transfer. Overhead, not MB/s, is the figure of merit.

Sources: <https://github.com/catid/wirehair>, <https://github.com/cberner/raptorq>, <https://www.rfc-editor.org/rfc/rfc6330>

### 2.4 The txqr baseline, and why its conclusions do not generalize

divan/txqr (Go, LT codes via `gofountain`): record of ~13 KB in 501 ms at 12 FPS, 1,850 B/QR, ECC level L — "almost 25 kbps" (~3 KB/s). The author's sweep found FPS had negligible effect on total time, chunk sizes of 1,400–1,700 B caused frequent decode timeouts while 1,800–2,000 B performed best, and ECC level had negligible effect.

**This is accurate reportage of one sweep, and it must not be read as generalizable.** "FPS negligible" and "ECC negligible" are artifacts of a pipeline that was decoder-bound at ~3 KB/s: when the decoder cannot keep up with 12 fps, raising fps changes nothing and lowering ECC buys nothing usable. Both findings are directly contradicted by systems that are not decoder-bound, where TX fps is the primary lever and ECC ratio is a deliberately tuned parameter. The 1,400–1,700 B dead zone is a property of one Go QR encoder's version-boundary behaviour, not of the channel.

Sources: <https://divan.dev/posts/fountaincodes/>, <https://github.com/divan/txqr>

### 2.5 Decoder selection: the only defensible reading of the available benchmark

On Dynamsoft's 536-image / 1,232-code dataset (Intel i5-10400, 16 GB, 16 difficulty categories):

| Decoder | Read rate | Mean ms/image |
|---|---|---|
| Dynamsoft (vendor's own) | 83.29% | 195.01 |
| BoofCV | 60.69% | 104.95 |
| OpenCV `wechat_qrcode` | 48.89% | 757.71 |
| ZBar | 38.95% | 157.31 |
| ZXing 3.5.1 | 31.87% | 179.57 |

`wechat_qrcode` is slow because it bolts a Caffe SSD/MobileNetV2 detector and a QRSR super-resolution CNN onto zxing-cpp (confirmed from its README architecture description).

**Discounts that are mandatory when using this table:** it is vendor-run by a direct commercial competitor whose product tops it; the ZXing tested is the Java 3.5.1 line, **not zxing-cpp**, which is what every real pipeline in this space actually uses — so the benchmark does not measure the relevant decoder; and the dataset is deliberately difficult still images (read rates 31–60%), so mean ms/image is dominated by failure paths where decoders exhaust binarization/rotation retries. **None of these ms figures predict per-frame cost on a clean, large, well-lit, screen-displayed code**, which is the actual workload.

BoofCV's independent page corroborates only partially: "no library dominates all categories"; Java libraries fastest ignoring the multi-code case; ZBar 20× slower than BoofCV when many codes are present. quirc separately claims ~50 ms to extract and decode a VGA frame on a modern x86 core.

**Actionable conclusion: do not select a decoder from this table. Run a purpose-built measurement** — full-frame, screen-displayed, single large code, at target resolution and fps, on the target device — across zxing-cpp (native and WASM), quirc, rqrr, BoofCV. That measurement does not currently exist publicly and is the cheapest high-value experiment available.

Sources: <https://www.dynamsoft.com/codepool/qr-code-reading-benchmark-and-comparison.html>, <https://github.com/opencv/opencv_contrib/tree/master/modules/wechat_qrcode>, <https://boofcv.org/index.php?title=Performance%3AQrCode>

### 2.6 Academic SCC systems: mine for robustness, not rate

| System | Reported figure |
|---|---|
| Focus (MobiSys'16) | 30 KB/s throughput; 23 KB/s file-transfer goodput (50.3 KB file, Galaxy S6) |
| PixNet (as reimplemented on a phone by Focus) | peaks at 10 KB/s at 50 cm |
| RDCode | ~17 KB/s goodput |
| AIRCODE (NSDI'21) | 1,086 kbps raw throughput → 139–159 kbps goodput at ~5% BER |

All are far below 128 KB/s. Two caveats: the PixNet 10 KB/s is *Focus's own reimplementation of a rival system* (the original PixNet reports Mbit/s with DSLR optics) — structurally the same rival-measurement problem flagged for the Dynamsoft table, and it should be discounted the same way. And AIRCODE's ~7× throughput-to-goodput gap comes from embedding data *imperceptibly in ordinary video*; it is not a general goodput tax and does not apply to a dedicated tool where the whole screen is the code.

**Conclusion:** these papers are worth reading for mechanisms (frame-mixing tolerance, perspective handling, control-channel design, adaptive ECC), not for throughput targets.

Sources: <https://vs.inf.ethz.ch/publ/papers/soeroesg-mobisys2016-Focus.pdf>, <https://www.usenix.org/system/files/nsdi21-qian-kun.pdf>

### 2.7 Provenance of the 128–190 KB/s figures

The 128–190 KB/s numbers circulating in press (Tom's Hardware, dev.ua) trace to a single project's self-reported README (`decimen-optical-transfer`): 129 KB/s goodput on a 2 MB phone-to-phone image transfer, "~128 KB/s handheld, ~186 KB/s propped". Three Hacker News submissions drew 3, 1 and 1 points with zero comments — traction was on Reddit and aggregator press, not technical scrutiny.

**A correction the parent should carry:** the README attributes the 128/186 KB/s figures to *"the parent experiment's measured ceiling with this exact architecture plus denser frames, a 120 fps ProMotion sender, and stacked codes"* — a different, unpublished codebase. The config usually quoted alongside them (QR v40 = 2,953 B/frame, ECC L, 60 fps TX) is the *published* PoC's default, and 2,953 B × 60 fps = 177 KB/s, which is **below** the 186 KB/s headline. The headline is only reachable at 120 fps with stacked codes. **The headline number is not reproducible from any published artifact.**

Two symmetry notes: low HN karma is not evidence of anything (it measures submission timing); and libcimbar's `PERFORMANCE.md` is equally self-reported by its author — the replication standard must be applied to both.

The README's candid failure modes are themselves useful data: `Math.log` differing across JS engines desynchronising the soliton distribution; `requestVideoFrameCallback` zombie loops; `file://` blocking camera access.

Sources: <https://github.com/bashalarmistalt/decimen-optical-transfer>, <https://hn.algolia.com/api/v1/search?query=decimen&tags=story>

### 2.8 Synthesized design guidance

From the confirmed set only:

- **Target the frame-yield term first.** 47% yield is the single largest identified loss. Frame-disposal of blurred/in-between frames plus a rateless layer makes discarding nearly free — libcimbar's "shakycam" option does exactly this so it can "spend more processing time decoding real data."
- **Do not run half-rate FEC.** Every measured 100+ KB/s system in this survey runs a high-rate in-frame code: libcimbar ecc=30/155 (rate 0.81), mode S 40/216 (0.815), decimen QR ECC level L (~0.93). The erasure/fountain layer handles frame-level loss far more cheaply than in-frame parity handles it.
- **Choose the fountain codec on ecosystem, not speed.** RaptorQ (`cberner/raptorq`, Rust + Python bindings, RFC 6330) and Wirehair (C++, what libcimbar uses) are both far faster than needed; overhead is a wash. Verify *incremental* decode cost for your arrival pattern.
- **Compression is a real multiplier and a reporting hazard.** libcimbar's headline is post-zstd bytes. Compressing before the fountain layer is correct engineering; quoting the result against uncompressed-file goodput is not.
- **Measure your own decoder.** The published decoder benchmarks test the wrong decoder on the wrong workload.
- **Expect ~10% inter-mode deltas to be noise.** libcimbar's 4C (838) vs 8C (943) vs B (852) kbit/s were separate runs under uncontrolled conditions.

---

## 3. Unverified leads (weak claims — do not build on these)

These failed adversarial verification. Each is listed with what survives and what does not.

**3.1 8-color trades throughput for reliability.** *Contradicted by its own source.* PERFORMANCE.md lists beta mode S (4-color, 5×5 tiles, ecc=40/216) at ">1 Mbit/s", faster than 8C's 943 kbit/s — so 8C was never "the fastest ever measured," only the fastest finalized mode. Worse for the thesis: 8C ran the same file in 40 s vs 4C's 45 s, i.e. +12.5% goodput for a +16.7% raw-bit increase, and mode S roughly doubles tile count for ~+17% goodput. **What survives: raw density of *any* kind has sharply diminishing returns, because the camera is the bottleneck.** That is a more useful conclusion than the color/reliability framing. (8C *was* removed in v0.6.0 as "inconsistent, needs future research" — that fact stands.)

**3.2 Browser receivers are capped by `requestVideoFrameCallback`.** MDN's rule is real — callbacks fire at min(video frame rate, paint refresh rate), with the explicit 120 fps-video-in-60 Hz-browser → 60 Hz example, Baseline since Oct 2024, `presentedFrames` exposed for drop detection. But the "video frame rate" term is the *receiver's camera track*, not the sender's display; a 120 Hz ProMotion sender does not enter the expression. Since phone cameras deliver 30 or 60 fps, paint rate is usually not binding. rVFC is also not the only capture path (MediaStreamTrackProcessor/WebCodecs in Chromium, ImageCapture). **What survives: threading is a genuine throughput issue** — WASM threads require COOP/COEP headers, which break `file://` and simple static hosting, and WASM SIMD parity with native is not free.

**3.3 Browser/WASM runs an order of magnitude below native.** The libcimbar ladder facts are solid (v0.6.3 mode Bm ≈ 70% of mode B capacity; v0.6.4 mode Bu ≈ 43% for "max compatibility/reliability", e.g. laptop webcam reading a phone screen). The inference is not: the ~80 kbit/s iPhone web figure comes from a v0.6.2c *beta* whose own release notes list "suboptimal webapp camera settings" and intermittent freezes as known issues, and it holds nothing constant against the 106 KB/s native comparator (different device, app, likely mode, capture stack). It also conflicts with decimen's reported 129 KB/s from a browser receiver running zxing-cpp WASM in workers. **Verdict: native-vs-browser is an open question requiring a controlled measurement.** zxing-cpp ships official Python, WASM, Rust, Android, iOS, Kotlin/Native, .NET, Go, C, Qt and WinRT bindings, so the *decode core* is identical across stacks — any gap is capture path and threading.

**3.4 iOS structurally handicaps browser receivers.** WebKit bug 281848 ("Shape Detection API doesn't work on iOS", filed 2024-10-21, status NEW, unassigned) is real but largely moot: no throughput-maximizing design would use `BarcodeDetector` anyway — one code per call, no control over binarization, ROI, or worker parallelism. Both libcimbar and decimen ship custom WASM decoders on every platform regardless. The companion citation (bug 179994, getUserMedia constraints) is **outdated** — RESOLVED/CONFIGURATION CHANGED, closed with WebKit engineers stating it was addressed by iOS 13.4 in 2020. The `{exact:60}` vs `{ideal:60}` behaviour is an unverified project-README assertion worth testing directly.

**3.5 Practical capture floor ~900×900 pixels.** The mechanism worth keeping: **stalls are usually finder/corner-pattern detection failures, and any occlusion (mouse cursor, glare) over a corner costs whole frames.** The numbers are dated: the 900×900 figure is a December 2020 forum remark, superseded by current PERFORMANCE.md ("designed to decode at resolutions as low as 700×700, but performance may suffer"). The "250 KB file could not be decoded" report is a misreading — the commenter got bored and stopped.

**3.6 Shannon headroom is 30×.** Internally inconsistent *within the single cited paper*. Ashok et al. state both "6 bits/camera-pixel" with no blur (→ 31 Mbps at 1920×1080@30fps) and, separately, "room for at least 2.5× improvement in throughput when compared to capacity" versus real prototypes. The 2.5× is the operationally meaningful number; the 30× is derived from the idealized zero-blur, screen-fills-frame figure with no budget for FEC, sync, geometry or headers, and treats post-demosaic/post-ISP/post-H.264 camera pixels as independent samples, which they are not. **Being 30× below Shannon is true of essentially every communication system ever built.** Useful surviving datum: ~1 bit/camera-pixel is still achievable when the screen occupies only 15% of each image dimension (≈2.6 m distance).
Source: <https://www3.cs.stonybrook.edu/~jain/papers/shubham_pmc.pdf>

**3.7 Motion blur is the dominant killer; Wiener deconvolution recovers it.** The paper's numbers argue against "dominant": handheld is 5 vs 6 bits/px, a 17% loss. The ~2 bits/px figure is for a phone *waved horizontally at walking speed* — not a file-transfer posture — where without deblurring capacity is "almost zero." The paper never compares deblurring against block size or code density and defers deblur-algorithm modelling to future work, so "bigger lever than any symbology change" has no support. Feasibility is oversold: MATLAB Wiener filtering applied offline over 100 frames with an estimated PSF is not "cheap" at 60 fps with a rolling-shutter-induced, spatially varying PSF. **The field's shipped answer is the opposite one:** detect and *discard* blurred frames (libcimbar "shakycam"), which the fountain layer makes nearly free. Frame disposal plus rateless coding likely dominates deblurring on cost/benefit — though nobody has measured the two head to head.

**3.8 Phase-domain encoding makes fps mismatch harmless.** Focus (MobiSys'16) proves the algebra: for `c_mix = a·c_i + b·c_{i+1}`, if the sub-channel symbol is unchanged, `S_mix = (a+b)·S_i` — magnitude scales, **phase is unaffected**. Multi-rate sub-channels let one stream serve readers from ~5 to 240 FPS. But sub-channels that *did* change are destroyed for that reader, so this is graceful multi-rate degradation, not immunity. The price is disqualifying for a throughput goal: 2,267 bits (~283 B) per code with finest detail at 1/26 of code width — roughly **26× below libcimbar's 7,500 B/frame** — plus a full-frame FFT per frame at 60 fps. The proposed rescue (phase trick on a small control sub-band, dense tiles for payload) is untested speculation and competes with a ~20-byte self-describing per-frame header costing ~0.7% of a v40 frame.

**3.9 Audio side channel for metadata.** AIRCODE Table 2: audio control 1,084.3 kbps / 4.4% BER / **149.1 kbps goodput** vs in-video metadata 893.7 / 4.6% / **144.3 kbps**; adaptive ECC adds "10 kbps more goodput". That is +3.3%, and confounded — the audio row also carries 21% higher raw throughput yet converts a *smaller* fraction to goodput (13.8% vs 16.1%). The "freed screen area" argument is AIRCODE-specific (it had to duplicate metadata across an imperceptible in-video code); a dedicated system pays ~20 B/frame for a self-describing header. ggwave (MIT, 8–16 bytes/sec, ultrasonic F0=15 kHz, C/Python/JS/WASM/Java/Kotlin/Obj-C bindings) is worse than implied: seconds of latency for a handshake, unreliable across phone speaker/mic responses and in noise, and it requires microphone permission — forfeiting the camera-only property.
Sources: <https://github.com/ggerganov/ggwave>, AIRCODE PDF above.

**3.10 JAB Code / soft-decision LDPC.** The API is real: `libjabcode` (LGPL-2.1, Fraunhofer SIT) exposes `decodeLDPC(jab_float *enc, ...)` documented as "LDPC decoding to performe soft decision" alongside hard-decision `decodeLDPChd`, with wc/wr setting code rate; ISO/IEC 23634:2022; 8 colors → 3 bits/module. The engineering argument is not established: the "1–2 dB" gain figure is unsourced AWGN intuition, but screen-camera errors are dominated by blur, misregistration, glare, saturation and cross-device color mismatch — structured and spatially correlated, not per-module Gaussian. "3 bits/module ≈ 3× QR" is raw modulation *before* the LDPC rate is applied, so it is not a goodput claim. An 8-color symbology also sits against libcimbar's field experience (§3.1). No measured throughput, decode latency, or streaming benchmark for libjabcode exists, and soft LDPC is materially more expensive per frame than Reed-Solomon — which matters when frames are already being dropped for lack of decode headroom.
Sources: <https://jabcode.github.io/jabcode/ldpc_8h.html>, <https://en.wikipedia.org/wiki/JAB_Code>

**3.11 Exotic receivers / global-shutter cameras.** Selene (SenSys'24) reaches 1.61 Mbps with 1,995 parallel channels (57×35 blocks of 8×8 DMD mirrors, DLP4500) read by a DVXplorer Lite event camera at 320×240 from 1 m — 16× over PhotoLink's 100 kbps. But the transmitter is a DMD, not a phone screen, and the receiver is the lowest-resolution device in its class, so "exotic hardware buys only ~1.5×" is an overgeneralization from one prototype at minimum sensor resolution; the obvious scaling axis (sensor resolution) is untested. The OV9281 global-shutter UVC recommendation has no measured screen-camera number at all, and its caveat is disqualifying rather than a footnote: **120 fps is MJPG-only** — lossy DCT applied to exactly the high-spatial-frequency content the code lives in — while uncompressed YUY2 collapses to 10 fps at 1280×800 (30 fps at 640×400), worse than a phone. 1280×800 also caps a square code at 800×800, barely above libcimbar's 700×700 design floor. And a UVC camera needs a host computer, abandoning phone-to-phone entirely. (The ~$30 price is not on the cited page.)
Sources: <https://arxiv.org/html/2410.14228v1>, <https://sites.google.com/site/globalshutterov9281usbcamera/>

**3.12 Standard animated-QR browser benchmark (xulihang).** Measured 0.74–16.38 KB/s, dropping to ~1–3 KB/s on a 231 KB file, concluding the approach "works great for transferring small-sized files which are under 200KB", with decode time (hundreds of ms/frame against 30 fps capture) named as the bottleneck — using the commercial Dynamsoft JS SDK. The claim's inference is self-contradicting: decode time being binding **is** a decoder-performance artifact. Test devices (iPhone SE 2016, low-end Sharp AQUOS S2) and a single-threaded architecture make this a measurement of one era's mobile JS, not of the channel; worker-parallel WASM decode is exactly what later systems do. Specific per-file sub-ranges quoted elsewhere were not reproducible from the source text.
Source: <https://dev.to/xulihang/transfer-data-with-animated-qr-codes-1ffl>

---

## Appendix A — Refuted claims

**A.1 "txqr's ~25 kbit/s is *the* community baseline, so 128 KB/s is ~40× prior art."** *Refuted by the survey's own evidence.* libcimbar is an equally well-known open-source screen-to-camera file transfer with published measurements of ~106 KB/s in 2020 on 2016-era hardware. Against the actual state of the art, 128 KB/s is **~1.2×, not 40×**. Two further defects: txqr is a 2018 Go/LT prototype whose ceiling was its own decoder, so the ratio measures eight years of hardware and decoder progress rather than design merit; and HN points are not a citation metric (171 points measures one day's front-page traffic).

**A.2 "Paper archiving converged on two transferable rules: reliable cell ≈ 3× nominal resolution unit, and rate-1/2 FEC is the normal operating point."** *Refuted.* Rate-1/2 is not what anyone in this channel runs: libcimbar ecc=30/155 (rate 0.81), mode S 40/216 (0.815), decimen QR level L (~0.93) — all reaching 100+ KB/s. The reason is structural: an erasure/fountain layer plus frame disposal handles frame-level loss far more cheaply than doubling in-frame parity, so half-rate FEC is a large unforced loss. The "3× resolution unit" rule is a *printing* artifact (dot gain, ink bleed, toner scatter, scanner MTF) with no analog on an emissive display that reproduces pixels exactly; libcimbar's actual geometry is an 8×8 tile in a 9×9 grid decoding at ~0.9 camera pixels per screen pixel, i.e. ~8 camera pixels per symbol. (A ~3 camera-pixels-per-*module* heuristic is separately defensible on Nyquist grounds, but that is not what Optar's 3× means.) One hobby project is also not a field that "converged" on anything. The underlying Optar facts are accurate — 600 dpi printer with a 200 dpi practical limit (3×3 squares), Golay(24,12), ~200 kB/A4 — the *rules* are extrapolation. Source: <http://ronja.twibright.com/optar/>

## Appendix B — Dropped in verification

**B.1 "QR v40 = 2,953 B at ECC L is ~3× less per frame than libcimbar's ~7,500 usable bytes on a comparable 1024×1024 canvas, which is why libcimbar abandoned QR."** — **source-mismatch.** The QR half is grounded (qrcode.com confirms versions 1–40 with v40 = 177×177 modules; Wikipedia gives 2,953 bytes as byte-mode max at 40-L). The libcimbar half is not: `ABOUT.md` describes a sub-1080×1080 square grid chosen "semi-arbitrarily", cites txqr's 25 kb/s and a "sustained 100 kb/s" goal, but never states per-frame payload density as the reason for abandoning QR. **The 3× ratio and the causal explanation are unsupported by the cited sources and must not be used.** (The 7,500 B/frame figure itself *is* independently confirmed from DETAILS.md — see §2.2 — but the comparison and the causal story are not.)

**B.2 Unverifiable within this pass:** the AIRCODE PDF returns HTTP 403 to WebFetch (retrievable via curl); the Tom's Hardware article body did not render, so press-side attribution in §2.7 is inferred from the headline only.

---

## Appendix C — Recommended next measurements

Ranked by information gained per hour of work:

1. **Frame-yield instrumentation.** Log frames captured vs. frames decoded vs. frames contributing new fountain symbols. Resolves §2.2's 47% gap and tells you whether to work on capture, registration, or density.
2. **Purpose-built decoder benchmark** on the real workload (one large screen-displayed code, target resolution/fps, target device) across zxing-cpp native, zxing-cpp WASM, quirc, rqrr, BoofCV. Replaces §2.5's unusable vendor table.
3. **Controlled native-vs-browser capture comparison** on identical hardware, identical mode, identical decoder core. Resolves §3.2/§3.3, currently the biggest open architectural question.
4. **Blur-discard vs. deblur A/B.** Cheap to run, directly tests §3.7's contested claim.
5. **Incremental fountain-decode cost** under realistic frame-arrival patterns, not bulk decode. Confirms §2.3's headroom actually transfers.
