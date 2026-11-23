# -*- coding: utf-8 -*-
"""Importing this package registers every MCP tool."""

from . import batch, decode, detect, generate, health, ocr, opener, parse, repair  # noqa: F401

__all__ = ["batch", "decode", "detect", "generate", "health",
           "ocr", "opener", "parse", "repair"]
