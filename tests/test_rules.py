import pytest

from eml_stencil import rules
from eml_stencil.rules import Miss, RuleError, apply

TEXT = """Subject: Order #A-1 is confirmed

Order number: A-1
  Total charged: $10.00 (Visa ending 4242)
Discount (SAVE5): -$0.50
Ship to:
Jane Doe
1 Main St

Items:
1 x Tea
2 x Mug
Subtotal: $9.50
Note: first
Note: second"""


def value(rule):
    hit = apply(rule, TEXT)
    assert TEXT[hit.start:hit.end] == (hit.value if isinstance(hit.value, str) else TEXT[hit.start:hit.end])
    return hit.value


def test_after():
    assert value({"type": "after", "label": "Order number:", "line_start": True}) == "A-1"
    assert value({"type": "after", "label": "Total charged:", "line_start": True, "until": " ("}) == "$10.00"
    assert value({"type": "after", "label": "ending", "until": ")"}) == "4242"
    assert value({"type": "after", "label": "Note:", "occurrence": 2}) == "second"
    assert value({"type": "after", "line": "Discount", "label": "-$"}) == "0.50"


def test_after_misses_explain_why():
    with pytest.raises(Miss, match="no line starting with 'Total:'"):
        apply({"type": "after", "label": "Total:", "line_start": True}, TEXT)
    with pytest.raises(Miss, match="only 2 line"):
        apply({"type": "after", "label": "Note:", "occurrence": 3}, TEXT)
    with pytest.raises(Miss, match="not found after the value"):
        apply({"type": "after", "label": "Order number:", "until": "!"}, TEXT)
    with pytest.raises(Miss, match="not found on the line"):
        apply({"type": "after", "line": "Discount", "label": "+$"}, TEXT)


def test_line_start_ignores_indent_but_not_substrings():
    assert value({"type": "after", "label": "Total charged:", "line_start": True, "until": " ("}) == "$10.00"
    with pytest.raises(Miss):
        apply({"type": "after", "label": "charged:", "line_start": True}, TEXT)


def test_below_single_block_and_count():
    assert value({"type": "below", "anchor": "Ship to:"}) == "Jane Doe"
    assert value({"type": "below", "anchor": "Ship to:", "offset": 2}) == "1 Main St"
    assert value({"type": "below", "anchor": "Ship to:", "until": " "}) == "Jane"
    assert value({"type": "below", "anchor": "Ship to:", "lines": "block"}) == ["Jane Doe", "1 Main St"]
    # counted lines skip blank lines
    assert value({"type": "below", "anchor": "Ship to:", "offset": 2, "lines": 2}) == ["1 Main St", "Items:"]
    with pytest.raises(Miss, match="fewer than 40"):
        apply({"type": "below", "anchor": "Ship to:", "offset": 40}, TEXT)


def test_below_span_covers_the_lines():
    hit = apply({"type": "below", "anchor": "Ship to:", "lines": "block"}, TEXT)
    assert TEXT[hit.start:hit.end] == "Jane Doe\n1 Main St"


def test_between():
    assert value({"type": "between", "start": "Items:", "end": "Subtotal:"}) == ["1 x Tea", "2 x Mug"]
    with pytest.raises(Miss, match="no line starting with 'Total:' after 'Items:'"):
        apply({"type": "between", "start": "Items:", "end": "Total:"}, TEXT)
    with pytest.raises(Miss, match="nothing between"):
        apply({"type": "between", "start": "Note: first", "end": "Note: second"}, TEXT)


def test_regex():
    assert value({"type": "regex", "pattern": r"order #(\S+)", "group": 1, "flags": "i"}) == "A-1"
    assert value({"type": "regex", "pattern": r"(?P<code>[A-Z]+\d)\)", "group": "code"}) == "SAVE5"
    with pytest.raises(Miss):
        apply({"type": "regex", "pattern": "nope"}, TEXT)


@pytest.mark.parametrize("rule, msg", [
    ({"type": "nope"}, "unknown rule type"),
    ({"type": "after"}, "needs 'label'"),
    ({"type": "after", "label": ""}, "non-empty"),
    ({"type": "after", "label": "x", "occurrence": 0}, "occurrence"),
    ({"type": "after", "label": "x", "line": "a", "line_start": True}, "either"),
    ({"type": "below", "anchor": "x", "lines": 0}, "lines"),
    ({"type": "below", "anchor": "x", "lines": 2, "until": ","}, "single line"),
    ({"type": "between", "start": "x"}, "needs 'end'"),
    ({"type": "regex", "pattern": "("}, "bad regex"),
    ({"type": "regex", "pattern": "(a)", "group": 2}, "1 group"),
    ({"type": "regex", "pattern": "a", "flags": "x"}, "unknown regex flag"),
    ("after", "JSON object"),
])
def test_validate(rule, msg):
    with pytest.raises(RuleError, match=msg):
        rules.validate(rule)
