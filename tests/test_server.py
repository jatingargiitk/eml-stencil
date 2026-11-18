import http.client
import json
import threading

import pytest

from eml_stencil import server, template


def start(srv):
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv.server_address[1]


def call(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, body=body, headers=headers or {})
    res = conn.getresponse()
    data = res.read()
    conn.close()
    try:
        return res.status, json.loads(data)
    except ValueError:
        return res.status, data.decode()


def post(port, path, obj):
    return call(port, "POST", path, json.dumps(obj), {"Content-Type": "application/json"})


@pytest.fixture
def ui(tmp_path, mail):
    session = server.Session(mail / "northwind-2.eml", tmp_path / "t.json", sorted(mail.glob("*.eml")))
    srv = server.UIServer(session)
    port = start(srv)
    yield port, tmp_path / "t.json"
    srv.shutdown()
    srv.server_close()


def test_ui_page_and_state(ui):
    port, _ = ui
    status, page = call(port, "GET", "/")
    assert status == 200 and "Add field from selection" in page
    status, state = call(port, "GET", "/api/state")
    assert status == 200
    assert state["view"] == "plain" and state["saved"] is False and state["samples"] == 7
    assert state["template"]["match"] == {"from": "orders@northwind.example"}
    assert state["text"].split("\n")[9] == "Order number: NW-10517"


def test_ui_add_update_remove_save_preview(ui):
    port, path = ui
    _, state = call(port, "GET", "/api/state")
    text = state["text"]
    start = text.index("$105.30")
    status, state = post(port, "/api/add", {"name": "total", "start": start, "end": start + 7})
    assert status == 200
    f = state["fields"][0]
    assert f["rule"] == {"type": "after", "label": "Total charged:", "line_start": True, "until": " (Visa"}
    assert f["value"] == "$105.30" and f["span"] == [start, start + 7]

    status, err = post(port, "/api/add", {"name": "total", "start": start, "end": start + 7})
    assert status == 400 and "already" in err["error"]
    status, err = post(port, "/api/add", {"name": "x"})
    assert status == 400 and "select some text" in err["error"]

    status, state = post(port, "/api/update", {"name": "total", "rule": {"type": "after", "label": "Nope:"}})
    assert status == 200 and state["fields"][0]["problem"] == "no line containing 'Nope:'"
    status, err = post(port, "/api/update", {"name": "total", "rule": {"type": "after"}})
    assert status == 400 and "needs 'label'" in err["error"]

    status, state = post(port, "/api/update", {"name": "total", "rule": f["rule"], "pattern": r"\$[\d.]+"})
    assert state["fields"][0]["pattern"] == r"\$[\d.]+" and state["fields"][0]["value"] == "$105.30"

    assert not path.exists()
    status, state = post(port, "/api/save", {})
    assert status == 200 and state["saved"] is True
    assert template.load(path)["fields"][0]["name"] == "total"

    status, pv = call(port, "GET", "/api/preview")
    assert pv["fields"] == ["total"]
    by_file = {r["file"].rsplit("/", 1)[1]: r for r in pv["results"]}
    assert by_file["northwind-1.eml"]["fields"] == {"total": "$40.95"}
    assert by_file["northwind-4.eml"]["status"] == "partial"
    assert by_file["parcelly-1.eml"]["status"] == "skipped"

    status, state = post(port, "/api/remove", {"name": "total"})
    assert state["fields"] == [] and state["saved"] is False


def test_ui_rejects_foreign_hosts_and_non_json(ui):
    port, _ = ui
    status, err = call(port, "GET", "/api/state", headers={"Host": "evil.example"})
    assert status == 403
    status, err = call(port, "POST", "/api/save", "{}", {"Content-Type": "text/plain"})
    assert status == 415
    status, err = call(port, "POST", "/api/save", "[", {"Content-Type": "application/json"})
    assert status == 400


def test_ui_reopens_existing_template(tmp_path, mail):
    path = tmp_path / "t.json"
    template.save({"name": "t", "view": "plain", "fields": [
        {"name": "placed", "rule": {"type": "after", "label": "Placed:", "line_start": True}}]}, path)
    session = server.Session(mail / "northwind-3.eml", path, [])
    state = session.state()
    assert state["saved"] is True and state["fields"][0]["value"] == "November 12, 2026"


@pytest.fixture
def parse_port(mail):
    tpl = {"name": "nw", "view": "plain", "match": {"from": "orders@northwind.example"}, "fields": [
        {"name": "order_id", "rule": {"type": "after", "label": "Order number:", "line_start": True}},
        {"name": "total", "rule": {"type": "after", "label": "Total charged:", "line_start": True, "until": " ("}},
    ]}
    srv = server.ParseServer(tpl)
    port = start(srv)
    yield port
    srv.shutdown()
    srv.server_close()


def test_parse_endpoint(parse_port, mail):
    status, res = call(parse_port, "POST", "/parse", (mail / "northwind-3.eml").read_bytes())
    assert status == 200
    assert res == {"status": "ok", "fields": {"order_id": "NW-10533", "total": "$55.45"}}
    status, res = call(parse_port, "POST", "/parse", (mail / "northwind-4.eml").read_bytes())
    assert res["status"] == "partial" and res["fields"]["total"] is None
    status, res = call(parse_port, "POST", "/parse", (mail / "parcelly-1.eml").read_bytes())
    assert res["status"] == "skipped"
    status, res = call(parse_port, "POST", "/parse?ignore_match=1", (mail / "parcelly-1.eml").read_bytes())
    assert status == 422 and res["status"] == "error"
    status, res = call(parse_port, "POST", "/parse", b"")
    assert status == 400
    status, res = call(parse_port, "GET", "/health")
    assert res == {"ok": True, "template": "nw"}
    status, _ = call(parse_port, "POST", "/other", b"x")
    assert status == 404
