# -*- coding: utf-8 -*-
"""Transport adapters: stdio, Streamable HTTP, legacy SSE."""

__all__ = ["run_stdio", "run_http", "run_sse"]


def run_stdio(*args, **kwargs):
    from .stdio import run_stdio as _run
    return _run(*args, **kwargs)


def run_http(*args, **kwargs):
    from .http import run_http as _run
    return _run(*args, **kwargs)


def run_sse(*args, **kwargs):
    from .sse import run_sse as _run
    return _run(*args, **kwargs)
