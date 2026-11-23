# -*- coding: utf-8 -*-
"""Locate QR symbols without decoding them."""
from __future__ import annotations

import numpy as np

from ..registry import tool
from .imaging import cv2_optional, load_image


def position_points(result) -> list[list[float]] | None:
    """Extract four corner points from a zxing Result's Quadrilateral."""
    p = getattr(result, "position", None)
    if p is None:
        return None
    names = ("top_left", "top_right", "bottom_right", "bottom_left")
    try:
        pts = []
        for name in names:
            q = getattr(p, name, None)
            if q is None:
                return None
            pts.append([float(q.x), float(q.y)])
        return pts
    except (AttributeError, TypeError, ValueError):
        pass
    try:
        return [[float(v[0]), float(v[1])] for v in list(p)[:4]]
    except Exception:
        return None


def _bbox(points) -> list[int]:
    arr = np.asarray(points, dtype=float).reshape(-1, 2)
    x0, y0 = arr.min(axis=0)
    x1, y1 = arr.max(axis=0)
    return [int(round(x0)), int(round(y0)),
            int(round(x1 - x0)), int(round(y1 - y0))]


@tool(
    "qr_detect",
    "Locate QR symbols in an image without decoding. Returns bounding boxes and corner "
    "points, useful for cropping before qr_repair. Deduplicates results across engines.",
    {
        "image": {"type": "string", "description": "Local path, base64, data URI or http(s) URL"},
        "max_variants": {"type": "integer", "minimum": 1, "maximum": 12, "default": 4,
                         "description": "How many preprocessing variants to try"},
    },
    ("image",),
)
def qr_detect(image: str, max_variants: int = 4) -> dict:
    loaded = load_image(image)
    found: list[dict] = []
    engines: list[str] = []

    cv2 = cv2_optional()
    if cv2 is not None:
        engines.append("opencv")
        try:
            ok, pts = cv2.QRCodeDetector().detectMulti(loaded.gray)
            if ok and pts is not None:
                for p in np.asarray(pts).reshape(-1, 4, 2):
                    points = [[float(x), float(y)] for x, y in p]
                    found.append({"points": points, "bbox": _bbox(points),
                                  "engine": "opencv"})
        except Exception:
            pass

    try:
        import zxingcpp
        engines.append("zxing-cpp")
        from .imaging import variants
        for name, arr in variants(loaded.gray, limit=max_variants):
            try:
                results = zxingcpp.read_barcodes(
                    np.ascontiguousarray(arr), return_errors=True)
            except Exception:
                continue
            for r in results:
                points = position_points(r)
                if not points:
                    continue
                found.append({"points": points, "bbox": _bbox(points),
                              "engine": "zxing-cpp", "variant": name,
                              "decoded": bool(getattr(r, "text", ""))})
            if found:
                break
    except ImportError:
        pass

    # simple IoU-ish dedupe on bbox centre + size
    unique: list[dict] = []
    for item in found:
        x, y, w, h = item["bbox"]
        c = (x + w / 2, y + h / 2)
        if any(abs(c[0] - (u["bbox"][0] + u["bbox"][2] / 2)) < 12
               and abs(c[1] - (u["bbox"][1] + u["bbox"][3] / 2)) < 12
               and abs(w - u["bbox"][2]) < 20 for u in unique):
            continue
        unique.append(item)
    return {"ok": True, "count": len(unique), "boxes": unique,
            "engines": engines,
            "image": {"width": loaded.width, "height": loaded.height},
            "warnings": [] if unique else ["no QR symbol located"]}
