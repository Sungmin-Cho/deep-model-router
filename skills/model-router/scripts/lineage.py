"""Model lineage templates and generation order (design DD-A1).

A registry row declares the id spelling its vendor uses across generations,
e.g. ``gpt-{gen}-sol`` or ``claude-haiku-{gen}[-{date}]``. This module is the
one parser for that grammar and the one ordering over what it extracts. It is
pure: no config, no files, no clock.

Grammar
  ``{gen}``      exactly once; matches ``\\d+(?:[.-]\\d+)*`` and becomes a tuple
                 of integers split on ``.``/``-``
  ``[-{date}]``  optional, only at the very end; an 8-digit ``-YYYYMMDD``
                 suffix, stripped only when what remains still matches
  anything else  a literal (regex-escaped); any other ``{…}``, ``[`` or ``]``
                 is a template error (``ValueError``)

Order: generations compare element-wise with the shorter side zero-padded
(``5 == 5.0``); on a tie an undated id sorts below any dated one, then dates
compare. Equal is NOT a successor.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

GEN_PATTERN = r"(\d+(?:[.-]\d+)*)"
DATE_SUFFIX = "[-{date}]"
_DATE_RE = re.compile(r"^(.*)-(\d{8})$")


@dataclass(frozen=True)
class Generation:
    parts: tuple[int, ...]
    date: int | None


@lru_cache(maxsize=256)
def _compile(template: str) -> tuple[re.Pattern, bool]:
    if not isinstance(template, str) or not template:
        raise ValueError("lineage template must be a non-empty string")
    dated = template.endswith(DATE_SUFFIX)
    body = template[: -len(DATE_SUFFIX)] if dated else template
    if body.count("{gen}") != 1:
        raise ValueError(f"lineage template {template!r} must contain {{gen}} exactly once")
    literal_parts = body.split("{gen}")
    for part in literal_parts:
        if any(ch in part for ch in "{}[]"):
            raise ValueError(
                f"lineage template {template!r}: only {{gen}} and a trailing "
                f"{DATE_SUFFIX} are placeholders")
    pattern = "^" + re.escape(literal_parts[0]) + GEN_PATTERN + re.escape(literal_parts[1]) + "$"
    return re.compile(pattern), dated


def validate_template(template: str) -> None:
    """Raise ValueError when the template does not follow the grammar."""
    _compile(template)


def parse(template: str, model_id: str) -> Generation | None:
    """The generation `model_id` names under `template`, or None when the id
    is not a spelling of this lineage. A malformed template raises."""
    regex, dated = _compile(template)
    if not isinstance(model_id, str):
        return None
    if dated:
        m = _DATE_RE.match(model_id)
        if m:
            g = regex.match(m.group(1))
            if g:
                return Generation(_split(g.group(1)), int(m.group(2)))
    g = regex.match(model_id)
    if g is None:
        return None
    return Generation(_split(g.group(1)), None)


def _split(gen: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.split(r"[.-]", gen))


def compare(a: Generation, b: Generation) -> int:
    """-1 / 0 / 1. Zero-padded generation first, then date with None lowest."""
    width = max(len(a.parts), len(b.parts))
    pa = a.parts + (0,) * (width - len(a.parts))
    pb = b.parts + (0,) * (width - len(b.parts))
    if pa != pb:
        return -1 if pa < pb else 1
    if a.date == b.date:
        return 0
    if a.date is None:
        return -1
    if b.date is None:
        return 1
    return -1 if a.date < b.date else 1


def is_successor(template: str, current: str, candidate: str) -> bool:
    """True only when both ids belong to the lineage and `candidate` is
    strictly newer. A tie (``gpt-6-sol`` vs ``gpt-6.0-sol``) is not."""
    cur, cand = parse(template, current), parse(template, candidate)
    if cur is None or cand is None:
        return False
    return compare(cand, cur) > 0
