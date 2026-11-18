"""eml-stencil command line."""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import webbrowser
from pathlib import Path
from typing import List, Optional

from . import __version__, infer, message, rules, server, template


def _emails(paths: List[str]) -> List[Path]:
    out: List[Path] = []
    for p in paths:
        path = Path(p)
        if path.is_dir():
            out.extend(sorted(x for x in path.iterdir() if x.suffix.lower() == ".eml" and x.is_file()))
        else:
            out.append(path)
    return out


def _fmt(value) -> List[str]:
    if value is None:
        return ["(none)"]
    if isinstance(value, list):
        return [f"- {v}" for v in value]
    return value.split("\n")


def cmd_show(args) -> int:
    doc = message.load(args.email, view=args.view)
    lines = doc.lines
    width = len(str(len(lines)))
    print(f"# {args.email} ({doc.view} body)")
    for i, line in enumerate(lines, 1):
        print(f"{i:>{width}}  {line}".rstrip())
    return 0


def cmd_add(args) -> int:
    tpath = Path(args.template)
    if tpath.exists():
        tpl = template.load(tpath)
        doc = message.load(args.email, view=tpl.get("view", "plain"))
    else:
        doc = message.load(args.email, view=args.view)
        tpl = template.new(tpath.stem, view=doc.view, sender=doc.sender or None)
    if args.text is not None:
        start, end = infer.span_of_text(doc.text, args.text, args.occurrence)
    else:
        try:
            a, _, b = args.lines.partition("-")
            first, last = int(a), int(b or a)
        except ValueError:
            raise infer.InferError(f"--lines wants N or N-M, got {args.lines!r}") from None
        start, end = infer.span_of_lines(doc.text, first, last)
    rule = infer.infer(doc.text, start, end)
    fields = [f for f in tpl["fields"] if f["name"] != args.field]
    if len(fields) != len(tpl["fields"]) and not args.replace:
        raise template.TemplateError(f"{tpath} already has a field {args.field!r} (use --replace)")
    field = {"name": args.field, "rule": rule}
    if args.optional:
        field["required"] = False
    if args.pattern:
        field["pattern"] = args.pattern
    tpl["fields"] = fields + [field] if len(fields) == len(tpl["fields"]) else \
        [field if f["name"] == args.field else f for f in tpl["fields"]]
    template.validate(tpl)
    hit = rules.apply(rule, doc.text)
    problem = template._pattern_problem(field, hit.value)
    if problem:
        raise template.TemplateError(f"{args.field}: {problem}")
    template.save(tpl, tpath)
    print(f"{args.field}: {json.dumps(rule, ensure_ascii=False)}")
    for line in _fmt(hit.value):
        print(f"  {line}")
    return 0


def _results(args):
    tpl = template.load(args.template)
    return tpl, [template.extract_file(tpl, p, ignore_match=args.all) for p in _emails(args.emails)]


def cmd_preview(args) -> int:
    tpl, results = _results(args)
    names = [f["name"] for f in tpl["fields"]]
    width = max([len(n) for n in names] + [4])
    counts = {"ok": 0, "partial": 0, "skipped": 0, "error": 0}
    for res in results:
        counts[res.status] += 1
        head = f"{res.file}: {res.status}"
        if res.reason:
            head += f" ({res.reason})"
        print(head)
        if res.status in ("skipped", "error"):
            continue
        for name in names:
            if name in res.problems:
                print(f"  {name:<{width}}  MISSING: {res.problems[name]}")
                continue
            lines = _fmt(res.values.get(name))
            print(f"  {name:<{width}}  {lines[0]}")
            for extra in lines[1:]:
                print(f"  {'':<{width}}  {extra}")
    print(", ".join(f"{v} {k}" for k, v in counts.items()))
    return 1 if counts["partial"] or counts["error"] else 0


def cmd_parse(args) -> int:
    tpl, results = _results(args)
    if not args.include_skipped:
        results = [r for r in results if r.status != "skipped"]
    records = [r.to_json() for r in results]
    if args.format == "json":
        print(json.dumps(records, indent=2, ensure_ascii=False))
    elif args.format == "jsonl":
        for rec in records:
            print(json.dumps(rec, ensure_ascii=False))
    else:
        names = [f["name"] for f in tpl["fields"]]
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(["file", "status"] + names)
        for r in results:
            row = [r.file, r.status]
            for n in names:
                v = r.values.get(n)
                row.append("" if v is None else "\n".join(v) if isinstance(v, list) else v)
            w.writerow(row)
        sys.stdout.write(buf.getvalue())
    bad = any(r.status in ("partial", "error") for r in results)
    return 1 if args.strict and bad else 0


def cmd_ui(args) -> int:
    email_path = Path(args.email)
    tpath = Path(args.template) if args.template else email_path.with_suffix(".template.json")
    samples = _emails([args.samples]) if args.samples else _emails([str(email_path.parent)])
    session = server.Session(email_path, tpath, samples, view=args.view)
    srv = server.UIServer(session, port=args.port)
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(f"eml-stencil UI for {email_path} at {url}")
    print(f"template: {tpath} ({len(samples)} sample email(s) for preview); Ctrl-C to stop")
    sys.stdout.flush()
    if not args.no_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


def cmd_serve(args) -> int:
    tpl = template.load(args.template)
    srv = server.ParseServer(tpl, host=args.host, port=args.port)
    host, port = srv.server_address[:2]
    print(f"POST raw emails to http://{host}:{port}/parse (template {tpl.get('name')}); Ctrl-C to stop")
    sys.stdout.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="eml-stencil",
        description="Highlight values in one example email, get a reusable template, parse the rest of that email family.",
    )
    p.add_argument("--version", action="version", version=f"eml-stencil {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("show", help="print the text rules run against, with line numbers")
    s.add_argument("email")
    s.add_argument("--view", choices=message.VIEWS, default="auto",
                   help="body to use: text/plain, text/html converted to text, or auto (plain if present)")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("ui", help="open the local authoring page for an example email")
    s.add_argument("email")
    s.add_argument("--template", help="template file to create or edit (default: EMAIL with .template.json)")
    s.add_argument("--samples", help="folder of .eml files to preview against (default: the email's folder)")
    s.add_argument("--view", choices=message.VIEWS, default="auto")
    s.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default: any free port)")
    s.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    s.set_defaults(func=cmd_ui)

    s = sub.add_parser("add", help="add a field from the command line by naming the text to select")
    s.add_argument("template")
    s.add_argument("email")
    s.add_argument("--field", required=True)
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--text", help="the value as it appears in the email")
    g.add_argument("--lines", help="whole lines N or N-M, numbered as by `show`")
    s.add_argument("--occurrence", type=int, default=1, help="which occurrence of --text (default 1)")
    s.add_argument("--pattern", help="regex the whole value must match")
    s.add_argument("--optional", action="store_true", help="a missing value is not a problem")
    s.add_argument("--replace", action="store_true", help="replace an existing field of that name")
    s.add_argument("--view", choices=message.VIEWS, default="auto", help="only used when creating the template")
    s.set_defaults(func=cmd_add)

    for name, func, helptext in (("preview", cmd_preview, "show what a template extracts from each email"),
                                 ("parse", cmd_parse, "extract fields as JSON, JSONL or CSV")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("template")
        s.add_argument("emails", nargs="+", help=".eml files or folders of them")
        s.add_argument("--all", action="store_true", help="ignore the template's sender/subject match")
        if name == "parse":
            s.add_argument("--format", choices=("json", "jsonl", "csv"), default="json")
            s.add_argument("--include-skipped", action="store_true", help="also list emails the match skipped")
            s.add_argument("--strict", action="store_true", help="exit 1 if any email is missing a required field")
        s.set_defaults(func=func)

    s = sub.add_parser("serve", help="HTTP endpoint: POST a raw email to /parse, get JSON back")
    s.add_argument("template")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8025)
    s.set_defaults(func=cmd_serve)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (message.EmailError, template.TemplateError, infer.InferError, rules.RuleError,
            rules.Miss, OSError) as exc:
        print(f"eml-stencil: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
