"""Security and complexity limits for governed semantic metrics."""

from __future__ import annotations


MAX_AST_DEPTH = 12
MAX_AST_NODES = 256
MAX_FUNCTION_ARGUMENTS = 16
MAX_BOOLEAN_OPERANDS = 16
MAX_IN_VALUES = 100
MAX_LITERAL_STRING_LENGTH = 1_024


__all__ = [
    "MAX_AST_DEPTH",
    "MAX_AST_NODES",
    "MAX_BOOLEAN_OPERANDS",
    "MAX_FUNCTION_ARGUMENTS",
    "MAX_IN_VALUES",
    "MAX_LITERAL_STRING_LENGTH",
]
