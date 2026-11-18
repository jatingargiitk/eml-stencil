"""Turn a highlighted span of an example email into a rule that finds it again.

The rule is anchored on the static text around the highlight (the label before it
on the same line, or a label line above it), never on the highlighted value itself,
so it keeps working when the value changes from one email to the next. Every
proposed rule is applied back to the example and only accepted if it reproduces
the highlight exactly; if nothing does, inference fails and says so.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterator, List, Optional, Tuple

from . import rules

Rule = Dict[str, Any]


class InferError(ValueError):
    pass


def span_of_text(text: str, needle: str, occurrence: int = 1) -> Tuple[int, int]:
    """Character span of the Nth occurrence of a literal string."""
    if not needle.strip():
        raise InferError("the text to select is empty")
    pos = -1
    for _ in range(occurrence):
        pos = text.find(needle, pos + 1)
        if pos < 0:
            count = text.count(needle)
            raise InferError(f"{needle!r} occurs {count} time(s) in the email, occurrence {occurrence} wanted")
    return pos, pos + len(needle)


def span_of_lines(text: str, first: int, last: int) -> Tuple[int, int]:
    """Character span covering 1-based lines first..last (as numbered by `show`)."""
    lines = text.split("\n")
    if not (1 <= first <= last <= len(lines)):
        raise InferError(f"line range {first}-{last} is outside 1-{len(lines)}")
    start = sum(len(l) + 1 for l in lines[: first - 1])
    end = sum(len(l) + 1 for l in lines[:last]) - 1
    return start, end


def _trim(text: str, start: int, end: int) -> Tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    if start >= end:
        raise InferError("the selection is empty")
    return start, end


def _has_letter(s: str) -> bool:
    return any(ch.isalpha() for ch in s)


def _static(s: str) -> bool:
    """Text that looks like a fixed label rather than data: has letters, no digits."""
    return _has_letter(s) and not any(ch.isdigit() for ch in s)


def _label_prefix(line: str, max_words: int = 6) -> Optional[str]:
    """The fixed-looking start of a line: its words up to the first one with a digit
    (at most `max_words` of them), e.g. "Discount (WELCOME10): -$11.70" -> "Discount"."""
    words = []
    for word in line.strip().split(" "):
        if any(ch.isdigit() for ch in word) or len(words) == max_words:
            break
        words.append(word)
    head = " ".join(words).rstrip(" $€£¥#(-").rstrip()
    if len(head) >= 3 and _has_letter(head):
        return head
    return None


def _anchors_above(lines: List[str], li: int) -> Iterator[Tuple[str, int]]:
    """(anchor text, offset) for label lines above line li, best first.

    Lines that read like labels ("Shipping to:") beat other digit-free lines (which
    may well be data, like a name), which beat the fixed start of a line with digits.
    Within each tier the nearest line wins.
    """
    found = []
    offset = 0
    for k in range(li - 1, max(-1, li - 13), -1):
        if not lines[k].strip():
            continue
        offset += 1
        stripped = lines[k].strip()
        if _static(stripped) and stripped.endswith(":"):
            tier = 0
        elif _static(stripped):
            tier = 1
        else:
            tier = 2
        label = _label_prefix(lines[k])
        if label:
            found.append((tier, offset, label))
    for _, offset, label in sorted(found):
        yield label, offset


def _fits(rule: Rule, text: str, start: int, end: int, want: Any = None) -> bool:
    try:
        hit = rules.apply(rule, text)
    except (rules.Miss, rules.RuleError):
        return False
    if want is not None:
        return hit.value == want and hit.start == start and hit.end == end
    return hit.start == start and hit.end == end


def _with_occurrence(rule: Rule, key: str, text: str, li: int, line_start: bool) -> Optional[Rule]:
    lines = text.split("\n")
    label = rule[key]
    if line_start:
        found = [i for i, l in enumerate(lines) if l.lstrip(" ").startswith(label)]
    else:
        found = [i for i, l in enumerate(lines) if label in l]
    if li not in found:
        return None
    n = found.index(li) + 1
    if n > 1:
        rule = dict(rule, occurrence=n)
    return rule


def _until_candidates(after: str) -> Iterator[Optional[str]]:
    """Text right after the value to stop at: a punctuation mark, then whole words."""
    if not after.strip():
        yield None
        return
    if not after[0].isspace() and not after[0].isalnum():
        yield after[0]
    for m in re.finditer(r"\s*\S+", after):
        yield after[: m.end()]


def _single_line(text: str, lines: List[str], li: int, s: int, e: int) -> Iterator[Rule]:
    line = lines[li]
    before, after = line[:s], line[e:]
    untils = list(_until_candidates(after))

    def with_untils(rule: Rule, key: str, line_start: bool) -> Iterator[Rule]:
        for until in untils:
            r = dict(rule, until=until) if until is not None else dict(rule)
            r = _with_occurrence(r, key, text, li, line_start)
            if r:
                yield r

    # 1. the whole fixed text before the value: "Order number: |NW-10482|"
    head = before.lstrip(" ").rstrip()
    if head and _static(head):
        yield from with_untils({"type": "after", "label": head, "line_start": True}, "label", True)
    # 2. fixed line start, data in between: "Discount (WELCOME10): -$|11.70|"
    for rule in _labels_on_line(line, before):
        yield from with_untils(rule, "line", True)
    # 3. the last few fixed words before the value, anywhere on a line
    if _has_letter(before):
        words = before.rstrip().split(" ")
        for k in range(min(4, len(words)), 0, -1):
            cand = " ".join(words[-k:]).strip()
            if cand and _static(cand) and cand != head:
                yield from with_untils({"type": "after", "label": cand}, "label", False)
    # 4. value at the start of a line: anchor on a label line above it
    if not before.strip():
        for anchor, offset in _anchors_above(lines, li):
            rule: Rule = {"type": "below", "anchor": anchor}
            if offset != 1:
                rule["offset"] = offset
            anchor_line = _anchor_index(lines, li, offset)
            for until in untils:
                r = dict(rule, until=until) if until is not None else dict(rule)
                r = _with_occurrence(r, "anchor", text, anchor_line, True)
                if r:
                    yield r
    # 5. last resort: a line-anchored regex built from the literal text around the value
    if before.strip():
        tail = r"\s*$" if not after.strip() else re.escape(next(u for u in untils if u))
        pattern = r"^\s*" + re.escape(before.lstrip(" ")) + r"(.+?)" + tail
        yield {"type": "regex", "pattern": pattern, "group": 1, "flags": "m"}


def _labels_on_line(line: str, before: str) -> Iterator[Rule]:
    """For lines whose start is fixed but whose label is mixed with data, e.g.
    "Discount (WELCOME10): -$11.70": keep to lines starting "Discount (" and take the
    value after the nearest fixed-looking word before it ("-$")."""
    prefix = _label_prefix(line)
    if not prefix or prefix == before.strip():
        return
    lead = len(line) - len(line.lstrip(" "))
    rest = before[lead + len(prefix):]
    if not rest.strip() or not line.lstrip(" ").startswith(prefix):
        return
    words = rest.rstrip().split(" ")
    for k in range(1, len(words) + 1):
        label = " ".join(words[-k:]).strip()
        if label and not any(ch.isdigit() for ch in label):
            yield {"type": "after", "line": prefix, "label": label}


def _anchor_index(lines: List[str], li: int, offset: int) -> int:
    seen, k = 0, li
    while seen < offset:
        k -= 1
        if lines[k].strip():
            seen += 1
    return k


def _multi_line(text: str, lines: List[str], li: int, lj: int) -> Iterator[Tuple[Rule, Any]]:
    picked = [k for k in range(li, lj + 1) if lines[k].strip()]
    want = [lines[p].strip() for p in picked]
    has_gap = len(picked) != lj - li + 1
    ends_block = lj + 1 >= len(lines) or not lines[lj + 1].strip()
    k = lj + 1
    while k < len(lines) and not lines[k].strip():
        k += 1
    end_label = _label_prefix(lines[k]) if k < len(lines) else None
    for anchor, offset in _anchors_above(lines, li):
        anchor_line = _anchor_index(lines, li, offset)
        base: Rule = {"type": "below", "anchor": anchor}
        if offset != 1:
            base["offset"] = offset
        options: List[Tuple[Rule, str]] = []
        # 1. a block that ends at a blank line (addresses, item lists of any length)
        if ends_block and not has_gap:
            options.append((dict(base, lines="block"), "anchor"))
        # 2. everything between two label lines
        if offset == 1 and end_label:
            options.append(({"type": "between", "start": anchor, "end": end_label}, "start"))
        # 3. a fixed number of lines
        options.append((dict(base, lines=len(picked)), "anchor"))
        for rule, key in options:
            rule = _with_occurrence(rule, key, text, anchor_line, True)
            if rule:
                yield rule, want


def infer(text: str, start: int, end: int) -> Rule:
    """Propose the first rule (in order of preference) that reproduces text[start:end]."""
    if not (0 <= start < end <= len(text)):
        raise InferError("selection is outside the email")
    start, end = _trim(text, start, end)
    lines = text.split("\n")
    offsets, pos = [], 0
    for l in lines:
        offsets.append(pos)
        pos += len(l) + 1
    li = max(i for i, o in enumerate(offsets) if o <= start)
    lj = max(i for i, o in enumerate(offsets) if o <= end - 1)

    if li == lj:
        s, e = start - offsets[li], end - offsets[li]
        for rule in _single_line(text, lines, li, s, e):
            if _fits(rule, text, start, end):
                return rule
        raise InferError("could not find fixed text around the selection to anchor a rule on; "
                         "write a regex rule by hand")

    # several lines: take them whole
    first = lines[li]
    start = offsets[li] + len(first) - len(first.lstrip(" "))
    last = lines[lj]
    end = offsets[lj] + len(last.rstrip(" "))
    for rule, want in _multi_line(text, lines, li, lj):
        if _fits(rule, text, start, end, want):
            return rule
    raise InferError("could not find a label line above the selected lines to anchor a rule on")
