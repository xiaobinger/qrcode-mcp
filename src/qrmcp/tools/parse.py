# -*- coding: utf-8 -*-
"""Interpret a decoded payload: URL, WiFi, vCard, phone, payment, text."""
from __future__ import annotations

import ipaddress
import json
import re
from urllib.parse import parse_qs, urlsplit

from ..registry import tool

_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
_SHORTENERS = {
    "t.cn", "url.cn", "dwz.cn", "suo.im", "bit.ly", "tinyurl.com",
    "goo.gl", "is.gd", "cutt.ly", "shorturl.at", "s.id", "mrw.so",
    "w.url.cn", "985.so", "u.nu", "rb.gy",
}
_SENSITIVE_KEYS = {"token", "key", "pwd", "password", "pass", "secret",
                   "auth", "sign", "signature", "access_token", "session"}


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _url_report(url: str) -> dict:
    p = urlsplit(url)
    host = p.hostname or ""
    query = parse_qs(p.query, keep_blank_values=True)
    flat = {k: v[0] if len(v) == 1 else v for k, v in query.items()}
    flags: list[str] = []
    if _is_ip(host):
        flags.append("ip-literal-host")
    if host.lower() in _SHORTENERS:
        flags.append("short-link")
    if "xn--" in host.lower():
        flags.append("punycode-host")
    if p.port and p.port not in (80, 443):
        flags.append("nonstandard-port")
    low_keys = {k.lower() for k in flat}
    if low_keys & _SENSITIVE_KEYS:
        flags.append("sensitive-query-key")
    for k, v in flat.items():
        val = v if isinstance(v, str) else " ".join(v)
        if k.lower() in {"url", "redirect", "target", "next", "return", "goto"} and _URL_RE.search(val):
            flags.append("nested-redirect")
    if host.count("-") >= 3:
        flags.append("hyphen-heavy-host")
    if p.username or p.password or "@" in p.netloc:
        flags.append("userinfo")
    level = "high" if {"ip-literal-host", "punycode-host", "userinfo", "nested-redirect"} & set(flags) else (
        "medium" if flags else "low")
    return {
        "url": url,
        "scheme": p.scheme,
        "host": host,
        "port": p.port,
        "path": p.path,
        "query": flat,
        "fragment": p.fragment,
        "risk_flags": flags,
        "risk_level": level,
    }


def parse_content(text: str) -> dict:
    """Return a structured interpretation of any decoded payload."""
    if text is None:
        text = ""
    s = str(text)
    t = s.strip()
    lower = t.lower()
    base = {"text": s, "length": len(s)}
    links = _URL_RE.findall(s)

    if lower.startswith(("http://", "https://")):
        return {**base, "type": "url", "url": _url_report(t), "links": links}
    if lower.startswith("wifi:"):
        fields = dict(re.findall(r"([A-Za-z]+):([^;]*);", t[5:]))
        out = {**base, "type": "wifi", "wifi": fields, "links": links}
        if "P" in fields:
            out["wifi"]["P"] = "***redacted***"
            out["redacted"] = True
        return out
    if lower.startswith("begin:vcard"):
        return {**base, "type": "vcard",
                "vcard": dict(re.findall(r"^([A-Za-z;:-]+):(.+)$", t, re.M)),
                "links": links}
    if lower.startswith("tel:") or re.fullmatch(r"\+?[\d\s()\-]{7,}", t):
        return {**base, "type": "phone", "phone": t.split(":", 1)[-1], "links": links}
    if lower.startswith(("mailto:", "email:")):
        return {**base, "type": "email", "email": t.split(":", 1)[-1], "links": links}
    if lower.startswith(("smsto:", "sms:")):
        return {**base, "type": "sms", "links": links}
    if lower.startswith("geo:"):
        return {**base, "type": "geo", "links": links}
    if re.search(r"(alipay|weixin|wxp://|https://qr\.alipay|pay\.qq\.com|unionpay|upay)", lower):
        return {**base, "type": "payment", "links": links,
                "risk_flags": ["payment-payload"], "risk_level": "high"}
    if lower.startswith(("javascript:", "file:", "data:", "vbscript:")):
        return {**base, "type": "dangerous-scheme", "risk_flags": ["executable-scheme"],
                "risk_level": "high", "links": links}
    if t.startswith(("{", "[")):
        try:
            return {**base, "type": "json", "json": json.loads(t), "links": links}
        except ValueError:
            pass
    if _URL_RE.fullmatch(t):
        return {**base, "type": "url", "url": _url_report(t), "links": links}
    if links:
        return {**base, "type": "text-with-links", "links": links}
    if re.fullmatch(r"[0-9]+", t):
        return {**base, "type": "numeric", "value": t, "links": []}
    return {**base, "type": "text", "links": links}


@tool(
    "qr_parse",
    "Interpret a decoded QR payload. Detects URL/WiFi/vCard/phone/email/payment/text, "
    "splits URL query parameters, extracts embedded links and flags phishing-style risks.",
    {"text": {"type": "string", "description": "Decoded payload text"}},
    ("text",),
)
def qr_parse(text: str) -> dict:
    return {"ok": True, **parse_content(text)}
