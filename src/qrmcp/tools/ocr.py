# -*- coding: utf-8 -*-
"""Pluggable OCR: Windows built-in OCR (WinRT), RapidOCR, Tesseract.

Every backend is optional. Missing backends never break server startup; they
are reported by server_health and by the error hint of ocr_image.
"""
from __future__ import annotations

import asyncio
import importlib.util
import io
import os
import threading
import time
from typing import Any

import numpy as np
from PIL import Image

from ..errors import ToolError
from ..registry import tool

DEFAULT_PRIORITY = ("windows", "rapid", "tesseract")
_WIN_LOCK = threading.Lock()


def _installed(mod: str) -> bool:
    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):
        return False


def _line(text: str, box=None, confidence=None, words=None) -> dict:
    out: dict[str, Any] = {"text": text, "box": box, "confidence": confidence}
    if words is not None:
        out["words"] = words
    return out


class BaseOCR:
    name = "base"

    @classmethod
    def available(cls) -> bool:
        return False

    @classmethod
    def hint(cls) -> str:
        return ""

    def recognize(self, pil: Image.Image, language: str | None = None) -> dict:
        raise NotImplementedError


class WindowsOCR(BaseOCR):
    """Windows.Media.Ocr via the winrt bindings, using an in-memory stream.

    StorageFile.get_file_from_path_async is broken in current Python/WinRT
    bindings, so the bitmap is decoded from an InMemoryRandomAccessStream.
    """

    name = "windows"

    @classmethod
    def available(cls) -> bool:
        return all(_installed(m) for m in (
            "winrt.windows.media.ocr", "winrt.windows.graphics.imaging",
            "winrt.windows.storage.streams", "winrt.runtime"))

    @classmethod
    def hint(cls) -> str:
        return "pip install 'qrcode-mcp[ocr-windows]' (Windows only, uses the built-in zh-Hans-CN recognizer)"

    def recognize(self, pil: Image.Image, language: str | None = None) -> dict:
        buf = io.BytesIO()
        pil.save(buf, format="PNG")
        payload = buf.getvalue()
        with _WIN_LOCK:
            return asyncio.run(self._run(payload, language))

    @staticmethod
    async def _run(payload: bytes, language: str | None) -> dict:
        from winrt.windows.graphics.imaging import BitmapDecoder
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.storage.streams import InMemoryRandomAccessStream, DataWriter

        stream = InMemoryRandomAccessStream()
        writer = DataWriter(stream)
        writer.write_bytes(payload)
        await writer.store_async()
        await stream.flush_async()
        stream.seek(0)
        decoder = await BitmapDecoder.create_async(stream)
        bitmap = await decoder.get_software_bitmap_async()

        warnings: list[str] = []
        engine = None
        if language:
            try:
                from winrt.windows.globalization import Language
                engine = OcrEngine.try_create_from_language(Language(language))
                if engine is None:
                    warnings.append(f"OCR language {language!r} unavailable; using system default")
            except Exception as exc:
                warnings.append(f"cannot select OCR language: {exc}")
        if engine is None:
            engine = OcrEngine.try_create_from_user_profile_languages()
        if engine is None:
            raise ToolError("no Windows OCR engine available")

        result = await engine.recognize_async(bitmap)
        lang = engine.recognizer_language.language_tag
        lines = []
        for ln in result.lines:
            words = []
            for wd in ln.words:
                r = wd.bounding_rect
                words.append({"text": wd.text,
                              "box": [r.x, r.y, r.width, r.height]})
            if words:
                x0 = min(w["box"][0] for w in words)
                y0 = min(w["box"][1] for w in words)
                x1 = max(w["box"][0] + w["box"][2] for w in words)
                y1 = max(w["box"][1] + w["box"][3] for w in words)
                box = [x0, y0, x1 - x0, y1 - y0]
            else:
                box = None
            lines.append(_line(ln.text, box, None, words))
        try:
            stream.close()
        except Exception:
            pass
        return {"text": result.text, "lines": lines,
                "language": lang, "warnings": warnings,
                "confidence_supported": False}


class RapidOCRBackend(BaseOCR):
    name = "rapid"
    _engine = None

    @classmethod
    def available(cls) -> bool:
        return _installed("rapidocr_onnxruntime")

    @classmethod
    def hint(cls) -> str:
        return "pip install 'qrcode-mcp[ocr-rapid]' (offline Chinese+English ONNX models)"

    def recognize(self, pil: Image.Image, language: str | None = None) -> dict:
        if RapidOCRBackend._engine is None:
            from rapidocr_onnxruntime import RapidOCR
            RapidOCRBackend._engine = RapidOCR()
        result, elapse = RapidOCRBackend._engine(np.asarray(pil.convert("RGB")))
        lines = []
        for item in result or []:
            try:
                box, text, score = item
                pts = [[float(p[0]), float(p[1])] for p in box]
                x0 = min(p[0] for p in pts); y0 = min(p[1] for p in pts)
                x1 = max(p[0] for p in pts); y1 = max(p[1] for p in pts)
                lines.append(_line(str(text), [x0, y0, x1 - x0, y1 - y0],
                                   float(score), None))
            except (ValueError, TypeError, IndexError):
                continue
        return {"text": " ".join(l["text"] for l in lines), "lines": lines,
                "language": language or "ch+en", "warnings": [],
                "confidence_supported": True,
                "timing": elapse if isinstance(elapse, (list, tuple)) else None}


class TesseractOCR(BaseOCR):
    name = "tesseract"

    @classmethod
    def available(cls) -> bool:
        if not _installed("pytesseract"):
            return False
        cmd = os.environ.get("QRMCP_TESSERACT_CMD")
        if cmd:
            return os.path.isfile(cmd)
        import shutil
        return shutil.which("tesseract") is not None

    @classmethod
    def hint(cls) -> str:
        return "install Tesseract + chi_sim traineddata, then pip install 'qrcode-mcp[ocr-tesseract]'"

    def recognize(self, pil: Image.Image, language: str | None = None) -> dict:
        import pytesseract
        cmd = os.environ.get("QRMCP_TESSERACT_CMD")
        if cmd:
            pytesseract.pytesseract.tesseract_cmd = cmd
        data = pytesseract.image_to_data(
            pil, lang=language or "eng",
            output_type=pytesseract.Output.DICT)
        grouped: dict[tuple, list] = {}
        for i, txt in enumerate(data.get("text", [])):
            if not str(txt).strip():
                continue
            key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
            grouped.setdefault(key, []).append({
                "text": str(txt),
                "box": [data["left"][i], data["top"][i],
                        data["width"][i], data["height"][i]],
                "confidence": float(data["conf"][i]) / 100.0,
            })
        lines = []
        for words in grouped.values():
            x0 = min(w["box"][0] for w in words); y0 = min(w["box"][1] for w in words)
            x1 = max(w["box"][0] + w["box"][2] for w in words)
            y1 = max(w["box"][1] + w["box"][3] for w in words)
            lines.append(_line(" ".join(w["text"] for w in words),
                               [x0, y0, x1 - x0, y1 - y0],
                               sum(w["confidence"] for w in words) / len(words),
                               words))
        return {"text": "\n".join(l["text"] for l in lines), "lines": lines,
                "language": language or "eng", "warnings": [],
                "confidence_supported": True}


BACKENDS: dict[str, type[BaseOCR]] = {
    "windows": WindowsOCR, "rapid": RapidOCRBackend, "tesseract": TesseractOCR,
}


def backend_status() -> dict:
    out = {}
    for name, cls in BACKENDS.items():
        out[name] = {"available": cls.available(), "install_hint": cls.hint()}
    return out


@tool(
    "ocr_image",
    "Recognize text in an image with pluggable OCR backends. "
    "backend=auto tries Windows built-in OCR, then RapidOCR, then Tesseract. "
    "Returns full text, per-line boxes/confidence and word boxes when available.",
    {
        "image": {"type": "string", "description": "Local path, base64, data URI or http(s) URL"},
        "backend": {"type": "string", "enum": ["auto", "windows", "rapid", "tesseract"],
                    "default": "auto", "description": "OCR engine"},
        "language": {"type": ["string", "null"], "default": None,
                     "description": "Windows BCP-47 tag (zh-Hans-CN) or Tesseract lang (chi_sim+eng)"},
        "upscale": {"type": "boolean", "default": True,
                    "description": "Enlarge small images 2x before recognition"},
        "detail": {"type": "boolean", "default": True,
                   "description": "Include line/word boxes; false returns text only"},
        "priority": {"type": ["array", "null"], "default": None,
                     "description": "Override auto backend order"},
    },
    ("image",),
)
def ocr_image(image: str, backend: str = "auto", language: str | None = None,
              upscale: bool = True, detail: bool = True,
              priority: list | None = None) -> dict:
    from .imaging import load_image
    loaded = load_image(image)
    pil = loaded.pil
    if upscale and max(pil.size) < 1000:
        pil = pil.resize((pil.width * 2, pil.height * 2), Image.Resampling.LANCZOS)

    if backend != "auto":
        names = [backend]
    elif priority:
        names = [str(n) for n in priority if str(n) in BACKENDS] or list(DEFAULT_PRIORITY)
    else:
        env_order = os.environ.get("QRMCP_OCR_PRIORITY")
        names = ([x.strip() for x in env_order.split(",") if x.strip() in BACKENDS]
                 if env_order else list(DEFAULT_PRIORITY))

    tried: list[dict] = []
    for name in names:
        cls = BACKENDS.get(name)
        if cls is None:
            continue
        if not cls.available():
            tried.append({"backend": name, "ok": False,
                          "reason": "not installed", "hint": cls.hint()})
            continue
        t0 = time.time()
        try:
            res = cls().recognize(pil, language)
        except Exception as exc:
            tried.append({"backend": name, "ok": False,
                          "reason": f"{type(exc).__name__}: {exc}"})
            continue
        if not res.get("text", "").strip():
            tried.append({"backend": name, "ok": False, "reason": "no text recognized"})
            continue
        out: dict[str, Any] = {
            "ok": True,
            "backend": name,
            "text": res["text"],
            "language": res.get("language"),
            "elapsed_ms": int((time.time() - t0) * 1000),
            "confidence_supported": res.get("confidence_supported", False),
            "warnings": res.get("warnings", []),
            "tried": tried,
            "image": {"width": loaded.width, "height": loaded.height,
                      "processed": {"width": pil.width, "height": pil.height}},
        }
        if detail:
            out["lines"] = res.get("lines", [])
            out["word_count"] = sum(len(l.get("words") or []) for l in res.get("lines", []))
        return out

    raise ToolError(
        "no OCR backend produced text",
        {"tried": tried,
         "available": {k: v["available"] for k, v in backend_status().items()},
         "hint": "install one of: " + " | ".join(c.hint() for c in BACKENDS.values())},
    )
