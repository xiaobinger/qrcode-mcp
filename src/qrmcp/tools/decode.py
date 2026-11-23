# -*- coding: utf-8 -*-
"""Layered QR/2D-code decoding with an optional damaged-code repair fallback."""
from __future__ import annotations

import os
import time

import numpy as np
from PIL import Image

from ..registry import tool
from .imaging import cv2_optional, load_image, variants
from .parse import parse_content

_FORMAT_NAMES = {
    "QRCode": "QRCode", "MicroQRCode": "MicroQRCode", "rMQRCode": "rMQRCode",
    "DataMatrix": "DataMatrix", "Aztec": "Aztec", "PDF417": "PDF417",
    "EAN13": "EAN13", "EAN8": "EAN8", "Code128": "Code128", "Code39": "Code39",
    "UPCA": "UPCA", "UPCE": "UPCE", "ITF": "ITF", "Codabar": "Codabar",
}
DEFAULT_FORMATS = ("QRCode", "MicroQRCode", "rMQRCode")


def _zxing_formats(names):
    import zxingcpp
    f = None
    for name in names:
        attr = getattr(zxingcpp.BarcodeFormat, name, None)
        if attr is not None:
            f = attr if f is None else f | attr
    return f


def _zxing(arr, formats, budget_left):
    import zxingcpp
    out = []
    f = _zxing_formats(formats)
    opts = {"formats": f, "try_rotate": True, "try_downscale": True,
            "return_errors": False}
    binarizers = [zxingcpp.Binarizer.LocalAverage,
                  zxingcpp.Binarizer.GlobalHistogram,
                  zxingcpp.Binarizer.BoolCast]
    for b in binarizers:
        if time.time() > budget_left:
            break
        try:
            results = zxingcpp.read_barcodes(np.ascontiguousarray(arr),
                                             binarizer=b, **opts)
        except Exception:
            continue
        for r in results:
            text = getattr(r, "text", "") or ""
            if not text:
                continue
            fmt = getattr(getattr(r, "format", None), "name", None) or "unknown"
            ec = getattr(r, "ec_level", None)
            out.append({"text": text, "format": fmt,
                        "ec_level": str(ec) if ec is not None else None,
                        "engine": "zxing-cpp", "binarizer": b.name,
                        "points": _points(r), "confidence": 1.0})
        if out:
            break
    return out


def _points(r):
    try:
        from .detect import position_points
        return position_points(r)
    except Exception:
        return None


def _opencv(arr, budget_left):
    cv2 = cv2_optional()
    if cv2 is None or time.time() > budget_left:
        return []
    out = []
    try:
        det = cv2.QRCodeDetector()
        ok, texts, pts, _ = det.detectAndDecodeMulti(np.ascontiguousarray(arr))
        if ok and texts:
            for i, t in enumerate(texts):
                if not t:
                    continue
                p = pts[i].tolist() if pts is not None and i < len(pts) else None
                out.append({"text": t, "format": "QRCode", "ec_level": None,
                            "engine": "opencv", "points": p, "confidence": 0.95})
        if not out:
            s, p, _ = det.detectAndDecode(np.ascontiguousarray(arr))
            if s:
                out.append({"text": s, "format": "QRCode", "ec_level": None,
                            "engine": "opencv",
                            "points": p.tolist() if p is not None else None,
                            "confidence": 0.95})
    except Exception:
        pass
    return out


def _zbar(arr, budget_left):
    if time.time() > budget_left:
        return []
    try:
        from pyzbar.pyzbar import decode as zbar_decode
    except Exception:
        return []
    out = []
    try:
        for r in zbar_decode(Image.fromarray(arr)):
            try:
                text = r.data.decode("utf-8")
            except UnicodeDecodeError:
                text = r.data.decode("latin-1")
            rect = getattr(r, "rect", None)
            out.append({"text": text, "format": str(getattr(r, "type", "unknown")),
                        "ec_level": None, "engine": "zbar",
                        "bbox": [rect.left, rect.top, rect.width, rect.height]
                        if rect is not None else None,
                        "confidence": 0.95})
    except Exception:
        pass
    return out


def decode_image(source: str, *, repair: bool = True, max_variants: int = 24,
                 formats=None, include_parse: bool = True,
                 budget_ms: int | None = None) -> dict:
    """Shared decoder used by qr_decode, qr_open and qr_batch."""
    started = time.time()
    budget = (budget_ms if budget_ms is not None
              else int(os.environ.get("QRMCP_DECODE_BUDGET_MS", "12000")))
    deadline = started + budget / 1000.0
    loaded = load_image(source)
    names = tuple(formats) if formats else DEFAULT_FORMATS
    unknown = [n for n in names if n not in _FORMAT_NAMES]
    if unknown:
        from ..errors import ParamError
        raise ParamError("unknown barcode format", {"unknown": unknown,
                                                    "allowed": sorted(_FORMAT_NAMES)})

    results: list[dict] = []
    seen: set[str] = set()
    variants_tried = 0
    engines_used: list[str] = []
    for name, arr in variants(loaded.gray, limit=max_variants):
        if time.time() > deadline:
            break
        variants_tried += 1
        for fn in (_zxing, _opencv, _zbar):
            hits = (fn(arr, names, deadline) if fn is _zxing
                    else fn(arr, deadline))
            for h in hits:
                h["variant"] = name
                if h["engine"] not in engines_used:
                    engines_used.append(h["engine"])
                key = h["text"]
                if key not in seen:
                    seen.add(key)
                    results.append(h)
        if results:
            break

    warnings: list[str] = []
    repair_used = False
    repair_detail = None
    if not results and repair:
        repair_used = True
        try:
            from .repair import qr_repair
            rp = qr_repair(image=source, save_clean=False)
            repair_detail = rp
            if rp.get("ok") and rp.get("text"):
                results.append({
                    "text": rp["text"], "format": "QRCode",
                    "ec_level": rp.get("format", {}).get("ec"),
                    "engine": "qr-repair", "variant": "grid+RS",
                    "points": None,
                    "confidence": rp.get("verification", {}).get("agreement", 0.8),
                    "repaired": True,
                })
            else:
                warnings.append("damaged-code repair did not fully recover the payload")
                soft = rp.get("soft_decode", {}).get("soft_text")
                if soft:
                    warnings.append("best-effort reading: " + soft)
        except Exception as exc:
            warnings.append(f"repair failed: {type(exc).__name__}: {exc}")

    if include_parse:
        for h in results:
            h["parse"] = parse_content(h["text"])
            h["repaired"] = h.get("engine") == "qr-repair"

    output = {
        "ok": bool(results),
        "count": len(results),
        "results": results,
        "source": loaded.source,
        "image": {"width": loaded.width, "height": loaded.height,
                  "bytes": loaded.meta.get("bytes")},
        "engines_used": engines_used,
        "variants_tried": variants_tried,
        "repair_used": repair_used,
        "elapsed_ms": int((time.time() - started) * 1000),
        "warnings": warnings or ([] if results else ["no barcode decoded"]),
    }
    if repair_detail is not None:
        output["repair"] = {
            "ok": repair_detail.get("ok"),
            "stage": repair_detail.get("stage"),
            "grid": repair_detail.get("grid"),
            "format": repair_detail.get("format"),
            "verification": repair_detail.get("verification"),
            "rs_diagnostics": repair_detail.get("rs_diagnostics"),
            "soft_decode": repair_detail.get("soft_decode"),
            "warnings": repair_detail.get("warnings"),
        }
    return output


@tool(
    "qr_decode",
    "Decode one or more QR/2D codes from an image. Tries zxing-cpp (multiple binarizers), "
    "OpenCV, ZBar and preprocessing variants; if all fail it automatically falls back to "
    "qr_repair for glare/blur/low-resolution damage. Returns text, symbology, EC level, "
    "corner points, confidence and a parsed payload with URL risk flags.",
    {
        "image": {"type": "string", "description": "Local path, base64, data URI or http(s) URL"},
        "repair": {"type": "boolean", "default": True,
                   "description": "Try damaged-code recovery when normal decoding fails"},
        "max_variants": {"type": "integer", "minimum": 1, "maximum": 40,
                         "default": 24, "description": "Max preprocessing variants"},
        "formats": {"type": ["array", "null"], "default": None,
                    "description": "Symbologies to try, e.g. [\"QRCode\",\"DataMatrix\"]"},
        "include_parse": {"type": "boolean", "default": True,
                          "description": "Attach semantic/risk analysis to each result"},
        "budget_ms": {"type": ["integer", "null"], "minimum": 100, "maximum": 120000,
                      "default": None, "description": "Time budget in milliseconds"},
    },
    ("image",),
)
def qr_decode(image: str, repair: bool = True, max_variants: int = 24,
              formats: list | None = None, include_parse: bool = True,
              budget_ms: int | None = None) -> dict:
    return decode_image(image, repair=repair, max_variants=max_variants,
                        formats=formats, include_parse=include_parse,
                        budget_ms=budget_ms)
