from eml_stencil import message
from eml_stencil.message import html_to_text, normalize_text

import pytest


def test_plain_document_has_header_block(mail):
    doc = message.load(mail / "northwind-1.eml")
    assert doc.view == "plain"
    assert doc.sender == "orders@northwind.example"
    assert doc.lines[:5] == [
        "Subject: Your Northwind order #NW-10482 is confirmed",
        "From: Northwind Coffee <orders@northwind.example>",
        "To: jane@example.com",
        "Date: Tue, 03 Nov 2026 09:14:02 +0000",
        "",
    ]
    assert "Order number: NW-10482" in doc.lines


def test_html_only_falls_back_to_html_and_decodes_quoted_printable(mail):
    doc = message.load(mail / "parcelly-2.eml")
    assert doc.view == "html"
    assert "Your parcel from München has left our hub and is on its way." in doc.lines
    assert "Tracking number | PX4419031102" in doc.lines
    assert "Estimated delivery | Wednesday, November 11" in doc.lines
    assert not any("font-family" in line for line in doc.lines)  # <style> dropped
    assert not any("Parcelly</title>" in line or line == "Parcelly" for line in doc.lines)


def test_view_selection(mail):
    html = message.load(mail / "northwind-1.eml", view="html")
    assert html.view == "html"
    assert "Order NW-10482" in html.lines
    with pytest.raises(message.EmailError, match="no text/plain"):
        message.load(mail / "parcelly-1.eml", view="plain")


def test_bytes_and_crlf():
    raw = b"Subject: Hi\r\nFrom: A <a@x.example>\r\n\r\nline one\r\n\r\n\r\n\r\nline\xc2\xa0two   \r\n"
    doc = message.load(raw)
    assert doc.text.endswith("line one\n\nline two")
    assert doc.lines[1] == "From: A <a@x.example>"
    assert doc.lines[2] == "To: "


def test_folded_subject_is_one_line():
    raw = b"Subject: a very long\r\n subject line\r\nFrom: a@x.example\r\n\r\nbody\r\n"
    assert message.load(raw).lines[0] == "Subject: a very long subject line"


def test_html_to_text_tables_and_blocks():
    html = """<div>Hi&nbsp;<b>Jane</b>,<br>thanks</div>
    <table><tr><td><table><tr><th>Item</th><td>Qty</td></tr><tr><td>Beans</td><td>2</td></tr></table></td></tr></table>
    <script>var x = "<p>not text</p>";</script><p>Total: &pound;4</p>"""
    assert html_to_text(html).split("\n") == ["Hi Jane,", "thanks", "Item | Qty", "Beans | 2", "", "Total: £4"]


def test_normalize_text():
    assert normalize_text("a\t b  \r\n\r\n\r\nc\n\n") == "a     b\n\nc"
