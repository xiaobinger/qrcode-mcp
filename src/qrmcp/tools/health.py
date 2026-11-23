# -*- coding: utf-8 -*-
"""Capability probe for clients and troubleshooting."""
from __future__ import annotations

import importlib.util
import os
import platform
import sys

from .. import SERVER_NAME, __version__
from ..protocol import SUPPORTED_PROTOCOLS
from ..registry import TOOL_DEFS, tool
from ..security import get_allowlist, output_dir


def _spec(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


@tool(
    "server_health",
    "Report server version, supported MCP protocol versions, transports, registered tools, "
    "available OCR backends and the active safety configuration.",
    {},
)
def server_health() -> dict:
    from .ocr import backend_status
    deps = {name: _spec(mod) for name, mod in {
        "zxing-cpp": "zxingcpp", "segno": "segno", "reedsolo": "reedsolo",
        "numpy": "numpy", "Pillow": "PIL", "opencv": "cv2", "pyzbar": "pyzbar",
        "winrt": "winrt", "rapidocr": "rapidocr_onnxruntime",
        "pytesseract": "pytesseract",
    }.items()}
    return {
        "ok": True,
        "server": SERVER_NAME,
        "version": __version__,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "protocol_versions": list(SUPPORTED_PROTOCOLS),
        "transports": ["stdio", "http (Streamable HTTP)", "sse (legacy 2024-11-05)"],
        "tools": sorted(TOOL_DEFS),
        "tool_count": len(TOOL_DEFS),
        "dependencies": deps,
        "ocr_backends": backend_status(),
        "safety": {
            "output_dir": output_dir(),
            "url_allowlist": get_allowlist(),
            "allow_remote_images": os.environ.get("QRMCP_ALLOW_REMOTE", "0") in ("1", "true"),
            "allow_private_hosts": os.environ.get("QRMCP_ALLOW_PRIVATE", "0") in ("1", "true"),
        },
    }
