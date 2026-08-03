"""fqt command line: send / recv / bench."""

from __future__ import annotations

import argparse


def _parse_grid(s: str) -> tuple[int, int]:
    try:
        c, r = s.lower().split("x")
        cols, rows = int(c), int(r)
    except ValueError:
        raise argparse.ArgumentTypeError("grid must look like 2x2")
    if not (1 <= cols <= 4 and 1 <= rows <= 4):
        raise argparse.ArgumentTypeError("grid dimensions must be 1..4")
    return cols, rows


PROFILES = {  # block_len presets: zxing-writer v40-L / v27-L ceilings minus 24B header
    "close": 2927,
    "far": 1439,
}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="fqt", description="fountain QR transfer")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("send", help="stream a file as animated QR codes")
    s.add_argument("file")
    s.add_argument("--fps", type=float, default=30.0, help="display frames/s (<= refresh/2)")
    s.add_argument("--profile", choices=PROFILES, default="close")
    s.add_argument("--bytes", type=int, default=None, help="payload bytes per code (overrides profile)")
    s.add_argument("--grid", type=_parse_grid, default=(1, 1), help="codes per frame, e.g. 2x2")
    s.add_argument("--ecc", choices="LMQH", default="L")
    s.add_argument("--size", type=int, default=900, help="display size in px")

    r = sub.add_parser("recv", help="receive from the camera")
    r.add_argument("--camera", type=int, default=0)
    r.add_argument("--width", type=int, default=1280)
    r.add_argument("--fps", type=float, default=60.0)
    r.add_argument("--workers", type=int, default=3)
    r.add_argument("--out", default=".")
    r.add_argument("--preview", action="store_true", help="show camera preview window")
    r.add_argument("--dump", default=None, help="save every 5th capture as PNG into this dir")

    w = sub.add_parser("sweep", help="continuous test: sender+receiver in one process, config matrix")
    w.add_argument("--camera", type=int, default=1)
    w.add_argument("--width", type=int, default=1920)
    w.add_argument("--cam-fps", type=float, default=60.0)
    w.add_argument("--kb", type=int, default=500, help="payload per round")
    w.add_argument("--size", type=int, default=950)
    w.add_argument("--workers", type=int, default=3)
    w.add_argument("--timeout", type=float, default=0.0, help="per-round timeout s (0=auto)")
    w.add_argument(
        "--configs",
        default=None,
        help="comma list of fps:grid:profile, e.g. 12:2x2:close,24:2x2:close",
    )

    b = sub.add_parser("bench", help="camera-less loopback benchmark")
    b.add_argument("--kb", type=int, default=512)
    b.add_argument("--profile", choices=PROFILES, default="close")
    b.add_argument("--bytes", type=int, default=None)
    b.add_argument("--grid", type=_parse_grid, default=(1, 1))
    b.add_argument("--ecc", choices="LMQH", default="L")
    b.add_argument("--channel", choices=["none", "camera"], default="camera")
    b.add_argument("--loss", type=float, default=0.0, help="simulated frame loss 0..1")

    args = ap.parse_args(argv)

    if args.cmd == "send":
        from .send import run_sender

        run_sender(
            args.file,
            fps=args.fps,
            block_len=args.bytes or PROFILES[args.profile],
            grid=args.grid,
            ec_level=args.ecc,
            display_px=args.size,
        )
    elif args.cmd == "recv":
        from .recv import run_receiver

        run_receiver(
            camera=args.camera,
            width=args.width,
            fps=args.fps,
            workers=args.workers,
            out_dir=args.out,
            show_preview=args.preview,
            dump_dir=args.dump,
        )
    elif args.cmd == "sweep":
        from .sweep import run_sweep

        configs = None
        if args.configs:
            configs = []
            for part in args.configs.split(","):
                fps_s, grid_s, profile = part.strip().split(":")
                configs.append((float(fps_s), _parse_grid(grid_s), profile))
        run_sweep(
            camera=args.camera,
            width=args.width,
            cam_fps=args.cam_fps,
            kb=args.kb,
            display_px=args.size,
            workers=args.workers,
            configs=configs,
            timeout=args.timeout,
        )
    elif args.cmd == "bench":
        from .bench import run_bench

        run_bench(
            size_kb=args.kb,
            block_len=args.bytes or PROFILES[args.profile],
            grid=args.grid,
            ec_level=args.ecc,
            mode=args.channel,
            loss=args.loss,
        )


if __name__ == "__main__":
    main()
