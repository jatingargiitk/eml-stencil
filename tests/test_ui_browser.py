"""Drive the authoring page in a real browser: select text, add fields, save, preview.

Needs `pip install playwright && playwright install chromium` (the CI `ui` job does
this); skipped otherwise. CHROME_PATH points it at an already installed Chrome.
"""

import os
import threading

import pytest

from eml_stencil import server, template

sync_api = pytest.importorskip("playwright.sync_api")

SELECT = """([needle, occ]) => {
  const doc = document.getElementById('doc');
  const text = doc.textContent;
  let pos = -1;
  for (let i = 0; i < occ; i++) pos = text.indexOf(needle, pos + 1);
  if (pos < 0) throw new Error('not found: ' + needle);
  const walker = document.createTreeWalker(doc, NodeFilter.SHOW_TEXT);
  const r = document.createRange();
  let n, acc = 0, started = false;
  while ((n = walker.nextNode())) {
    const len = n.textContent.length;
    if (!started && pos < acc + len) { r.setStart(n, pos - acc); started = true; }
    if (started && pos + needle.length <= acc + len) { r.setEnd(n, pos + needle.length - acc); break; }
    acc += len;
  }
  const s = window.getSelection(); s.removeAllRanges(); s.addRange(r);
}"""


@pytest.fixture
def page_url(tmp_path, mail):
    session = server.Session(mail / "northwind-2.eml", tmp_path / "t.json", sorted(mail.glob("*.eml")))
    srv = server.UIServer(session)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/", tmp_path / "t.json"
    srv.shutdown()
    srv.server_close()


def test_select_add_save_preview(page_url):
    url, path = page_url
    with sync_api.sync_playwright() as pw:
        kwargs = {"executable_path": os.environ["CHROME_PATH"]} if os.environ.get("CHROME_PATH") else {}
        browser = pw.chromium.launch(**kwargs)
        page = browser.new_page()
        page.goto(url)
        page.wait_for_selector("#fields")
        for name, needle, occ in [
            ("order_id", "NW-10517", 2),
            ("total", "$105.30", 1),
            ("ship_to", "Marcus Webb\nFlat 2, 19 Ropemaker Road\nLeeds LS2 7EA\nUnited Kingdom", 1),
        ]:
            page.evaluate(SELECT, [needle, occ])
            page.fill("#fname", name)  # moving focus to the input must not lose the selection
            page.click("#add")
            page.wait_for_selector(f'.field[data-field="{name}"]')
        assert page.inner_text('.field[data-field="total"] .value') == "$105.30"
        # highlights are drawn without changing the text the offsets are computed on
        assert page.evaluate("document.getElementById('doc').textContent") == \
            page.evaluate("fetch('/api/state').then(r => r.json()).then(s => s.text)")
        assert page.locator("#doc mark").count() == 3

        # adding without a selection explains itself
        page.fill("#fname", "nothing")
        page.click("#add")
        assert "Highlight a value" in page.inner_text("#addmsg")

        page.click("#save")
        page.wait_for_selector("#savestate:has-text('saved')")
        page.click("#preview")
        page.wait_for_selector("#previewout table")
        table = page.inner_text("#previewout")
        browser.close()

    tpl = template.load(path)
    assert [f["name"] for f in tpl["fields"]] == ["order_id", "total", "ship_to"]
    assert tpl["fields"][0]["rule"] == {"type": "after", "label": "Order number:", "line_start": True}
    assert "NW-10482" in table and "$40.95" in table
    assert "✗ no line starting with 'Total charged:'" in table
