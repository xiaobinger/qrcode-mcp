# Contributing

Thanks for taking the time. The bar is low and the useful contributions are
concrete.

## Development setup

```bash
git clone https://github.com/xiaobinger/qrcode-mcp
cd qrcode-mcp
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e '.[full,dev]'
PYTHONPATH=src python tests/selftest.py           # full end-to-end, 38 checks
python tests/ci_smoke.py                          # portable core-only smoke, 16 checks
python -m pip wheel . --no-deps -w dist           # packaging check
```

Core-only development (`pip install -e .`) is enough for the protocol layer,
QR decode / repair / generate and the safety layer. OpenCV and the OCR backends
are optional and must degrade gracefully when absent.

## Layout

```
src/qrmcp/
  protocol.py          JSON-RPC 2.0 + MCP method dispatch
  app.py / registry.py tool registration and response framing
  security.py          URL policy, output path sandbox, resource limits
  transports/          stdio · Streamable HTTP · legacy SSE
  tools/
    qrspec.py          QR spec tables, masks, format info, RS, segments
    repair.py          damaged-code recovery pipeline
    decode.py          layered decoding
    generate.py detect.py parse.py opener.py ocr.py batch.py health.py
assets/                README visual assets (regenerate with build_assets.py)
tests/                 selftest.py (full) · ci_smoke.py (portable)
```

## Adding a tool

```python
from ..registry import tool

@tool(
    "my_tool",
    "One-line description the model will read.",
    {
        "image": {"type": "string", "description": "Local path, base64 or data URI"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10},
    },
    ("image",),                       # required argument names
)
def my_tool(image: str, limit: int = 10) -> dict:
    return {"ok": True}
```

Import the module from `tools/__init__.py`. Return JSON-serialisable dictionaries,
never raise raw exceptions — use `ToolError` from `errors.py`. Add one check to
`tests/ci_smoke.py` if the tool works with core dependencies only, otherwise to
`tests/selftest.py`.

## The contribution we need most

A QR image that currently fails. Open an issue with:

- the original image (synthetic samples preferred — no real serial numbers),
- the expected payload,
- which tool failed and what it returned,
- optionally: EC level / version / mask if you know them.

Damaged samples let us tune grid fitting and erasure search without guessing.

## Before opening a PR

- [ ] `python tests/ci_smoke.py` passes on core dependencies only
- [ ] `PYTHONPATH=src python tests/selftest.py` passes with extras installed
- [ ] No new hard dependency in the core install
- [ ] New behaviour is documented in `README.md` (and `README.zh-CN.md` if user-facing)
- [ ] No credentials, real device identifiers or customer data in the diff
- [ ] Visual assets regenerated via `python assets/build_assets.py` if the logo changed

## Code style

- Standard library for the protocol layer; keep optional dependencies optional.
- Tool responses are plain JSON-serialisable dictionaries. Log to **stderr**
  only — stdout is the JSON-RPC channel in stdio mode.
- Keep `README.md` and `README.zh-CN.md` in sync for user-facing changes.
- Never commit generated images, caches or local diagnostics; `.gitignore`
  already covers them.

By contributing you agree that your work is licensed under MIT.
