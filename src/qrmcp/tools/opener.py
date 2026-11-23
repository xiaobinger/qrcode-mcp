# -*- coding: utf-8 -*-
"""Open a QR URL in the system browser, behind explicit confirmation."""
from __future__ import annotations

import webbrowser

from ..errors import ToolError
from ..registry import tool
from ..security import check_url, get_allowlist
from .parse import parse_content


@tool(
    "qr_open",
    "Validate and optionally open a QR-code URL in the system browser. "
    "IMPORTANT: show the URL and its risk flags to the user before calling with confirm=true. "
    "Without confirm=true this tool only validates and reports; it never opens anything. "
    "Blocks file/javascript/data schemes, private/loopback hosts (SSRF) and hosts outside "
    "QRMCP_URL_ALLOWLIST.",
    {
        "url": {"type": ["string", "null"], "default": None,
                "description": "URL to open. Omit when using image=."},
        "image": {"type": ["string", "null"], "default": None,
                  "description": "Decode this image and open its first URL instead"},
        "confirm": {"type": "boolean", "default": False,
                    "description": "Must be true to actually launch the browser"},
        "dry_run": {"type": "boolean", "default": False,
                    "description": "Validate only, never open even if confirm=true"},
        "allow_private": {"type": "boolean", "default": False,
                          "description": "Allow hosts resolving to private/loopback IPs"},
        "allowlist": {"type": ["array", "null"], "default": None,
                      "description": "Override QRMCP_URL_ALLOWLIST for this call"},
        "new_tab": {"type": "boolean", "default": True,
                    "description": "Open in a new tab when supported"},
    },
)
def qr_open(url: str | None = None, image: str | None = None,
            confirm: bool = False, dry_run: bool = False,
            allow_private: bool = False, allowlist: list | None = None,
            new_tab: bool = True) -> dict:
    source = "url"
    if not url and image:
        from .decode import decode_image
        decoded = decode_image(image, repair=True)
        candidates = [r["text"] for r in decoded.get("results", [])]
        urls = [t for t in candidates
                if t.lower().startswith(("http://", "https://"))]
        if not urls:
            raise ToolError("no URL found in the QR image",
                            {"decoded": candidates})
        url = urls[0]
        source = "image"
    if not url:
        raise ToolError("provide url or image")

    report = check_url(url, allowlist=allowlist, allow_private=allow_private)
    parsed = parse_content(report["url"])
    payload = {
        "ok": True,
        "opened": False,
        "source": source,
        "url": report["url"],
        "host": report["host"],
        "scheme": report["scheme"],
        "private": report["private"],
        "allowlist": report["allowlist"] or get_allowlist(),
        "risk_flags": parsed.get("risk_flags", []),
        "risk_level": parsed.get("risk_level", "low"),
        "query": parsed.get("url", {}).get("query", {}),
    }
    if not confirm:
        payload["message"] = "validation only; pass confirm=true after the user has reviewed the URL"
        return payload
    if dry_run:
        payload["message"] = "dry_run=true; browser was not launched"
        return payload
    try:
        launched = webbrowser.open(report["url"], new=2 if new_tab else 0)
    except Exception as exc:
        raise ToolError("browser launch failed", {"detail": str(exc)})
    payload["opened"] = bool(launched)
    payload["method"] = "webbrowser.open"
    payload["message"] = ("browser launched" if launched
                          else "browser reported failure; the URL is still returned above")
    return payload
