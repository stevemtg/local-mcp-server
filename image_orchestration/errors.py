"""Typed orchestration failure carrying a machine-readable code."""
from __future__ import annotations


class Fault(RuntimeError):
    """A failure whose ``code`` is surfaced to the host in the tool result."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
