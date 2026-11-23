# -*- coding: utf-8 -*-
"""Regenerate the visual assets used by README.md.

Only the project's core dependencies are needed: Pillow, numpy, segno.
Run from anywhere:  python assets/build_assets.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import segno
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets"
OUT.mkdir(exist_ok=True)

FONT_DIR = Path("C:/Windows/Fonts")
_CANDIDATES = {
    "regular": ["segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"],
    "bold": ["segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"],
    "mono": ["consola.ttf", "consolab.ttf", "DejaVuSansMono.ttf"],
}


def font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    names = _CANDIDATES[kind]
    paths = [FONT_DIR / n for n in names] + [Path(n) for n in names]
    for p in paths:
        if p.is_file():
            try:
                return ImageFont.truetype(str(p), size)
            except OSError:
                continue
    return ImageFont.load_default()


def spaced(draw: ImageDraw.ImageDraw, xy, text: str, fnt, fill, gap: int = 2):
    """Draw text with letter spacing (Pillow has no native tracking)."""
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=fnt, fill=fill)
        x += int(draw.textlength(ch, font=fnt)) + gap
    return x


def gradient(w: int, h: int, c0, c1, glow=None) -> Image.Image:
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    t = (xx / w * 0.55 + yy / h * 0.45)
    arr = np.zeros((h, w, 3), np.float32)
    for k in range(3):
        arr[..., k] = c0[k] + (c1[k] - c0[k]) * t
    if glow:
        for (gx, gy, radius, color, strength) in glow:
            d = np.sqrt(((xx - gx) / radius) ** 2 + ((yy - gy) / radius) ** 2)
            a = np.clip(1.0 - d, 0, 1) ** 2 * strength
            for k in range(3):
                arr[..., k] = arr[..., k] + (color[k] - arr[..., k]) * a
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB")


def qr_matrix(text: str) -> np.ndarray:
    """V3 / L / mask 6 matrix.

    segno encodes long URLs as one byte segment, which overflows V3-L.
    Fall back to the project's own byte+numeric multi-segment encoder so
    the demo reflects what qr_repair actually reconstructs.
    """
    try:
        q = segno.make(text, version=3, error="l", mask=6, boost_error=False)
        return np.array([[1 if v else 0 for v in row] for row in q.matrix],
                        np.uint8)
    except Exception:
        sys.path.insert(0, str(ROOT / "src"))
        from qrmcp.tools.qrspec import encode_payload
        matrix, _ = encode_payload(text, 3, "L", 6)
        return matrix.astype(np.uint8)


def qr_image(matrix: np.ndarray, module: int = 12, quiet: int = 4,
             dark=(15, 23, 42), light=(255, 255, 255)) -> Image.Image:
    n = matrix.shape[0]
    size = (n + 2 * quiet) * module
    img = Image.new("RGB", (size, size), light)
    d = ImageDraw.Draw(img)
    for r in range(n):
        for c in range(n):
            if matrix[r, c]:
                x, y = (c + quiet) * module, (r + quiet) * module
                d.rectangle([x, y, x + module - 1, y + module - 1], fill=dark)
    return img


# --------------------------------------------------------------------------
# 1. wordmark, light + dark variants (2x for retina)
# --------------------------------------------------------------------------
def build_logo(dark_mode: bool) -> Image.Image:
    S = 2
    W, H = 640 * S, 140 * S
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    board = (20, 27, 48, 255) if dark_mode else (238, 243, 255, 255)
    ink = (240, 244, 255, 255) if dark_mode else (17, 24, 39, 255)
    sub = (145, 158, 194, 255) if dark_mode else (100, 116, 139, 255)
    accent = (124, 140, 255, 255) if dark_mode else (79, 70, 229, 255)
    cyan = (56, 189, 248, 255) if dark_mode else (14, 165, 233, 255)

    # rounded tile that holds the QR-inspired mark
    tile = [10 * S, 10 * S, 130 * S, 130 * S]
    d.rounded_rectangle(tile, radius=28 * S, fill=board,
                        outline=(*accent[:3], 55), width=2 * S)

    def finder(x, y):
        s = 30 * S
        d.rounded_rectangle([x, y, x + s, y + s], radius=7 * S,
                            outline=accent, width=6 * S)
        q = 12 * S
        cx, cy = x + s // 2, y + s // 2
        d.rounded_rectangle([cx - q // 2, cy - q // 2,
                             cx + q // 2, cy + q // 2],
                            radius=2 * S, fill=accent)

    finder(28 * S, 28 * S)
    finder(82 * S, 28 * S)
    finder(28 * S, 82 * S)

    # bottom-right: a "rebuilt" module, split by a repair seam
    bx, by, bs = 82 * S, 82 * S, 30 * S
    d.rounded_rectangle([bx, by, bx + bs, by + bs], radius=7 * S,
                        fill=(*cyan[:3], 255))
    d.line([bx + 4 * S, by + bs - 3 * S, bx + bs - 4 * S, by + 3 * S],
           fill=(*board[:3], 255), width=4 * S)
    d.ellipse([bx + 8 * S, by + 8 * S, bx + 13 * S, by + 13 * S],
              fill=(*board[:3], 255))

    # wordmark
    title = font("bold", 52 * S)
    d.text((156 * S, 30 * S), "qrcode-mcp", font=title, fill=ink)
    tag = font("mono", 15 * S)
    spaced(d, (158 * S, 96 * S), "DECODE  /  REPAIR  /  GENERATE  /  OCR",
           tag, sub, gap=1 * S)
    tiny = font("regular", 13 * S)
    d.text((158 * S, 118 * S), "MCP server  ·  9 tools  ·  3 transports",
           font=tiny, fill=sub)

    return img


# --------------------------------------------------------------------------
# 2. damaged / repaired demo pair (synthetic, no real device identifiers)
# --------------------------------------------------------------------------
def build_demo():
    payload = "https://example.com/qrmcp?sn=12345678901234567890&eC=99"
    m = qr_matrix(payload)
    clean = qr_image(m, module=12, quiet=4)

    # simulate a low-resolution photo of a screen: 3 px/module then upscale
    small = clean.resize(((29 + 8) * 3, (29 + 8) * 3), Image.Resampling.LANCZOS)
    shot = small.resize(clean.size, Image.Resampling.BICUBIC)
    h, w = shot.size
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    # one diagonal glare band sweeping across the middle, like the real sample
    dist = (xx * 0.68 + yy * 0.73 - w * 0.62) / (w * 0.13)
    glare = np.exp(-dist ** 2) * 0.78
    rng = np.random.default_rng(7)
    arr = np.asarray(shot).astype(np.float32)
    arr = arr + glare[..., None] * (255.0 - arr)
    arr += rng.normal(0, 2.2, arr.shape).astype(np.float32)
    damaged = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB")
    damaged = damaged.filter(ImageFilter.GaussianBlur(0.55))

    clean.save(OUT / "demo-after.png", optimize=True)
    # JPEG suits a simulated photo and keeps the README payload small
    damaged.save(OUT / "demo-before.jpg", quality=88, optimize=True,
                 subsampling=0)


# --------------------------------------------------------------------------
# 3. GitHub social preview (1280 x 640)
# --------------------------------------------------------------------------
def build_social():
    W, H = 1280, 640
    img = gradient(
        W, H, (9, 13, 27), (17, 25, 48),
        glow=[(1040, 130, 620, (46, 62, 130), 0.85),
              (150, 600, 560, (14, 96, 120), 0.55)],
    ).convert("RGBA")

    grid = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grid)
    for x in range(0, W, 44):
        gd.line([(x, 0), (x, H)], fill=(255, 255, 255, 7), width=1)
    for y in range(0, H, 44):
        gd.line([(0, y), (W, y)], fill=(255, 255, 255, 7), width=1)
    img = Image.alpha_composite(img, grid)

    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    # glass card, restrained
    d.rounded_rectangle([64, 108, 742, 532], radius=30,
                        fill=(255, 255, 255, 11), outline=(255, 255, 255, 34), width=2)
    d.rounded_rectangle([96, 152, 372, 190], radius=19,
                        fill=(56, 189, 248, 28), outline=(56, 189, 248, 90), width=1)
    d.text((114, 160), "MODEL CONTEXT PROTOCOL", font=font("mono", 17),
           fill=(125, 211, 252, 255))
    d.text((96, 218), "qrcode-mcp", font=font("bold", 78),
           fill=(244, 247, 255, 255))
    d.text((98, 322), "Decode  ·  Repair  ·  Generate", font=font("regular", 32),
           fill=(198, 208, 232, 255))
    d.text((98, 366), "Route safely  ·  Read any text", font=font("regular", 32),
           fill=(198, 208, 232, 255))

    x = 98
    for label, col in [("stdio", (124, 140, 255)),
                       ("Streamable HTTP", (56, 189, 248)),
                       ("SSE", (45, 212, 191))]:
        f = font("mono", 19)
        w = int(d.textlength(label, font=f)) + 34
        d.rounded_rectangle([x, 430, x + w, 470], radius=20,
                            fill=(*col, 34), outline=(*col, 120), width=1)
        d.text((x + 17, 438), label, font=f, fill=(*col, 255))
        x += w + 12
    d.text((98, 490), "9 tools   /   offline by default   /   MIT",
           font=font("mono", 17), fill=(133, 147, 180, 255))
    img = Image.alpha_composite(img, layer)

    # right: a real QR symbol with a glare band and a "repaired" stamp
    qr = qr_image(qr_matrix("https://example.com/qrmcp"), module=8, quiet=3)
    plate = Image.new("RGBA", (360, 360), (0, 0, 0, 0))
    pd = ImageDraw.Draw(plate)
    pd.rounded_rectangle([0, 0, 359, 359], radius=24, fill=(255, 255, 255, 255))
    plate.alpha_composite(qr.convert("RGBA"), (22, 22))
    yy, xx = np.mgrid[0:360, 0:360].astype(np.float32)
    dist = (xx * 0.75 + yy * 0.66 - 250) / 46
    band = (np.exp(-dist ** 2) * 150).astype(np.uint8)
    glow_layer = Image.new("RGBA", (360, 360), (255, 255, 255, 0))
    clip = Image.new("L", (360, 360), 0)
    ImageDraw.Draw(clip).rounded_rectangle([0, 0, 359, 359], radius=24, fill=255)
    glow_layer.putalpha(Image.fromarray(
        np.minimum(band, np.asarray(clip)).astype(np.uint8), "L"))
    plate = Image.alpha_composite(plate, glow_layer)
    pd = ImageDraw.Draw(plate)
    pd.rounded_rectangle([176, 300, 344, 340], radius=20,
                         fill=(14, 116, 144, 235), outline=(56, 189, 248, 255), width=1)
    pd.text((194, 310), "qr_repair", font=font("mono", 19),
            fill=(224, 247, 255, 255))
    img.alpha_composite(plate, (838, 140))

    img.convert("RGB").save(OUT / "social-preview.png", optimize=True)


if __name__ == "__main__":
    build_logo(False).save(OUT / "logo-light.png", optimize=True)
    build_logo(True).save(OUT / "logo-dark.png", optimize=True)
    build_demo()
    build_social()
    for p in sorted(OUT.glob("*.png")):
        print(f"{p.name:24s} {p.stat().st_size / 1024:7.1f} KB")
