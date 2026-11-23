# -*- coding: utf-8 -*-
"""Portable smoke test used by CI (Linux + Windows, Python 3.10-3.13).

Only core dependencies are required: numpy, Pillow, zxing-cpp, segno,
reedsolo. OpenCV / WinRT / RapidOCR / Tesseract are all optional here.

    python tests/ci_smoke.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qrmcp.app import Application  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}"
          + (f"  {detail}" if detail else ""))


def call(app: Application, name: str, args: dict) -> dict:
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": name, "arguments": args}}
    reply = app.process(json.dumps(msg, ensure_ascii=False))
    assert reply, f"{name} returned no reply"
    payload = json.loads(reply)
    assert "error" not in payload, f"{name}: {payload['error']}"
    return payload["result"]["structuredContent"]


def main() -> int:
    app = Application()

    print("protocol")
    for version in ("2025-06-18", "2025-03-26", "2024-11-05"):
        msg = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
               "params": {"protocolVersion": version, "capabilities": {},
                          "clientInfo": {"name": "ci-smoke", "version": "1"}}}
        res = json.loads(app.process(json.dumps(msg)))["result"]
        check(f"initialize {version}", res.get("protocolVersion") == version,
              str(res.get("protocolVersion")))
    tools = json.loads(app.process(json.dumps(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})))["result"]["tools"]
    names = {t["name"] for t in tools}
    check("tools/list exposes 9 tools", len(names) == 9, str(len(names)))
    check("ping", json.loads(app.process(json.dumps(
        {"jsonrpc": "2.0", "id": 3, "method": "ping"})))["result"] == {})

    print("core round-trip")
    text = "https://example.com/qrmcp-ci?sn=12345678901234567890"
    gen = call(app, "qr_generate", {"text": text, "format": "png",
                                    "scale": 8, "save": False})
    check("qr_generate", bool(gen.get("ok")) and bool(gen.get("data_uri")))
    uri = gen.get("data_uri", "")
    dec = call(app, "qr_decode", {"image": uri, "repair": False})
    got = [r.get("text") for r in dec.get("results", [])]
    check("generate -> decode round-trip", text in got, str(got[:2]))
    det = call(app, "qr_detect", {"image": uri})
    check("qr_detect", bool(det.get("ok")), f"count={det.get('count')}")

    print("parse + safety")
    parsed = call(app, "qr_parse", {"text": text + "&eC=99"})
    check("qr_parse", bool(parsed.get("ok")) and "sn" in json.dumps(parsed))
    preview = call(app, "qr_open", {"url": text})
    check("qr_open requires confirm", preview.get("opened") is False)
    blocked = call(app, "qr_open", {"url": "javascript:alert(1)", "confirm": True})
    check("qr_open blocks javascript:", not blocked.get("ok"))
    private = call(app, "qr_open", {"url": "http://127.0.0.1/", "confirm": True})
    check("qr_open blocks loopback", not private.get("ok"))

    print("repair hint verification")
    # max_attempts has a schema minimum of 50
    rep = call(app, "qr_repair", {"image": uri, "hint": text,
                                  "save_clean": False, "max_attempts": 50})
    check("qr_repair hint accepted", rep.get("text") == text,
          str(rep.get("hint_verification"))[:90])

    print("health + stdio process")
    health = call(app, "server_health", {})
    check("server_health", bool(health.get("ok")),
          f"tools={health.get('tool_count')}")
    env = dict(os.environ, PYTHONIOENCODING="utf-8",
               PYTHONPATH=str(ROOT / "src"))
    proc = subprocess.Popen(
        [sys.executable, "-m", "qrmcp", "--transport", "stdio"],
        cwd=str(ROOT), env=env, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        for msg in (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "ci-smoke", "version": "1"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ):
            proc.stdin.write(json.dumps(msg) + "\n")
            proc.stdin.flush()
            line = proc.stdout.readline()
            ok = bool(line.strip()) and "jsonrpc" in json.loads(line)
            check("stdio single-line JSON reply", ok)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("failed:", ", ".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
