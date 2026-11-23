# -*- coding: utf-8 -*-
"""Safety policy: URL checking, output path sandboxing, size limits."""
from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlsplit, urlunsplit

from .errors import SecurityError

ALLOWED_SCHEMES = {"http", "https"}
DANGEROUS_SCHEMES = {
    "file", "javascript", "data", "vbscript", "about", "blob",
    "ftp", "gopher", "mailto", "tel", "smb", "ldap",
}


def env_flag(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default


MAX_IMAGE_BYTES = env_int("QRMCP_MAX_IMAGE_BYTES", 20 * 1024 * 1024)
MAX_IMAGE_PIXELS = env_int("QRMCP_MAX_IMAGE_PIXELS", 40_000_000)
MAX_GENERATED_CHARS = env_int("QRMCP_MAX_GENERATED_CHARS", 4096)


def output_dir() -> str:
    d = os.environ.get("QRMCP_OUTPUT_DIR") or os.path.join(os.getcwd(), "out")
    d = os.path.abspath(d)
    os.makedirs(d, exist_ok=True)
    return d


def resolve_output_path(name: str | None) -> str:
    """Resolve a user supplied file name inside the configured output directory.

    Absolute paths and ``..`` traversal are rejected by default so a model
    cannot be tricked into writing outside the sandbox.
    """
    base = output_dir()
    if not name:
        return os.path.join(base, "qrcode.png")
    name = str(name)
    if os.path.isabs(name) and not env_flag("QRMCP_ALLOW_ABSOLUTE_OUTPUT"):
        raise SecurityError(
            "absolute output paths are disabled",
            {"hint": "pass a plain file name, or set QRMCP_ALLOW_ABSOLUTE_OUTPUT=1"},
        )
    target = os.path.abspath(os.path.join(base, name))
    try:
        inside = os.path.commonpath([base, target]) == base
    except ValueError:
        inside = False
    if not inside:
        raise SecurityError(
            "output path escapes QRMCP_OUTPUT_DIR",
            {"base": base, "requested": name},
        )
    parent = os.path.dirname(target)
    os.makedirs(parent, exist_ok=True)
    return target


def get_allowlist() -> list[str]:
    raw = os.environ.get("QRMCP_URL_ALLOWLIST", "")
    return [x.strip().lower() for x in raw.split(",") if x.strip()]


def domain_allowed(host: str, patterns: list[str]) -> bool:
    if not patterns:
        return True
    h = host.lower().rstrip(".")
    for p in patterns:
        p = p.lower().rstrip(".")
        if p.startswith("*."):
            root = p[2:]
            if h == root or h.endswith("." + root):
                return True
        elif h == p:
            return True
    return False


def _private_ip(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except Exception:
        return True  # fail closed: unresolvable host is treated as unsafe
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return True
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return True
    return False


def check_url(url: str, *, allowlist: list[str] | None = None,
              allow_private: bool | None = None) -> dict:
    """Validate a URL before any browser interaction.

    Returns a report dict. Raises SecurityError for hard refusals.
    """
    if not isinstance(url, str) or not url.strip():
        raise SecurityError("empty url")
    url = url.strip()
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise SecurityError("malformed url", {"detail": str(exc)})

    scheme = (parts.scheme or "").lower()
    if scheme in DANGEROUS_SCHEMES:
        raise SecurityError(
            "scheme is not allowed",
            {"scheme": scheme, "hint": "only http/https can be opened"},
        )
    if scheme not in ALLOWED_SCHEMES:
        # A bare "example.com/path" is upgraded to https rather than rejected.
        if not scheme and not url.startswith("//"):
            url = "https://" + url
            parts = urlsplit(url)
        else:
            raise SecurityError("unsupported scheme", {"scheme": scheme})

    host = parts.hostname or ""
    if not host:
        raise SecurityError("url has no host")
    if "@" in parts.netloc:
        raise SecurityError("userinfo in url is not allowed")

    patterns = get_allowlist() if allowlist is None else allowlist
    if not domain_allowed(host, patterns):
        raise SecurityError(
            "host is not in QRMCP_URL_ALLOWLIST",
            {"host": host, "allowlist": patterns},
        )

    allow_priv = env_flag("QRMCP_ALLOW_PRIVATE") if allow_private is None else allow_private
    private = _private_ip(host)
    if private and not allow_priv:
        raise SecurityError(
            "host resolves to a private/loopback address (SSRF protection)",
            {"host": host, "hint": "set QRMCP_ALLOW_PRIVATE=1 only for trusted local targets"},
        )

    normalized = urlunsplit((parts.scheme, parts.netloc, parts.path,
                             parts.query, parts.fragment))
    return {
        "url": normalized,
        "host": host,
        "scheme": parts.scheme,
        "port": parts.port,
        "private": private,
        "allowlist": patterns,
        "risk": "private-host" if private else "public-host",
    }
