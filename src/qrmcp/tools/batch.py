# -*- coding: utf-8 -*-
"""Batch scanning across a list of sources or a directory."""
from __future__ import annotations

import glob
import os
import time

from ..errors import ToolError
from ..registry import tool


@tool(
    "qr_batch",
    "Scan many images in one call. Accepts an explicit list of sources or a directory "
    "plus glob pattern; optionally runs OCR on every image too. Returns per-file results "
    "and aggregate counts.",
    {
        "sources": {"type": ["array", "null"], "default": None,
                    "description": "List of paths / base64 / data URIs"},
        "directory": {"type": ["string", "null"], "default": None,
                      "description": "Directory to scan (non-recursive)"},
        "pattern": {"type": "string", "default": "*.png",
                    "description": "Glob pattern inside directory"},
        "max_files": {"type": "integer", "minimum": 1, "maximum": 500,
                      "default": 50, "description": "Hard cap on files processed"},
        "include_ocr": {"type": "boolean", "default": False,
                        "description": "Also run OCR on each image"},
        "ocr_backend": {"type": "string", "enum": ["auto", "windows", "rapid", "tesseract"],
                        "default": "auto"},
        "repair": {"type": "boolean", "default": True,
                   "description": "Allow damaged-code repair per file"},
        "max_variants": {"type": "integer", "minimum": 1, "maximum": 20,
                         "default": 8, "description": "Preprocessing variants per file"},
    },
)
def qr_batch(sources: list | None = None, directory: str | None = None,
             pattern: str = "*.png", max_files: int = 50,
             include_ocr: bool = False, ocr_backend: str = "auto",
             repair: bool = True, max_variants: int = 8) -> dict:
    items: list[str] = []
    if sources:
        items.extend(str(s) for s in sources)
    if directory:
        if not os.path.isdir(directory):
            raise ToolError("directory not found", {"directory": directory})
        items.extend(sorted(glob.glob(os.path.join(directory, pattern)))[:max_files])
    if not items:
        raise ToolError("no input files", {"hint": "provide sources or directory+pattern"})
    items = items[:max_files]

    from .decode import decode_image
    from .ocr import ocr_image
    started = time.time()
    rows = []
    total_codes = 0
    for src in items:
        row: dict = {"source": src, "ok": False, "codes": [], "errors": []}
        try:
            d = decode_image(src, repair=repair, max_variants=max_variants,
                             include_parse=True, budget_ms=6000)
            row["codes"] = [{"text": r["text"], "format": r.get("format"),
                             "engine": r.get("engine"),
                             "parse_type": r.get("parse", {}).get("type")}
                            for r in d.get("results", [])]
            row["ok"] = bool(row["codes"])
            total_codes += len(row["codes"])
            row["warnings"] = d.get("warnings", [])
        except Exception as exc:
            row["errors"].append(f"decode: {type(exc).__name__}: {exc}")
        if include_ocr:
            try:
                o = ocr_image(src, backend=ocr_backend, detail=False)
                row["ocr"] = {"backend": o["backend"], "text": o["text"]}
            except Exception as exc:
                row["errors"].append(f"ocr: {type(exc).__name__}: {exc}")
        rows.append(row)
    return {"ok": True, "files": len(rows), "total_codes": total_codes,
            "files_with_codes": sum(1 for r in rows if r["codes"]),
            "results": rows, "elapsed_ms": int((time.time() - started) * 1000)}
