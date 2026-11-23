# -*- coding: utf-8 -*-
"""Legacy SSE transport (MCP 2024-11-05).

GET  /sse                      -> long-lived SSE stream, first event is `endpoint`
POST /messages?sessionId=<id>  -> JSON-RPC request, reply is pushed over SSE
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

LOG = logging.getLogger("qrmcp.sse")


def _compact(text: str) -> str:
    try:
        return json.dumps(json.loads(text), ensure_ascii=False, separators=(",", ":"))
    except (ValueError, TypeError):
        return text.replace("\n", "")


class _Session:
    def __init__(self, sid: str):
        self.sid = sid
        self.queue: queue.Queue[str | None] = queue.Queue()
        self.alive = True


class _State:
    def __init__(self, app, token):
        self.app = app
        self.token = token
        self.sessions: dict[str, _Session] = {}
        self.lock = threading.Lock()


def _handler(state: _State):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "qrcode-mcp"

        def log_message(self, fmt, *args):
            LOG.debug("%s - %s", self.address_string(), fmt % args)

        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, Accept")
            self.send_header("Cache-Control", "no-cache")

        def _auth(self) -> bool:
            if not state.token:
                return True
            return (self.headers.get("Authorization") or "") == "Bearer " + state.token

        def _send(self, code: int, body: bytes = b"",
                  ctype: str = "application/json", extra: dict | None = None):
            self.send_response(code)
            if body:
                self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _chunk(self, data: bytes):
            """Write one HTTP/1.1 chunk for the SSE stream."""
            self.wfile.write(("%X\r\n" % len(data)).encode("ascii"))
            self.wfile.write(data + b"\r\n")
            self.wfile.flush()

        def do_OPTIONS(self):
            self._send(204)

        def do_GET(self):
            if not self._auth():
                self.send_response(401)
                self.send_header("Content-Length", "0")
                self._cors()
                self.end_headers()
                return
            if urlsplit(self.path).path.rstrip("/") != "/sse":
                self._send(404, b'{"error":"not found"}')
                return

            sid = uuid.uuid4().hex
            session = _Session(sid)
            with state.lock:
                state.sessions[sid] = session
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self._cors()
            self.end_headers()
            try:
                endpoint = "/messages?sessionId=" + sid
                self._chunk(("event: endpoint\ndata: " + endpoint + "\n\n").encode("utf-8"))
                while session.alive:
                    try:
                        item = session.queue.get(timeout=15)
                    except queue.Empty:
                        self._chunk(b": keepalive\n\n")
                        continue
                    if item is None:
                        break
                    self._chunk(("event: message\ndata: " + item + "\n\n").encode("utf-8"))
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                session.alive = False
                with state.lock:
                    state.sessions.pop(sid, None)

        def do_POST(self):
            if not self._auth():
                self.send_response(401)
                self.send_header("Content-Length", "0")
                self._cors()
                self.end_headers()
                return
            parts = urlsplit(self.path)
            if parts.path.rstrip("/") != "/messages":
                self._send(404, b'{"error":"not found"}')
                return
            sid = (parse_qs(parts.query).get("sessionId") or [""])[0]
            with state.lock:
                session = state.sessions.get(sid)
            if session is None:
                self._send(404, b'{"error":"unknown session"}')
                return
            try:
                n = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                n = 0
            if n <= 0 or n > 8 * 1024 * 1024:
                self._send(400, b'{"error":"invalid Content-Length"}')
                return
            body = self.rfile.read(n)
            reply = state.app.process(body)
            if reply is not None:
                session.queue.put(_compact(reply))
            self._send(202)

    return Handler


def run_sse(app, *, host: str = "127.0.0.1", port: int = 8765,
            token: str | None = None) -> int:
    state = _State(app, token)
    server = ThreadingHTTPServer((host, port), _handler(state))
    server.daemon_threads = True
    LOG.warning("Legacy SSE listening on http://%s:%s/sse (token=%s)",
                host, port, "set" if token else "off")
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
