# -*- coding: utf-8 -*-
"""QR generation via segno (pure Python, no C extension)."""
from __future__ import annotations

import base64
import io
import os

import segno

from ..errors import ToolError
from ..registry import tool
from ..security import MAX_GENERATED_CHARS, resolve_output_path

_EXT = {"png": ".png", "svg": ".svg", "txt": ".txt", "ascii": ".txt"}


def _matrix_ascii(q) -> str:
    return "\n".join("".join("\u2588\u2588" if v else "  " for v in row)
                     for row in q.matrix)


@tool(
    "qr_generate",
    "Generate a QR code. Supports error-correction level, fixed version/mask, scale, "
    "quiet zone, colors and PNG/SVG/ASCII output. Optionally saves to the sandbox output directory.",
    {
        "text": {"type": "string", "minLength": 1, "maxLength": 8192,
                 "description": "Content to encode"},
        "error": {"type": "string", "enum": ["L", "M", "Q", "H"], "default": "M",
                  "description": "Error correction level"},
        "version": {"type": ["integer", "null"], "minimum": 1, "maximum": 40,
                    "default": None, "description": "Force QR version 1-40, or auto"},
        "mask": {"type": ["integer", "null"], "minimum": 0, "maximum": 7,
                 "default": None, "description": "Force mask pattern 0-7, or auto"},
        "scale": {"type": "integer", "minimum": 1, "maximum": 40, "default": 8,
                  "description": "Pixels per module"},
        "border": {"type": "integer", "minimum": 0, "maximum": 40, "default": 4,
                   "description": "Quiet-zone width in modules"},
        "dark": {"type": ["string", "null"], "default": "#000000",
                 "description": "Dark module color (#RRGGBB or a CSS name)"},
        "light": {"type": ["string", "null"], "default": "#FFFFFF",
                  "description": "Light module color; null means transparent"},
        "format": {"type": "string", "enum": ["png", "svg", "txt", "ascii"],
                   "default": "png", "description": "Output format"},
        "output": {"type": ["string", "null"], "default": None,
                   "description": "File name inside QRMCP_OUTPUT_DIR (relative only)"},
        "save": {"type": "boolean", "default": True,
                 "description": "Write the file to disk"},
        "include_base64": {"type": "boolean", "default": True,
                           "description": "Include a PNG data URI in the result"},
    },
    ("text",),
)
def qr_generate(text: str, error: str = "M", version: int | None = None,
                mask: int | None = None, scale: int = 8, border: int = 4,
                dark: str | None = "#000000", light: str | None = "#FFFFFF",
                format: str = "png", output: str | None = None,
                save: bool = True, include_base64: bool = True) -> dict:
    if len(text) > MAX_GENERATED_CHARS:
        raise ToolError("content too long for QR generation",
                        {"length": len(text), "limit": MAX_GENERATED_CHARS})
    fmt = format.lower()
    if fmt not in _EXT:
        raise ToolError("unsupported format", {"format": fmt})
    kwargs = {"error": error.lower(), "boost_error": False}
    if version is not None:
        kwargs["version"] = version
    if mask is not None:
        kwargs["mask"] = mask
    try:
        q = segno.make(text, **kwargs)
    except Exception as exc:
        raise ToolError("cannot encode content",
                        {"detail": str(exc), "hint": "try a smaller version=none or lower error level"})

    result = {
        "ok": True,
        "text": text,
        "version": int(q.version),
        "error": str(q.error).upper(),
        "mask": int(q.mask) if q.mask is not None else None,
        "modules": len(q.matrix),
        "format": fmt,
        "scale": scale,
        "border": border,
        "path": None,
    }

    if fmt in ("txt", "ascii"):
        art = _matrix_ascii(q)
        result["ascii"] = art
        result["size_bytes"] = len(art.encode("utf-8"))
        blob = art.encode("utf-8")
    else:
        buf = io.BytesIO()
        try:
            q.save(buf, kind=fmt, scale=scale, border=border,
                   dark=dark, light=light)
        except Exception as exc:
            raise ToolError("cannot render QR", {"detail": str(exc)})
        blob = buf.getvalue()
        result["size_bytes"] = len(blob)
        if fmt == "png" and include_base64:
            b64 = base64.b64encode(blob).decode("ascii")
            result["png_base64"] = b64
            result["data_uri"] = "data:image/png;base64," + b64
        if fmt == "svg":
            svg = blob.decode("utf-8", errors="replace")
            result["svg"] = svg if len(svg) <= 20000 else svg[:20000] + "<!-- truncated -->"

    if save:
        name = output or ("qrcode" + _EXT[fmt])
        if output and not name.lower().endswith(_EXT[fmt]):
            name += _EXT[fmt]
        path = resolve_output_path(name)
        with open(path, "wb") as fh:
            fh.write(blob)
        result["path"] = path
    return result
