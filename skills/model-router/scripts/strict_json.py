"""Shared strict JSON input checks; callers own size limits and error statuses."""
from __future__ import annotations

import json
import math


def ensure_json_value(value) -> None:
    """Reject non-JSON Python values, nonfinite numbers, cycles and bad Unicode."""
    active = set()

    def visit(node):
        if node is None or type(node) in (bool, int):
            return
        if type(node) is float:
            if not math.isfinite(node):
                raise ValueError("nonfinite JSON number")
            return
        if isinstance(node, str):
            node.encode("utf-8")
            return
        if not isinstance(node, (dict, list)):
            raise ValueError(f"unsupported JSON value type: {type(node).__name__}")
        if id(node) in active:
            raise ValueError("cyclic JSON value")
        active.add(id(node))
        if isinstance(node, dict):
            for key, child in node.items():
                if not isinstance(key, str):
                    raise ValueError("JSON object keys must be strings")
                visit(key)
                visit(child)
        else:
            for child in node:
                visit(child)
        active.remove(id(node))

    try:
        visit(value)
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("invalid JSON Unicode or nesting") from exc


def loads(raw):
    """Decode without duplicate keys or nonfinite constants/overflowed floats."""
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key {key!r}")
            result[key] = value
        return result

    def constant(value):
        raise ValueError(f"nonfinite JSON constant: {value}")

    try:
        value = json.loads(raw, object_pairs_hook=object_pairs, parse_constant=constant)
        ensure_json_value(value)  # also rejects exponent overflow such as 1e999
        return value
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("invalid JSON encoding or nesting") from exc
