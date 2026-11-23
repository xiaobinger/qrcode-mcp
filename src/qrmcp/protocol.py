# -*- coding: utf-8 -*-
"""MCP JSON-RPC 2.0 protocol handling, independent of the transport."""
from __future__ import annotations

import json
from typing import Any

from . import SERVER_NAME, __version__
from .errors import (INTERNAL_ERROR, INVALID_PARAMS, INVALID_REQUEST,
                     METHOD_NOT_FOUND, PARSE_ERROR)
from .registry import call_tool, list_tools

SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
LATEST_PROTOCOL = SUPPORTED_PROTOCOLS[0]

INSTRUCTIONS = (
    "Local QR/Image toolbox. Use qr_decode for normal images, qr_repair for "
    "glare-damaged or low-resolution QR codes, qr_generate to create codes, "
    "qr_parse to interpret payloads, ocr_image for text. qr_open never opens a "
    "link without confirm=true and always requires a human-visible URL check."
)


def _error(rid: Any, code: int, message: str, data: Any = None) -> dict:
    err: dict = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": rid, "error": err}


def _ok(rid: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


class MCPProtocol:
    def __init__(self):
        self.protocol_version = LATEST_PROTOCOL
        self.client_info: dict | None = None
        self.client_capabilities: dict | None = None
        self.initialized = False

    # ---- request dispatch -------------------------------------------------
    def handle(self, msg: Any) -> dict | None:
        if isinstance(msg, list):
            if not msg:
                return _error(None, INVALID_REQUEST, "empty batch")
            replies = [r for m in msg if (r := self.handle(m)) is not None]
            return replies or None
        if not isinstance(msg, dict):
            return _error(None, INVALID_REQUEST, "request must be an object")
        if msg.get("jsonrpc") != "2.0":
            return _error(msg.get("id"), INVALID_REQUEST, "jsonrpc must be '2.0'")

        method = msg.get("method")
        rid = msg.get("id")
        is_notification = "id" not in msg
        if not isinstance(method, str):
            return None if is_notification else _error(rid, INVALID_REQUEST, "missing method")

        try:
            result = self._dispatch(method, msg.get("params") or {})
        except Exception as exc:  # noqa: BLE001
            if is_notification:
                return None
            return _error(rid, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")

        if is_notification:
            return None
        if result is self._METHOD_NOT_FOUND:
            return _error(rid, METHOD_NOT_FOUND, f"method not found: {method}")
        return _ok(rid, result)

    _METHOD_NOT_FOUND = object()

    def _dispatch(self, method: str, params: dict):
        if method == "initialize":
            return self._initialize(params)
        if method.startswith("notifications/"):
            if method == "notifications/initialized":
                self.initialized = True
            return {}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": list_tools()}
        if method == "tools/call":
            if not isinstance(params, dict):
                raise ValueError("params must be an object")
            name = params.get("name")
            if not isinstance(name, str):
                raise ValueError("tool name must be a string")
            return call_tool(name, params.get("arguments"))
        if method == "resources/list":
            return {"resources": []}
        if method == "resources/templates/list":
            return {"resourceTemplates": []}
        if method == "resources/read":
            return self._METHOD_NOT_FOUND
        if method == "prompts/list":
            return {"prompts": []}
        if method == "prompts/get":
            return self._METHOD_NOT_FOUND
        if method == "logging/setLevel":
            return {}
        if method == "completion/complete":
            return {"completion": {"values": [], "total": 0, "hasMore": False}}
        return self._METHOD_NOT_FOUND

    def _initialize(self, params: dict) -> dict:
        requested = params.get("protocolVersion")
        if requested in SUPPORTED_PROTOCOLS:
            self.protocol_version = requested
        else:
            self.protocol_version = LATEST_PROTOCOL
        self.client_info = params.get("clientInfo")
        self.client_capabilities = params.get("capabilities")
        return {
            "protocolVersion": self.protocol_version,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"subscribe": False, "listChanged": False},
                "prompts": {"listChanged": False},
                "logging": {},
            },
            "serverInfo": {"name": SERVER_NAME, "version": __version__},
            "instructions": INSTRUCTIONS,
        }
