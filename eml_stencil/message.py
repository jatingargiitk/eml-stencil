"""Load an .eml file and turn it into the plain-text document that rules run against.

The document is a small header block followed by the body:

    Subject: Your Northwind order #NW-10482
    From: Northwind Coffee <orders@northwind.example>
    To: jane@example.com
    Date: Tue, 03 Nov 2026 09:14:02 +0000

    Hi Jane,
    ...

Rules only ever see this text, so `eml-stencil show` prints exactly what a
template is matched against. The body is the text/plain part when there is one,
otherwise the text/html part converted to text (block tags become line breaks,
table cells are joined with " | ").
"""

from __future__ import annotations

import email
import email.policy
import re
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import parseaddr
from html.parser import HTMLParser
from pathlib import Path
from typing import List, Optional, Union

HEADER_FIELDS = ("Subject", "From", "To", "Date")
VIEWS = ("auto", "plain", "html")


@dataclass
class Document:
    text: str
    view: str  # "plain" or "html": which body part was used
    sender: str  # bare address from From:, lowercased
    path: Optional[str] = None

    @property
    def lines(self) -> List[str]:
        return self.text.split("\n")


class EmailError(ValueError):
    pass


def load(source: Union[str, Path, bytes], view: str = "auto", path: Optional[str] = None) -> Document:
    """Parse raw RFC 822 bytes (or a path to an .eml file) into a Document."""
    if view not in VIEWS:
        raise ValueError(f"view must be one of {VIEWS}, got {view!r}")
    if isinstance(source, (str, Path)):
        path = str(source)
        raw = Path(source).read_bytes()
    else:
        raw = source
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    if not isinstance(msg, EmailMessage) or (not msg.keys() and not raw.strip()):
        raise EmailError("empty message")

    plain = _part_text(msg, "plain")
    html = _part_text(msg, "html")
    if view == "plain" or (view == "auto" and plain is not None and plain.strip()):
        if plain is None:
            raise EmailError("message has no text/plain part")
        body, used = normalize_text(plain), "plain"
    else:
        if html is None:
            if plain is None:
                raise EmailError("message has neither a text/plain nor a text/html part")
            raise EmailError("message has no text/html part")
        body, used = html_to_text(html), "html"

    header_lines = []
    for name in HEADER_FIELDS:
        value = msg.get(name)
        header_lines.append(f"{name}: {_one_line(str(value)) if value is not None else ''}")
    text = "\n".join(header_lines) + "\n\n" + body
    sender = parseaddr(str(msg.get("From", "")))[1].lower()
    return Document(text=text, view=used, sender=sender, path=path)


def _one_line(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _part_text(msg: EmailMessage, subtype: str) -> Optional[str]:
    """First non-attachment text/<subtype> part, decoded."""
    for part in msg.walk():
        if part.is_multipart():
            continue
        if part.get_content_type() != f"text/{subtype}":
            continue
        if part.get_content_disposition() == "attachment":
            continue
        try:
            content = part.get_content()
        except (LookupError, UnicodeDecodeError):
            payload = part.get_payload(decode=True) or b""
            content = payload.decode("utf-8", errors="replace")
        if not isinstance(content, str):
            content = content.decode("utf-8", errors="replace")
        if re.search("[\ufffd\udc80-\udcff]", content):
            # 8-bit text with no (or a wrong) charset: most of it is UTF-8 in practice
            payload = part.get_payload(decode=True) or b""
            try:
                content = payload.decode("utf-8")
            except UnicodeDecodeError:
                if re.search("[\udc80-\udcff]", content):
                    content = payload.decode("latin-1")
        return content
    return None


def normalize_text(text: str) -> str:
    """Unify line endings and spaces, drop trailing whitespace and runs of blank lines."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u00a0", " ").replace("\t", "    ")
    out: List[str] = []
    for line in text.split("\n"):
        line = line.rstrip()
        if not line and (not out or not out[-1]):
            continue
        out.append(line)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)


_BLOCK = {
    "p", "div", "br", "tr", "li", "ul", "ol", "table", "tbody", "thead", "tfoot",
    "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "hr", "section",
    "article", "header", "footer", "address", "center", "dd", "dt", "dl",
}
_SKIP = {"script", "style", "head", "title", "template", "noscript"}


class _TextExtractor(HTMLParser):
    """Block tags end a line; each table row becomes one line of " | "-joined cells.

    Layout emails nest tables; a nested <table> simply closes whatever row was
    being collected, so the innermost rows (the ones holding data) stay intact.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: List[str] = []
        self.current: List[str] = []
        self.cells: List[str] = []
        self.in_cell = False
        self.skip_depth = 0

    @staticmethod
    def _squash(parts: List[str]) -> str:
        return re.sub(r"[ \t\f\v\r\n\u00a0]+", " ", "".join(parts)).strip()

    def _flush_line(self, keep_blank: bool = True) -> None:
        line = self._squash(self.current)
        self.current = []
        if line or keep_blank:
            self.lines.append(line)

    def _end_cell(self) -> None:
        if self.in_cell:
            self.cells.append(self._squash(self.current))
            self.current = []
            self.in_cell = False

    def _end_row(self) -> None:
        self._end_cell()
        cells = [c for c in self.cells if c]
        self.cells = []
        if cells:
            self.lines.append(" | ".join(cells))

    def _break(self) -> None:
        if self.in_cell:
            self.current.append(" ")
        else:
            self._flush_line()

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self.skip_depth += 1
        elif self.skip_depth:
            return
        elif tag in ("td", "th"):
            self._end_cell()
            self._flush_line(keep_blank=False)
            self.in_cell = True
        elif tag in ("tr", "table"):
            self._end_row()
            self._flush_line(keep_blank=False)
        elif tag in _BLOCK:
            self._break()

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag in _SKIP:
            self.skip_depth = max(0, self.skip_depth - 1)
        elif self.skip_depth:
            return
        elif tag in ("td", "th"):
            self._end_cell()
        elif tag == "tr":
            self._end_row()
            self._flush_line(keep_blank=False)
        elif tag == "table":
            self._end_row()
            self._flush_line(keep_blank=False)
            self.lines.append("")
        elif tag in _BLOCK:
            self._break()

    def handle_data(self, data):
        if not self.skip_depth:
            self.current.append(data)

    def close(self):
        super().close()
        self._end_row()
        self._flush_line()


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return normalize_text("\n".join(parser.lines))
