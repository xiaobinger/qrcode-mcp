# -*- coding: utf-8 -*-
"""Image input normalisation and preprocessing variants.

Only numpy + Pillow are required here; OpenCV is an optional acceleration.
"""
from __future__ import annotations

import base64
import binascii
import io
import os
import re
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import unquote_to_bytes, urlsplit

import numpy as np
from PIL import Image, ImageFilter

from ..errors import ToolError
from ..security import (MAX_IMAGE_BYTES, MAX_IMAGE_PIXELS, env_flag)

_B64_RE = re.compile(r"^[A-Za-z0-9+/\s=]+$")
_DATA_URI_RE = re.compile(r"^data:image/[A-Za-z0-9.+-]+;base64,", re.I)


@dataclass
class LoadedImage:
    pil: Image.Image
    gray: np.ndarray
    source: str
    width: int
    height: int
    meta: dict = field(default_factory=dict)


def _decode_bytes(data: bytes, source: str) -> LoadedImage:
    if len(data) > MAX_IMAGE_BYTES:
        raise ToolError("image exceeds QRMCP_MAX_IMAGE_BYTES",
                        {"bytes": len(data), "limit": MAX_IMAGE_BYTES})
    try:
        pil = Image.open(io.BytesIO(data))
        pil.load()
    except Exception as exc:
        raise ToolError("cannot decode image", {"detail": str(exc)})
    w, h = pil.size
    if w * h > MAX_IMAGE_PIXELS:
        raise ToolError("image has too many pixels",
                        {"pixels": w * h, "limit": MAX_IMAGE_PIXELS})
    rgb = pil.convert("RGB")
    gray = np.asarray(rgb.convert("L"), dtype=np.uint8)
    return LoadedImage(rgb, gray, source, w, h,
                       {"format": pil.format, "mode": pil.mode, "bytes": len(data)})


def _from_b64(b64: str, source: str) -> LoadedImage:
    try:
        data = base64.b64decode(re.sub(r"\s+", "", b64), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ToolError("invalid base64 image", {"detail": str(exc)})
    return _decode_bytes(data, source)


def load_image(source: str, *, allow_remote: bool | None = None) -> LoadedImage:
    """Accept a local path, file:// URI, data URI, bare base64 or http(s) URL."""
    if not isinstance(source, str) or not source.strip():
        raise ToolError("image source is empty")
    src = source.strip()

    if src.startswith("data:") and _DATA_URI_RE.match(src):
        return _from_b64(src.split(",", 1)[1], "data-uri")
    if src.startswith("data:"):
        # percent-encoded data URI
        try:
            head, payload = src.split(",", 1)
            data = unquote_to_bytes(payload)
            if "base64" in head:
                import base64 as _b64
                data = _b64.b64decode(data)
            return _decode_bytes(data, "data-uri")
        except Exception as exc:
            raise ToolError("invalid data URI", {"detail": str(exc)})

    if src.startswith("file://"):
        path = urllib.request.url2pathname(urlsplit(src).path)
        return _load_path(path, src)

    if src.lower().startswith(("http://", "https://")):
        permitted = env_flag("QRMCP_ALLOW_REMOTE") if allow_remote is None else allow_remote
        if not permitted:
            raise ToolError(
                "remote image fetching is disabled",
                {"hint": "set QRMCP_ALLOW_REMOTE=1, or pass a local path / base64"},
            )
        try:
            req = urllib.request.Request(src, headers={"User-Agent": "qrcode-mcp/0.1"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read(MAX_IMAGE_BYTES + 1)
        except Exception as exc:
            raise ToolError("cannot fetch remote image", {"detail": str(exc)})
        if len(data) > MAX_IMAGE_BYTES:
            raise ToolError("remote image too large", {"limit": MAX_IMAGE_BYTES})
        return _decode_bytes(data, src)

    if (len(src) > 120 and _B64_RE.match(src)
            and os.path.sep not in src and not os.path.exists(src)):
        return _from_b64(src, "base64")

    return _load_path(src, src)


def _load_path(path: str, display: str) -> LoadedImage:
    if not os.path.isfile(path):
        raise ToolError("image file not found", {"path": path})
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    if size > MAX_IMAGE_BYTES:
        raise ToolError("image file exceeds QRMCP_MAX_IMAGE_BYTES",
                        {"bytes": size, "limit": MAX_IMAGE_BYTES})
    with open(path, "rb") as fh:
        return _decode_bytes(fh.read(), display)


# ---- preprocessing --------------------------------------------------------
def otsu_threshold(gray: np.ndarray) -> int:
    hist = np.bincount(gray.ravel(), minlength=256).astype(float)
    total = gray.size
    sum_all = float(np.dot(np.arange(256), hist))
    sum_b = 0.0
    w_b = 0.0
    best_t, best_var = 0, -1.0
    for t in range(256):
        w_b += hist[t]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += t * hist[t]
        m_b = sum_b / w_b
        m_f = (sum_all - sum_b) / w_f
        var = w_b * w_f * (m_b - m_f) ** 2
        if var > best_var:
            best_var, best_t = var, t
    return best_t


def quiet_zone(gray: np.ndarray, border: int = 20) -> np.ndarray:
    return np.pad(gray, ((border, border), (border, border)),
                  mode="constant", constant_values=255)


def variants(gray: np.ndarray, limit: int = 24):
    """Yield (name, uint8 image) preprocessing variants, cheapest first."""
    n = 0

    def emit(name, arr):
        nonlocal n
        if n < limit:
            n += 1
            return (name, np.ascontiguousarray(arr, dtype=np.uint8))
        return None

    out = emit("gray", gray)
    if out:
        yield out
    h, w = gray.shape
    pil = Image.fromarray(gray)

    for factor in (2, 4):
        out = emit(f"up{factor}x", np.asarray(
            pil.resize((w * factor, h * factor), Image.Resampling.LANCZOS)))
        if out:
            yield out

    out = emit("invert", 255 - gray)
    if out:
        yield out

    g = gray.astype(np.float32)
    bg = np.asarray(pil.filter(ImageFilter.GaussianBlur(6)), dtype=np.float32)
    norm = np.clip(g - bg + 128, 0, 255).astype(np.uint8)
    out = emit("illum_norm", norm)
    if out:
        yield out

    white = np.asarray(pil.filter(ImageFilter.MaxFilter(9)), dtype=np.float32)
    contrast = np.clip((white - g) / (white + 1.0) * 255.0, 0, 255).astype(np.uint8)
    out = emit("local_contrast", contrast)
    if out:
        yield out

    for name, arr in (("gray", gray), ("illum_norm", norm),
                      ("local_contrast", contrast)):
        t = otsu_threshold(arr)
        out = emit(f"{name}_otsu{t}", (arr > t).astype(np.uint8) * 255)
        if out:
            yield out
        out = emit(f"{name}_otsu_inv{t}", (arr <= t).astype(np.uint8) * 255)
        if out:
            yield out

    for angle in (90, 180, 270):
        out = emit(f"rot{angle}", np.asarray(pil.rotate(angle, expand=True)))
        if out:
            yield out

    out = emit("mirror", np.fliplr(gray))
    if out:
        yield out
    out = emit("quiet", quiet_zone(gray))
    if out:
        yield out


def cv2_optional():
    try:
        import cv2  # noqa: F401
        return cv2
    except Exception:
        return None
