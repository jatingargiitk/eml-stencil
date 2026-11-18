"""Templates: an ordered list of named fields, each with one rule, for one email family.

{
  "name": "northwind-order",
  "view": "plain",
  "match": {"from": "orders@northwind.example"},
  "fields": [
    {"name": "order_id", "rule": {"type": "after", "label": "Order number:", "line_start": true},
     "pattern": "NW-\\d+"},
    {"name": "gift_note", "rule": {...}, "required": false}
  ]
}

`match.from` (optional) skips emails from any other sender address; `match.subject`
(optional) is a substring the Subject must contain. `pattern` (optional) is a
regular expression the whole value (each item, for list values) must match.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import message, rules


class TemplateError(ValueError):
    pass


NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def name_for(path) -> str:
    """Template name from its file name: northwind.template.json -> northwind."""
    name = Path(path).name
    for suffix in (".json", ".template"):
        if name.endswith(suffix) and len(name) > len(suffix):
            name = name[: -len(suffix)]
    return name


def new(name: str, view: str = "plain", sender: Optional[str] = None) -> Dict[str, Any]:
    tpl: Dict[str, Any] = {"name": name, "view": view}
    if sender:
        tpl["match"] = {"from": sender}
    tpl["fields"] = []
    return tpl


def validate(tpl: Any) -> None:
    if not isinstance(tpl, dict):
        raise TemplateError("template must be a JSON object")
    if tpl.get("view", "plain") not in ("plain", "html", "auto"):
        raise TemplateError("'view' must be plain, html or auto")
    match = tpl.get("match", {})
    if not isinstance(match, dict) or set(match) - {"from", "subject"}:
        raise TemplateError("'match' may only contain 'from' and 'subject'")
    fields = tpl.get("fields")
    if not isinstance(fields, list):
        raise TemplateError("'fields' must be a list")
    seen = set()
    for f in fields:
        if not isinstance(f, dict) or "name" not in f or "rule" not in f:
            raise TemplateError("every field needs 'name' and 'rule'")
        name = f["name"]
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise TemplateError(f"bad field name {name!r} (letters, digits, underscore)")
        if name in seen:
            raise TemplateError(f"duplicate field {name!r}")
        seen.add(name)
        try:
            rules.validate(f["rule"])
        except rules.RuleError as exc:
            raise TemplateError(f"field {name!r}: {exc}") from None
        if "pattern" in f:
            try:
                re.compile(f["pattern"])
            except (re.error, TypeError) as exc:
                raise TemplateError(f"field {name!r}: bad pattern: {exc}") from None
        if not isinstance(f.get("required", True), bool):
            raise TemplateError(f"field {name!r}: 'required' must be true or false")


def load(path) -> Dict[str, Any]:
    try:
        tpl = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TemplateError(f"{path}: not valid JSON: {exc}") from None
    validate(tpl)
    return tpl


def dump(tpl: Dict[str, Any]) -> str:
    return json.dumps(tpl, indent=2, ensure_ascii=False) + "\n"


def save(tpl: Dict[str, Any], path) -> None:
    validate(tpl)
    Path(path).write_text(dump(tpl), encoding="utf-8")


@dataclass
class Result:
    file: Optional[str]
    status: str  # ok | partial | skipped | error
    values: Dict[str, Any] = field(default_factory=dict)
    spans: Dict[str, List[int]] = field(default_factory=dict)
    problems: Dict[str, str] = field(default_factory=dict)  # field -> why it's missing
    reason: Optional[str] = None  # for skipped / error

    def to_json(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"file": self.file} if self.file is not None else {}
        out["status"] = self.status
        if self.reason:
            out["reason"] = self.reason
        out["fields"] = self.values
        if self.problems:
            out["missing"] = self.problems
        return out


def skip_reason(tpl: Dict[str, Any], doc: message.Document) -> Optional[str]:
    match = tpl.get("match", {})
    want = match.get("from")
    if want and doc.sender != want.lower():
        return f"sender {doc.sender or '(none)'} is not {want}"
    subject = match.get("subject")
    if subject:
        line = doc.lines[0]
        if subject not in line[len("Subject: "):]:
            return f"subject does not contain {subject!r}"
    return None


def extract(tpl: Dict[str, Any], doc: message.Document, ignore_match: bool = False) -> Result:
    if not ignore_match:
        why = skip_reason(tpl, doc)
        if why:
            return Result(doc.path, "skipped", reason=why)
    res = Result(doc.path, "ok")
    for f in tpl["fields"]:
        name = f["name"]
        try:
            hit = rules.apply(f["rule"], doc.text)
        except rules.Miss as exc:
            res.values[name] = None
            if f.get("required", True):
                res.problems[name] = str(exc)
            continue
        bad = _pattern_problem(f, hit.value)
        if bad:
            res.values[name] = None
            res.problems[name] = bad
            continue
        res.values[name] = hit.value
        res.spans[name] = [hit.start, hit.end]
    if res.problems:
        res.status = "partial"
    return res


def _pattern_problem(f: Dict[str, Any], value: Any) -> Optional[str]:
    pattern = f.get("pattern")
    if not pattern:
        return None
    items = value if isinstance(value, list) else [value]
    for item in items:
        if not re.fullmatch(pattern, item):
            return f"value {item!r} does not match pattern /{pattern}/"
    return None


def extract_file(tpl: Dict[str, Any], path, ignore_match: bool = False) -> Result:
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        return Result(str(path), "error", reason=str(exc))
    return extract_bytes(tpl, raw, path=str(path), ignore_match=ignore_match)


def extract_bytes(tpl: Dict[str, Any], raw: bytes, path: Optional[str] = None, ignore_match: bool = False) -> Result:
    try:
        doc = message.load(raw, view=tpl.get("view", "plain"), path=path)
    except message.EmailError as exc:
        # an email from another family may simply lack the body part this template reads
        if not ignore_match:
            try:
                why = skip_reason(tpl, message.load(raw, view="auto", path=path))
            except message.EmailError:
                why = None
            if why:
                return Result(path, "skipped", reason=why)
        return Result(path, "error", reason=str(exc))
    return extract(tpl, doc, ignore_match=ignore_match)
