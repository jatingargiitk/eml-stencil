"""Two small local HTTP servers (standard library only).

`ui`    the authoring page: highlight a value in an example email, name it, and the
        rule is inferred, shown and checked against the other samples. Bound to
        127.0.0.1, only answers requests whose Host is localhost/127.0.0.1, and only
        accepts JSON POSTs, so other web pages can't drive it.
`serve` POST a raw email (RFC 822 bytes) to /parse and get the extracted fields as
        JSON, for an n8n/Zapier-style HTTP step or a mail hook.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from . import infer, message, rules, template

MAX_BODY = 25 * 1024 * 1024


class _Base(BaseHTTPRequestHandler):
    server_version = "eml-stencil"

    def log_message(self, fmt, *args):  # keep the terminal quiet
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj: Any) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _body(self) -> Optional[bytes]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self._json(413, {"error": f"body must be at most {MAX_BODY} bytes"})
            return None
        return self.rfile.read(length)


# --------------------------------------------------------------------- authoring UI

class Session:
    """Everything the UI edits: one example email and the template being built."""

    def __init__(self, email_path: Path, template_path: Path, samples: List[Path], view: str = "auto"):
        self.lock = threading.Lock()
        self.email_path = email_path
        self.template_path = template_path
        self.samples = samples
        if template_path.exists():
            self.template = template.load(template_path)
            view = self.template.get("view", view)
        else:
            self.template = None
        self.doc = message.load(email_path, view=view)
        if self.template is None:
            self.template = template.new(template_path.stem, view=self.doc.view, sender=self.doc.sender or None)
        self.saved = template_path.exists()

    def state(self) -> Dict[str, Any]:
        res = template.extract(self.template, self.doc, ignore_match=True)
        fields = []
        for f in self.template["fields"]:
            name = f["name"]
            fields.append({
                "name": name,
                "rule": f["rule"],
                "required": f.get("required", True),
                "pattern": f.get("pattern"),
                "value": res.values.get(name),
                "span": res.spans.get(name),
                "problem": res.problems.get(name),
            })
        return {
            "email": str(self.email_path),
            "view": self.doc.view,
            "text": self.doc.text,
            "template_path": str(self.template_path),
            "saved": self.saved,
            "template": self.template,
            "fields": fields,
            "samples": len(self.samples),
        }

    def add(self, req: Dict[str, Any]) -> Dict[str, Any]:
        name = str(req.get("name", "")).strip()
        if not template.NAME_RE.match(name):
            raise ValueError("field name: letters, digits and underscores, not starting with a digit")
        if any(f["name"] == name for f in self.template["fields"]):
            raise ValueError(f"there is already a field called {name!r}")
        start, end = req.get("start"), req.get("end")
        if not isinstance(start, int) or not isinstance(end, int):
            raise ValueError("select some text in the email first")
        rule = infer.infer(self.doc.text, start, end)
        field: Dict[str, Any] = {"name": name, "rule": rule}
        if req.get("optional"):
            field["required"] = False
        if req.get("pattern"):
            field["pattern"] = str(req["pattern"])
        self._replace_fields(self.template["fields"] + [field])
        return self.state()

    def update(self, req: Dict[str, Any]) -> Dict[str, Any]:
        name = req.get("name")
        fields = []
        for f in self.template["fields"]:
            if f["name"] == name:
                f = dict(f, rule=req.get("rule"))
                if "pattern" in req:
                    if req["pattern"]:
                        f["pattern"] = str(req["pattern"])
                    else:
                        f.pop("pattern", None)
                if "required" in req:
                    if req["required"] is False:
                        f["required"] = False
                    else:
                        f.pop("required", None)
            fields.append(f)
        if not any(f["name"] == name for f in fields):
            raise ValueError(f"no field called {name!r}")
        self._replace_fields(fields)
        return self.state()

    def remove(self, req: Dict[str, Any]) -> Dict[str, Any]:
        name = req.get("name")
        fields = [f for f in self.template["fields"] if f["name"] != name]
        if len(fields) == len(self.template["fields"]):
            raise ValueError(f"no field called {name!r}")
        self._replace_fields(fields)
        return self.state()

    def _replace_fields(self, fields: List[Dict[str, Any]]) -> None:
        candidate = dict(self.template, fields=fields)
        template.validate(candidate)
        self.template = candidate
        self.saved = False

    def save(self) -> Dict[str, Any]:
        template.save(self.template, self.template_path)
        self.saved = True
        return self.state()

    def preview(self) -> Dict[str, Any]:
        out = [template.extract_file(self.template, p).to_json() for p in self.samples]
        return {"results": out, "fields": [f["name"] for f in self.template["fields"]]}


class _UIHandler(_Base):
    server: "UIServer"

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        if host in ("127.0.0.1", "localhost", "::1"):
            return True
        self._json(403, {"error": "the authoring UI only answers on localhost"})
        return False

    def do_GET(self):
        if not self._host_ok():
            return
        path = urlparse(self.path).path
        if path == "/":
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/state":
            with self.server.session.lock:
                self._json(200, self.server.session.state())
        elif path == "/api/preview":
            with self.server.session.lock:
                self._json(200, self.server.session.preview())
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok():
            return
        if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
            self._json(415, {"error": "send application/json"})
            return
        raw = self._body()
        if raw is None:
            return
        try:
            req = json.loads(raw or b"{}")
            if not isinstance(req, dict):
                raise ValueError("expected a JSON object")
        except ValueError as exc:
            self._json(400, {"error": f"bad JSON: {exc}"})
            return
        session = self.server.session
        actions = {"/api/add": session.add, "/api/update": session.update,
                   "/api/remove": session.remove, "/api/save": lambda _req: session.save()}
        action = actions.get(urlparse(self.path).path)
        if action is None:
            self._json(404, {"error": "not found"})
            return
        try:
            with session.lock:
                self._json(200, action(req))
        except (ValueError, template.TemplateError, rules.RuleError, infer.InferError) as exc:
            self._json(400, {"error": str(exc)})


class UIServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, session: Session, host: str = "127.0.0.1", port: int = 0):
        super().__init__((host, port), _UIHandler)
        self.session = session
        self.verbose = False


# --------------------------------------------------------------------- parse endpoint

class _ParseHandler(_Base):
    server: "ParseServer"

    def do_GET(self):
        if urlparse(self.path).path == "/health":
            self._json(200, {"ok": True, "template": self.server.template.get("name")})
        else:
            self._json(404, {"error": "POST a raw email to /parse"})

    def do_POST(self):
        url = urlparse(self.path)
        if url.path != "/parse":
            self._json(404, {"error": "POST a raw email to /parse"})
            return
        raw = self._body()
        if raw is None:
            return
        if not raw.strip():
            self._json(400, {"error": "empty body: POST the raw email (RFC 822)"})
            return
        ignore = parse_qs(url.query).get("ignore_match", ["0"])[0] in ("1", "true", "yes")
        res = template.extract_bytes(self.server.template, raw, ignore_match=ignore)
        self._json(422 if res.status == "error" else 200, res.to_json())


class ParseServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, tpl: Dict[str, Any], host: str = "127.0.0.1", port: int = 0):
        super().__init__((host, port), _ParseHandler)
        self.template = tpl
        self.verbose = False


PAGE = (Path(__file__).with_name("ui.html")).read_text(encoding="utf-8")
