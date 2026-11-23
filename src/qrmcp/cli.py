# -*- coding: utf-8 -*-
"""Command line entry point for all three MCP transports."""
from __future__ import annotations

import argparse
import logging
import os
import sys

from . import __version__
from .app import Application
from .security import env_int


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="qrmcp",
        description="qrcode-mcp: QR decode/repair/generate + OCR over MCP",
    )
    p.add_argument("--transport", choices=("stdio", "http", "sse"),
                   default=os.environ.get("QRMCP_TRANSPORT", "stdio"),
                   help="stdio (default), http (Streamable HTTP), sse (legacy 2024-11-05 SSE)")
    p.add_argument("--host", default=os.environ.get("QRMCP_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=env_int("QRMCP_PORT", 8765))
    p.add_argument("--path", default=os.environ.get("QRMCP_PATH", "/mcp"))
    p.add_argument("--token", default=os.environ.get("QRMCP_HTTP_TOKEN"),
                   help="optional bearer token for http/sse transports")
    p.add_argument("--log-level", default=os.environ.get("QRMCP_LOG_LEVEL", "WARNING"))
    p.add_argument("--version", action="version", version=f"qrcode-mcp {__version__}")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.WARNING),
        stream=sys.stderr,
        format="[qrmcp] %(levelname)s %(message)s",
    )
    app = Application()
    if args.transport == "stdio":
        from .transports.stdio import run_stdio
        return run_stdio(app)
    if args.transport == "http":
        from .transports.http import run_http
        return run_http(app, host=args.host, port=args.port,
                        path=args.path, token=args.token)
    from .transports.sse import run_sse
    return run_sse(app, host=args.host, port=args.port, token=args.token)


if __name__ == "__main__":
    raise SystemExit(main())
