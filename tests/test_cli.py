import csv
import io
import json
import shutil

import pytest

from eml_stencil import template
from eml_stencil.cli import main


@pytest.fixture
def tpl(tmp_path, mail):
    path = tmp_path / "northwind.json"
    ex = str(mail / "northwind-2.eml")
    assert main(["add", str(path), ex, "--field", "order_id", "--text", "NW-10517", "--occurrence", "2",
                 "--pattern", r"NW-\d+"]) == 0
    assert main(["add", str(path), ex, "--field", "total", "--text", "$105.30"]) == 0
    assert main(["add", str(path), ex, "--field", "ship_to", "--lines", "24-27"]) == 0
    assert main(["add", str(path), ex, "--field", "gift_note", "--text", "Happy birthday, Dad! Enjoy the Huila.",
                 "--optional"]) == 0
    return path


def test_add_creates_template_with_sender_match(tpl, capsys):
    data = template.load(tpl)
    assert data["name"] == "northwind"
    assert data["view"] == "plain"
    assert data["match"] == {"from": "orders@northwind.example"}
    assert [f["name"] for f in data["fields"]] == ["order_id", "total", "ship_to", "gift_note"]
    assert data["fields"][0]["pattern"] == r"NW-\d+"
    assert data["fields"][3]["required"] is False


def test_add_prints_rule_and_value(tmp_path, mail, capsys):
    path = tmp_path / "t.json"
    assert main(["add", str(path), str(mail / "northwind-1.eml"), "--field", "placed", "--text", "November 3, 2026"]) == 0
    out = capsys.readouterr().out
    assert out == 'placed: {"type": "after", "label": "Placed:", "line_start": true}\n  November 3, 2026\n'


def test_add_refuses_duplicate_without_replace(tpl, mail, capsys):
    ex = str(mail / "northwind-2.eml")
    assert main(["add", str(tpl), ex, "--field", "total", "--text", "$117.00"]) == 2
    assert "already has a field 'total'" in capsys.readouterr().err
    assert main(["add", str(tpl), ex, "--field", "total", "--text", "$117.00", "--replace"]) == 0
    fields = template.load(tpl)["fields"]
    assert [f["name"] for f in fields] == ["order_id", "total", "ship_to", "gift_note"]
    assert fields[1]["rule"]["label"] == "Subtotal:"


def test_add_checks_pattern_against_the_example(tmp_path, mail, capsys):
    path = tmp_path / "t.json"
    assert main(["add", str(path), str(mail / "northwind-1.eml"), "--field", "x", "--text", "NW-10482",
                 "--pattern", r"\d+"]) == 2
    assert "does not match pattern" in capsys.readouterr().err
    assert not path.exists()


def test_add_errors(tmp_path, mail, capsys):
    path = str(tmp_path / "t.json")
    ex = str(mail / "northwind-1.eml")
    assert main(["add", path, ex, "--field", "x", "--text", "not in the email"]) == 2
    assert main(["add", path, ex, "--field", "x", "--lines", "a-b"]) == 2
    assert main(["add", path, ex, "--field", "9bad", "--text", "NW-10482"]) == 2
    assert main(["add", path, str(tmp_path / "missing.eml"), "--field", "x", "--text", "a"]) == 2
    err = capsys.readouterr().err
    assert "occurs 0 time(s)" in err and "--lines wants" in err and "bad field name" in err


def test_preview(tpl, mail, capsys):
    assert main(["preview", str(tpl), str(mail)]) == 1  # northwind-4 changed its layout
    out = capsys.readouterr().out
    assert "examples/mail/newsletter.eml: skipped (sender news@northwind.example is not orders@northwind.example)" in out
    assert "  total      MISSING: no line starting with 'Total charged:'" in out
    assert "  gift_note  Happy birthday, Dad! Enjoy the Huila." in out
    assert out.rstrip().endswith("3 ok, 1 partial, 3 skipped, 0 error")
    assert main(["preview", str(tpl), str(mail / "northwind-1.eml")]) == 0


def test_parse_json_jsonl_csv(tpl, mail, capsys):
    assert main(["parse", str(tpl), str(mail)]) == 0
    records = json.loads(capsys.readouterr().out)
    assert [r["status"] for r in records] == ["ok", "ok", "ok", "partial"]
    assert records[1]["fields"]["ship_to"] == ["Marcus Webb", "Flat 2, 19 Ropemaker Road", "Leeds LS2 7EA", "United Kingdom"]
    assert records[3]["missing"] == {"total": "no line starting with 'Total charged:'"}

    assert main(["parse", str(tpl), str(mail), "--format", "jsonl", "--include-skipped"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 7 and json.loads(lines[0])["status"] == "skipped"

    assert main(["parse", str(tpl), str(mail / "northwind-1.eml"), "--format", "csv"]) == 0
    rows = list(csv.reader(io.StringIO(capsys.readouterr().out)))
    assert rows[0] == ["file", "status", "order_id", "total", "ship_to", "gift_note"]
    assert rows[1][2:] == ["NW-10482", "$40.95", "Jane Alvarez\n88 Linden Street, Apt 4B\nBrooklyn, NY 11211\nUnited States", ""]


def test_parse_strict_and_all(tpl, mail, capsys):
    assert main(["parse", str(tpl), str(mail), "--strict"]) == 1
    capsys.readouterr()
    assert main(["parse", str(tpl), str(mail / "newsletter.eml"), "--all"]) == 0
    rec = json.loads(capsys.readouterr().out)[0]
    assert rec["status"] == "partial"
    # the newsletter has an "Order number:" line too; the pattern is what rejects it
    assert rec["missing"]["order_id"] == r"value 'not an order, just news.' does not match pattern /NW-\d+/"
    assert rec["missing"]["total"] == "no line starting with 'Total charged:'"


def test_show(mail, capsys):
    assert main(["show", str(mail / "parcelly-1.eml")]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].endswith("parcelly-1.eml (html body)")
    assert " 8  Tracking number | PX4419027785" in out


def test_bad_template(tmp_path, mail, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text('{"fields": [{"name": "a", "rule": {"type": "after"}}]}')
    assert main(["parse", str(bad), str(mail)]) == 2
    assert "field 'a': after rule needs 'label'" in capsys.readouterr().err
    bad.write_text("{")
    assert main(["preview", str(bad), str(mail)]) == 2


def test_template_validation():
    for tpl, msg in [
        ([], "JSON object"),
        ({"fields": {}}, "'fields' must be a list"),
        ({"fields": [], "view": "x"}, "'view'"),
        ({"fields": [], "match": {"to": "a"}}, "'match'"),
        ({"fields": [{"name": "a", "rule": {"type": "regex", "pattern": "a"}}] * 2}, "duplicate"),
        ({"fields": [{"name": "a", "rule": {"type": "regex", "pattern": "a"}, "pattern": "("}]}, "bad pattern"),
        ({"fields": [{"name": "a", "rule": {"type": "regex", "pattern": "a"}, "required": "no"}]}, "required"),
    ]:
        with pytest.raises(template.TemplateError, match=msg):
            template.validate(tpl)


def test_subject_match(mail):
    tpl = {"match": {"subject": "is on its way"}, "view": "html",
           "fields": [{"name": "t", "rule": {"type": "after", "label": "Tracking number |", "line_start": True}}]}
    assert template.extract_file(tpl, mail / "parcelly-1.eml").values == {"t": "PX4419027785"}
    res = template.extract_file(tpl, mail / "northwind-1.eml")
    assert res.status == "skipped" and res.reason == "subject does not contain 'is on its way'"


def test_html_only_mail_from_other_sender_is_skipped_not_error(tpl, mail):
    res = template.extract_file(template.load(tpl), mail / "parcelly-1.eml")
    assert res.status == "skipped"
    res = template.extract_file(template.load(tpl), mail / "parcelly-1.eml", ignore_match=True)
    assert res.status == "error" and "no text/plain part" in res.reason
