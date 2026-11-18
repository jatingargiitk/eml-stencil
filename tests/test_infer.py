import re

import pytest

from eml_stencil import message, rules
from eml_stencil.infer import InferError, infer, span_of_lines, span_of_text


def rule_for(doc, needle, occurrence=1):
    s, e = span_of_text(doc.text, needle, occurrence)
    return infer(doc.text, s, e)


def apply(rule, doc):
    try:
        return rules.apply(rule, doc.text).value
    except rules.Miss:
        return None


# authored on northwind-2, expected on northwind-1 / northwind-3
CASES = [
    ("NW-10517", 2, {"type": "after", "label": "Order number:", "line_start": True}, "NW-10482", "NW-10533"),
    ("November 7, 2026", 1, {"type": "after", "label": "Placed:", "line_start": True}, "November 3, 2026", "November 12, 2026"),
    ("$117.00", 1, {"type": "after", "label": "Subtotal:", "line_start": True}, "$36.00", "$50.50"),
    ("$105.30", 1, {"type": "after", "label": "Total charged:", "line_start": True, "until": " (Visa"}, "$40.95", "$55.45"),
    ("2241", 1, {"type": "after", "line": "Total charged:", "label": "ending", "until": ")"}, "1141", "3341"),
    ("11.70", 1, {"type": "after", "line": "Discount", "label": "-$"}, None, None),
    ("WELCOME10", 1, {"type": "after", "label": "Discount (", "line_start": True, "until": ")"}, None, None),
    ("Marcus", 1, {"type": "after", "label": "Hi", "line_start": True, "until": ","}, "Jane", "Priya"),
    ("Happy birthday, Dad! Enjoy the Huila.", 1, {"type": "below", "anchor": "Gift note:"}, None, None),
    ("marcus@example.com", 1, {"type": "after", "label": "To:", "line_start": True}, "jane@example.com", "priya@example.com"),
]


@pytest.mark.parametrize("needle, occ, rule, one, three", CASES, ids=[c[0] for c in CASES])
def test_rules_carry_over_to_the_rest_of_the_family(mail, needle, occ, rule, one, three):
    doc2 = message.load(mail / "northwind-2.eml")
    got = rule_for(doc2, needle, occ)
    assert got == rule
    assert apply(got, doc2) == needle
    assert apply(got, message.load(mail / "northwind-1.eml")) == one
    assert apply(got, message.load(mail / "northwind-3.eml")) == three


def test_subject_value_uses_whole_words_for_until(mail):
    doc = message.load(mail / "northwind-2.eml")
    assert rule_for(doc, "NW-10517") == {
        "type": "after", "label": "Subject: Your Northwind order #", "line_start": True, "until": " is"}


def test_multi_line_block_and_items(mail):
    doc2 = message.load(mail / "northwind-2.eml")
    s, e = span_of_lines(doc2.text, 24, 27)
    assert infer(doc2.text, s, e) == {"type": "below", "anchor": "Shipping to:", "lines": "block"}
    s, e = span_of_lines(doc2.text, 14, 16)
    items = infer(doc2.text, s, e)
    assert items == {"type": "below", "anchor": "Your items:", "lines": "block"}
    assert apply(items, message.load(mail / "northwind-1.eml")) == ["2 x Ethiopia Guji, whole bean, 250 g  $36.00"]


def test_anchor_prefers_label_lines_over_data_lines(mail):
    doc2 = message.load(mail / "northwind-2.eml")
    # "Marcus Webb" and "Flat 2, ..." sit closer, but "Shipping to:" reads like a label
    assert rule_for(doc2, "Leeds LS2 7EA") == {"type": "below", "anchor": "Shipping to:", "offset": 3}


def test_block_of_html_table_rows(mail):
    doc = message.load(mail / "parcelly-1.eml")
    first = doc.lines.index("Tracking history") + 2
    s, e = span_of_lines(doc.text, first, first + 2)
    rule = infer(doc.text, s, e)
    assert rule == {"type": "below", "anchor": "Tracking history", "lines": "block"}
    other = message.load(mail / "parcelly-2.eml")
    assert apply(rule, other) == ["Nov 8, 09:40 | Label created", "Nov 9, 06:21 | Departed Parcelly hub"]


def test_between_when_lines_run_straight_into_a_label():
    text = "Items:\n1 x Tea\n2 x Mug\nSubtotal: $3\n"
    s, e = span_of_lines(text, 2, 3)
    rule = infer(text, s, e)
    assert rule == {"type": "between", "start": "Items:", "end": "Subtotal:"}
    assert rules.apply(rule, "Items:\n1 x Tea\n2 x Mug\n3 x Cup\nSubtotal: $9").value == ["1 x Tea", "2 x Mug", "3 x Cup"]


def test_html_table_cells(mail):
    doc = message.load(mail / "parcelly-1.eml")
    rule = rule_for(doc, "Friday, November 6")
    assert rule == {"type": "after", "label": "Estimated delivery |", "line_start": True}
    assert apply(rule, message.load(mail / "parcelly-2.eml")) == "Wednesday, November 11"
    city = rule_for(doc, "Portland, OR")
    assert apply(city, message.load(mail / "parcelly-2.eml")) == "München"


def test_occurrence_is_recorded():
    text = "Note: a\nNote: b\n"
    s, e = span_of_text(text, "b")
    assert infer(text, s, e) == {"type": "after", "label": "Note:", "line_start": True, "occurrence": 2}


def test_selection_whitespace_is_trimmed():
    text = "Order number: A-1\n"
    assert infer(text, 13, 17) == {"type": "after", "label": "Order number:", "line_start": True}


def test_no_fixed_text_fails_loudly():
    with pytest.raises(InferError):
        infer("12345\n", 0, 5)
    with pytest.raises(InferError, match="empty"):
        infer("a   b", 1, 3)


def test_span_helpers():
    with pytest.raises(InferError, match="occurs 1 time"):
        span_of_text("abc", "b", 2)
    with pytest.raises(InferError, match="outside"):
        span_of_lines("a\nb", 2, 3)
    assert span_of_lines("ab\ncd\nef", 2, 3) == (3, 8)


SAMPLES = ["northwind-1.eml", "northwind-2.eml", "northwind-3.eml", "northwind-4.eml", "parcelly-1.eml", "parcelly-2.eml"]


@pytest.mark.parametrize("name", SAMPLES)
def test_every_word_either_round_trips_or_fails_loudly(mail, name):
    """Property: whatever single word is selected, an inferred rule reproduces it exactly."""
    text = message.load(mail / name).text
    tried = inferred = 0
    for m in re.finditer(r"\S+", text):
        tried += 1
        try:
            rule = infer(text, m.start(), m.end())
        except InferError:
            continue
        inferred += 1
        hit = rules.apply(rule, text)
        assert (hit.start, hit.end, hit.value) == (m.start(), m.end(), m.group(0)), rule
    assert inferred >= tried * 0.9
