"""Extraction rules. Every rule is plain JSON and is applied to the document text.

after    {"type": "after", "label": "Order number:", "until": " (", "line_start": true, "occurrence": 2}
         The text following `label` on the Nth line that contains it (line_start: the
         line must begin with the label), up to `until` or the end of the line.
         With "line": "Total charged:" the rule looks only at lines starting with that
         text and takes the value after the first `label` that follows it.
below    {"type": "below", "anchor": "Ship to:", "offset": 1, "lines": 1 | "block", "until": " "}
         Counting only non-blank lines, the line `offset` lines under the first line that
         starts with `anchor`. lines > 1 takes that many non-blank lines; "block" takes
         lines until the next blank line; both give a list. `until` (single line only)
         cuts the value short.
between  {"type": "between", "start": "Your items", "end": "Subtotal"}
         Every non-blank line strictly between a line starting with `start` and the next
         line starting with `end`. The value is a list.
regex    {"type": "regex", "pattern": "Order #(\\w+)", "group": 1, "flags": "i"}
         Escape hatch: first match of a Python regular expression.

Matching is case-sensitive and ignores leading whitespace on a line. Every rule
either produces a value with its exact position in the text or a reason why not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union

RULE_TYPES = ("after", "below", "between", "regex")

Value = Union[str, List[str]]


@dataclass
class Hit:
    value: Value
    start: int  # character offsets into the document text
    end: int


class RuleError(ValueError):
    """The rule itself is malformed (as opposed to not matching an email)."""


class Miss(Exception):
    """The rule is fine but this email doesn't contain what it describes."""


def _line_offsets(text: str) -> List[Tuple[int, str]]:
    out, pos = [], 0
    for line in text.split("\n"):
        out.append((pos, line))
        pos += len(line) + 1
    return out


def _require(rule: Dict[str, Any], key: str, kind=str) -> Any:
    if key not in rule:
        raise RuleError(f"{rule.get('type')} rule needs {key!r}")
    value = rule[key]
    if not isinstance(value, kind) or (kind is str and not value):
        raise RuleError(f"{rule.get('type')} rule: {key!r} must be a non-empty {kind.__name__}")
    return value


def _occurrence(rule: Dict[str, Any]) -> int:
    n = rule.get("occurrence", 1)
    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        raise RuleError("'occurrence' must be an integer >= 1")
    return n


def validate(rule: Any) -> None:
    if not isinstance(rule, dict):
        raise RuleError("a rule must be a JSON object")
    kind = rule.get("type")
    if kind not in RULE_TYPES:
        raise RuleError(f"unknown rule type {kind!r} (expected one of {', '.join(RULE_TYPES)})")
    if kind == "after":
        _require(rule, "label")
        _occurrence(rule)
        if "line" in rule:
            _require(rule, "line")
            if rule.get("line_start"):
                raise RuleError("use either 'line' or 'line_start', not both")
    elif kind == "below":
        _require(rule, "anchor")
        _occurrence(rule)
        offset = rule.get("offset", 1)
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 1:
            raise RuleError("'offset' must be an integer >= 1")
        lines = rule.get("lines", 1)
        if lines != "block" and (not isinstance(lines, int) or isinstance(lines, bool) or lines < 1):
            raise RuleError("'lines' must be an integer >= 1 or \"block\"")
        if "until" in rule and lines != 1:
            raise RuleError("'until' only works with a single line")
    elif kind == "between":
        _require(rule, "start")
        _require(rule, "end")
        _occurrence(rule)
    elif kind == "regex":
        pattern = _require(rule, "pattern")
        try:
            compiled = re.compile(pattern, _flags(rule.get("flags", "")))
        except re.error as exc:
            raise RuleError(f"bad regex: {exc}") from None
        group = rule.get("group", 0)
        if not isinstance(group, (int, str)) or isinstance(group, bool):
            raise RuleError("'group' must be a group number or name")
        if isinstance(group, int) and group > compiled.groups:
            raise RuleError(f"regex has {compiled.groups} group(s), group {group} requested")
        if isinstance(group, str) and group not in compiled.groupindex:
            raise RuleError(f"regex has no group named {group!r}")
    if "until" in rule and (not isinstance(rule["until"], str) or not rule["until"]):
        raise RuleError("'until' must be a non-empty string")


def _flags(spec: str) -> int:
    flags = 0
    for ch in spec:
        if ch == "i":
            flags |= re.IGNORECASE
        elif ch == "m":
            flags |= re.MULTILINE
        elif ch == "s":
            flags |= re.DOTALL
        else:
            raise RuleError(f"unknown regex flag {ch!r} (use i, m, s)")
    return flags


def _cut(line: str, start: int, until: Optional[str]) -> Tuple[int, int]:
    """Span of the value inside `line` beginning at `start`, trimmed of spaces."""
    end = len(line)
    if until is not None:
        idx = line.find(until, start)
        if idx < 0:
            raise Miss(f"{until!r} not found after the value")
        end = idx
    while start < end and line[start] == " ":
        start += 1
    while end > start and line[end - 1] == " ":
        end -= 1
    if start == end:
        raise Miss("value is empty")
    return start, end


def _starts(line: str, prefix: str) -> bool:
    return line.lstrip(" ").startswith(prefix)


def apply(rule: Dict[str, Any], text: str) -> Hit:
    """Apply one rule. Raises Miss (with a human reason) when the email doesn't fit."""
    validate(rule)
    kind = rule["type"]
    lines = _line_offsets(text)

    if kind == "after":
        label, n = rule["label"], _occurrence(rule)
        prefix = rule.get("line")
        if prefix is not None:
            found = [(pos, line) for pos, line in lines if _starts(line, prefix)]
            what = repr(prefix)
            where = "starting with"
        elif rule.get("line_start"):
            found = [(pos, line) for pos, line in lines if _starts(line, label)]
            what, where = repr(label), "starting with"
        else:
            found = [(pos, line) for pos, line in lines if label in line]
            what, where = repr(label), "containing"
        if len(found) < n:
            if not found:
                raise Miss(f"no line {where} {what}")
            raise Miss(f"only {len(found)} line(s) {where} {what}, occurrence {n} wanted")
        pos, line = found[n - 1]
        base = 0 if prefix is None else (len(line) - len(line.lstrip(" "))) + len(prefix)
        at = line.find(label, base)
        if at < 0:
            raise Miss(f"{label!r} not found on the line starting with {prefix!r}")
        at += len(label)
        s, e = _cut(line, at, rule.get("until"))
        return Hit(line[s:e], pos + s, pos + e)

    if kind == "below":
        anchor, n = rule["anchor"], _occurrence(rule)
        idx = [i for i, (_, line) in enumerate(lines) if _starts(line, anchor)]
        if len(idx) < n:
            raise Miss(f"no line starting with {anchor!r}" if not idx else
                       f"only {len(idx)} line(s) starting with {anchor!r}, occurrence {n} wanted")
        i = idx[n - 1]
        offset, count = rule.get("offset", 1), rule.get("lines", 1)
        seen = 0
        j = i
        while seen < offset:
            j += 1
            if j >= len(lines):
                raise Miss(f"fewer than {offset} non-blank line(s) below {anchor!r}")
            if lines[j][1].strip():
                seen += 1
        if count == 1:
            pos, line = lines[j]
            s, e = _cut(line, 0, rule.get("until"))
            return Hit(line[s:e], pos + s, pos + e)
        picked = [j]
        k = j + 1
        while k < len(lines) and (count == "block" or len(picked) < count):
            if not lines[k][1].strip():
                if count == "block":
                    break
            else:
                picked.append(k)
            k += 1
        if count != "block" and len(picked) < count:
            raise Miss(f"only {len(picked)} of {count} lines below {anchor!r}")
        value = [lines[p][1].strip() for p in picked]
        first_pos, first = lines[picked[0]]
        last_pos, last = lines[picked[-1]]
        start = first_pos + (len(first) - len(first.lstrip(" ")))
        return Hit(value, start, last_pos + len(last.rstrip(" ")))

    if kind == "between":
        start_label, end_label, n = rule["start"], rule["end"], _occurrence(rule)
        starts = [i for i, (_, line) in enumerate(lines) if _starts(line, start_label)]
        if len(starts) < n:
            raise Miss(f"no line starting with {start_label!r}" if not starts else
                       f"only {len(starts)} line(s) starting with {start_label!r}, occurrence {n} wanted")
        i = starts[n - 1]
        for j in range(i + 1, len(lines)):
            if _starts(lines[j][1], end_label):
                break
        else:
            raise Miss(f"no line starting with {end_label!r} after {start_label!r}")
        picked = [k for k in range(i + 1, j) if lines[k][1].strip()]
        if not picked:
            raise Miss(f"nothing between {start_label!r} and {end_label!r}")
        first_pos, first = lines[picked[0]]
        last_pos, last = lines[picked[-1]]
        start = first_pos + (len(first) - len(first.lstrip(" ")))
        return Hit([lines[k][1].strip() for k in picked], start, last_pos + len(last.rstrip(" ")))

    # regex
    m = re.compile(rule["pattern"], _flags(rule.get("flags", ""))).search(text)
    if not m:
        raise Miss("regex did not match")
    group = rule.get("group", 0)
    if m.group(group) is None:
        raise Miss(f"regex group {group!r} did not participate in the match")
    s, e = m.span(group)
    value = m.group(group).strip()
    if not value:
        raise Miss("value is empty")
    lead = len(m.group(group)) - len(m.group(group).lstrip())
    return Hit(value, s + lead, s + lead + len(value))
