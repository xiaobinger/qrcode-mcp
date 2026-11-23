# -*- coding: utf-8 -*-
"""End-to-end self test for qrcode-mcp.

Run from the package root:
    PYTHONPATH=src python tests/selftest.py
"""
from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))
os.environ.setdefault("QRMCP_OUTPUT_DIR", str(ROOT / "out"))

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("  | " + str(detail)[:160]) if detail else ""))


def call(app, name: str, args: dict | None = None) -> dict:
    args = args or {}
    raw = app.process(json.dumps({
        "jsonrpc": "2.0", "id": 99, "method": "tools/call",
        "params": {"name": name, "arguments": args},
    }, ensure_ascii=False))
    return json.loads(raw)["result"]["structuredContent"]


def make_images(tmp: Path):
    from PIL import Image, ImageDraw, ImageFont
    import segno
    qr_path = tmp / "code.png"
    q = segno.make("https://example.com/qrmcp-selftest", error="m")
    q.save(str(qr_path), scale=8, border=4)
    ocr_path = tmp / "text.png"
    im = Image.new("RGB", (520, 120), "white")
    d = ImageDraw.Draw(im)
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 40)
    except Exception:
        font = ImageFont.load_default()
    d.text((16, 24), "QR MCP TEST 2026", fill="black", font=font)
    im.save(ocr_path)
    return str(qr_path), str(ocr_path)


def test_protocol(app):
    print("\n[1] MCP protocol")
    for ver in ("2025-06-18", "2025-03-26", "2024-11-05"):
        r = json.loads(app.process(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": ver, "capabilities": {},
                       "clientInfo": {"name": "selftest", "version": "1"}}})))
        check("initialize " + ver, r["result"]["protocolVersion"] == ver)
    check("notification has no reply",
          app.process('{"jsonrpc":"2.0","method":"notifications/initialized"}') is None)
    check("ping", json.loads(app.process('{"jsonrpc":"2.0","id":2,"method":"ping"}'))["result"] == {})
    tools = json.loads(app.process('{"jsonrpc":"2.0","id":3,"method":"tools/list"}'))["result"]["tools"]
    check("tools/list has 9 tools", len(tools) == 9, [t["name"] for t in tools])
    check("tool schemas valid", all("inputSchema" in t and "description" in t for t in tools))
    err = json.loads(app.process('{"jsonrpc":"2.0","id":4,"method":"nope"}'))["error"]["code"]
    check("unknown method -> -32601", err == -32601)
    bad = json.loads(app.process(json.dumps({
        "jsonrpc": "2.0", "id": 5, "method": "tools/call",
        "params": {"name": "qr_parse", "arguments": {}}})))["result"]
    check("missing argument -> isError", bad.get("isError") is True)
    batch = json.loads(app.process('[{"jsonrpc":"2.0","id":6,"method":"ping"},'
                                   '{"jsonrpc":"2.0","method":"notifications/initialized"}]'))
    check("batch support", isinstance(batch, list) and len(batch) == 1)


def test_tools(app, qr_path, ocr_path):
    print("\n[2] Tools")
    g = call(app, "qr_generate", {"text": "https://example.com/roundtrip",
                                  "format": "png", "save": False, "scale": 8})
    check("qr_generate", g.get("ok") is True and "data_uri" in g)
    d = call(app, "qr_decode", {"image": g["data_uri"], "repair": False})
    check("generate -> decode roundtrip",
          d.get("results", [{}])[0].get("text") == "https://example.com/roundtrip",
          d.get("warnings"))
    d2 = call(app, "qr_decode", {"image": qr_path})
    check("decode file path", d2.get("count", 0) >= 1)
    p = call(app, "qr_parse", {"text": "https://nqr.example.com?sn=123456&eC=99"})
    check("parse URL + query", p.get("type") == "url" and
          p.get("url", {}).get("query", {}).get("eC") == "99")
    risky = call(app, "qr_parse", {"text": "javascript:alert(1)"})
    check("parse flags dangerous scheme", risky.get("risk_level") == "high")
    o = call(app, "qr_open", {"url": "https://example.com/"})
    check("open without confirm is inert", o.get("opened") is False)
    o = call(app, "qr_open", {"url": "javascript:alert(1)", "confirm": True})
    check("blocks javascript scheme", o.get("ok") is False)
    o = call(app, "qr_open", {"url": "http://127.0.0.1/", "confirm": True})
    check("blocks private host (SSRF)", o.get("ok") is False)
    det = call(app, "qr_detect", {"image": qr_path})
    check("qr_detect locates symbol", det.get("count", 0) >= 1, det.get("boxes"))
    ocr = call(app, "ocr_image", {"image": ocr_path, "backend": "auto"})
    check("ocr auto backend", ocr.get("ok") is True and "QR" in ocr.get("text", "").upper(),
          (ocr.get("backend"), ocr.get("text"), ocr.get("error")))
    health = call(app, "server_health")
    check("server_health lists backends", health.get("tool_count") == 9 and
          "windows" in health.get("ocr_backends", {}))
    batch = call(app, "qr_batch", {"sources": [qr_path, ocr_path], "include_ocr": False})
    check("qr_batch", batch.get("files") == 2 and batch.get("total_codes") >= 1)


def _wait_port(port: int, proc, timeout: float = 12.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if proc.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), 0.25):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _spawn(transport: str, port: int):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.Popen(
        [sys.executable, "-m", "qrmcp", "--transport", transport,
         "--port", str(port), "--token", "selftest-token"],
        cwd=str(ROOT), env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _post(url: str, payload: dict, token: str | None = None):
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, dict(resp.headers), resp.read()


def test_stdio():
    print("\n[3] stdio transport")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.Popen([sys.executable, "-m", "qrmcp", "--transport", "stdio"],
                         cwd=str(ROOT), env=env, stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for rid, msg in enumerate([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                        "clientInfo": {"name": "selftest", "version": "1"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "qr_parse", "arguments": {"text": "hello"}}},
        ], start=1):
            p.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
            p.stdin.flush()
            line = p.stdout.readline()
            check("stdio reply #%d" % rid, bool(line) and
                  json.loads(line.decode("utf-8"))["id"] == rid)
    finally:
        p.terminate()
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill()


def test_http():
    print("\n[4] Streamable HTTP transport")
    port = 18765
    proc = _spawn("http", port)
    try:
        check("http server started", _wait_port(port, proc))
        status, headers, body = _post(
            "http://127.0.0.1:%d/mcp" % port,
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "selftest", "version": "1"}}},
            "selftest-token")
        check("POST initialize 200", status == 200)
        check("Mcp-Session-Id returned",
              any(k.lower() == "mcp-session-id" for k in headers))
        check("initialize result", json.loads(body)["result"]["serverInfo"]["name"] == "qrcode-mcp")
        status, _, body = _post(
            "http://127.0.0.1:%d/mcp" % port,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "qr_parse", "arguments": {"text": "https://example.com"}}},
            "selftest-token")
        check("POST tools/call", json.loads(body)["result"]["structuredContent"]["ok"] is True)
        try:
            get_req = urllib.request.Request(
                "http://127.0.0.1:%d/mcp" % port,
                headers={"Authorization": "Bearer selftest-token"})
            urllib.request.urlopen(get_req, timeout=5)
            ok = False
        except urllib.error.HTTPError as e:
            ok = e.code == 405
        check("GET /mcp -> 405 (no server stream)", ok)
        req = urllib.request.Request("http://127.0.0.1:%d/mcp" % port,
                                     method="DELETE",
                                     headers={"Authorization": "Bearer selftest-token"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            check("DELETE /mcp -> 204", resp.status == 204)
        try:
            _post("http://127.0.0.1:%d/mcp" % port,
                  {"jsonrpc": "2.0", "id": 3, "method": "ping"}, "wrong-token")
            check("token enforced", False)
        except urllib.error.HTTPError as e:
            check("token enforced", e.code == 401)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_sse():
    print("\n[5] legacy SSE transport")
    import http.client
    port = 18766
    proc = _spawn("sse", port)
    try:
        check("sse server started", _wait_port(port, proc))
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        conn.request("GET", "/sse", headers={"Authorization": "Bearer selftest-token"})
        resp = conn.getresponse()
        check("GET /sse is event-stream",
              "text/event-stream" in (resp.getheader("Content-Type") or ""))
        buf = b""
        endpoint = None
        while b"\n\n" not in buf and len(buf) < 4096:
            chunk = resp.read(1)
            if not chunk:
                break
            buf += chunk
        text = buf.decode("utf-8", errors="replace")
        for line in text.splitlines():
            if line.startswith("data: ") and "sessionId=" in line:
                endpoint = line[6:].strip()
        check("endpoint event received", endpoint is not None, endpoint)
        if endpoint:
            payload = json.dumps({
                "jsonrpc": "2.0", "id": 7, "method": "tools/call",
                "params": {"name": "qr_parse", "arguments": {"text": "sse-ok"}}}
            ).encode("utf-8")
            c2 = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            c2.request("POST", endpoint, body=payload,
                       headers={"Content-Type": "application/json",
                                "Authorization": "Bearer selftest-token"})
            r2 = c2.getresponse()
            check("POST /messages -> 202", r2.status == 202)
            r2.read()
            c2.close()
            # read the pushed response frame from the SSE connection
            frame = b""
            deadline = time.time() + 8
            while b"\n\n" not in frame and time.time() < deadline:
                chunk = resp.read(1)
                if not chunk:
                    break
                frame += chunk
            check("SSE pushed tools/call result", b'"id":7' in frame or b'"id": 7' in frame,
                  frame[:120])
        conn.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> int:
    from qrmcp.app import Application
    app = Application()
    with tempfile.TemporaryDirectory(prefix="qrmcp-test-") as td:
        qr_path, ocr_path = make_images(Path(td))
        test_protocol(app)
        test_tools(app, qr_path, ocr_path)
    test_stdio()
    test_http()
    test_sse()
    print("\n========== %d passed, %d failed ==========" % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  FAILED:", f)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
