"""QR create/read via zxing-cpp, plus multi-code grid composition.

Reader options are tuned for this channel: the code is upright, dark-on-light,
screen-rendered, and fills a known region — so try_rotate / try_invert /
try_downscale are OFF (the parent project left them on; see docs/design.md §2).
"""

from __future__ import annotations

import numpy as np
import zxingcpp

QR_CAPACITY_L = 2953  # v40 byte-mode ceiling at ECC L
QUIET_MODULES = 4


def make_qr(payload: bytes, ec_level: str = "L") -> np.ndarray:
    """payload → grayscale module raster (1 px per module, with quiet zone)."""
    bc = zxingcpp.create_barcode(payload, zxingcpp.BarcodeFormat.QRCode, ec_level=ec_level)
    img = zxingcpp.write_barcode_to_image(bc, scale=1, add_quiet_zones=True)
    return np.array(img, copy=True)


def compose_grid(cells: list[np.ndarray], cols: int, rows: int, gap_modules: int = 2) -> np.ndarray:
    """Tile QR rasters into one image (row-major). All cells same size."""
    if not cells:
        raise ValueError("no cells")
    h, w = cells[0].shape
    for c in cells:
        if c.shape != (h, w):
            raise ValueError("grid cells must be uniform size")
    out = np.full(
        (rows * h + (rows - 1) * gap_modules, cols * w + (cols - 1) * gap_modules),
        255,
        dtype=np.uint8,
    )
    for i, c in enumerate(cells):
        r, col = divmod(i, cols)
        y = r * (h + gap_modules)
        x = col * (w + gap_modules)
        out[y : y + h, x : x + w] = c
    return out


def read_frames(gray: np.ndarray, max_codes: int = 16) -> list[bytes]:
    """All QR payloads found in a grayscale camera frame."""
    results = zxingcpp.read_barcodes(
        gray,
        formats=zxingcpp.BarcodeFormat.QRCode,
        try_rotate=False,
        try_invert=False,
        try_downscale=False,
    )
    out = []
    for r in results[:max_codes]:
        if r.valid and r.bytes:
            out.append(r.bytes)
    return out
