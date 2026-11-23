# -*- coding: utf-8 -*-
"""Error types and JSON-RPC error codes."""

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class QRMCPError(Exception):
    """Base class for expected, reportable failures."""

    code = INTERNAL_ERROR

    def __init__(self, message, data=None):
        super().__init__(message)
        self.message = message
        self.data = data

    def as_dict(self):
        out = {"code": self.code, "message": self.message}
        if self.data is not None:
            out["data"] = self.data
        return out


class ToolError(QRMCPError):
    """A tool failed. Reported as an MCP tool result with isError=true."""

    code = INVALID_PARAMS


class ParamError(ToolError):
    """Invalid tool arguments."""

    def __init__(self, message, data=None):
        super().__init__(message, data)
        self.code = INVALID_PARAMS


class UnsupportedError(ToolError):
    """The requested operation is outside the supported scope."""

    def __init__(self, message, data=None):
        super().__init__(message, data)
        self.code = INVALID_PARAMS


class SecurityError(ToolError):
    """Refused by the safety policy."""

    def __init__(self, message, data=None):
        super().__init__(message, data)
        self.code = INVALID_PARAMS
