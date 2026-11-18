#!/usr/bin/env bash
# Re-runs every command shown in README.md and stores its output in examples/expected/.
# Rebuilds the two example templates from scratch, so they are checked too.
set -u
cd "$(dirname "$0")/.."
out=examples/expected
mkdir -p "$out"
rm -f examples/northwind.template.json examples/parcelly.template.json

run() {
  local name=$1; shift
  { echo "\$ eml-stencil $*"; eml-stencil "$@"; echo "[exit $?]"; } > "$out/$name.txt" 2>&1
}

# several commands recorded into one file
runs() {
  local name=$1; shift
  : > "$out/$name.txt"
  while [ $# -gt 0 ]; do
    local cmd=$1; shift
    { echo "\$ eml-stencil $cmd"; eval "eml-stencil $cmd"; echo "[exit $?]"; } >> "$out/$name.txt" 2>&1
  done
}

T=examples/northwind.template.json
E=examples/mail/northwind-2.eml
run show-northwind show $E
runs add-northwind \
  "add $T $E --field order_id --text NW-10517 --occurrence 2 --pattern 'NW-\d+'" \
  "add $T $E --field placed --text 'November 7, 2026'" \
  "add $T $E --field items --lines 14-16" \
  "add $T $E --field discount_code --text WELCOME10 --optional" \
  "add $T $E --field total --text '\$105.30'" \
  "add $T $E --field card_last4 --text 2241 --pattern '\d{4}'" \
  "add $T $E --field ship_to --lines 24-27" \
  "add $T $E --field gift_note --text 'Happy birthday, Dad! Enjoy the Huila.' --optional"
run preview-northwind preview $T examples/mail
run parse-json parse $T examples/mail/northwind-1.eml
run parse-csv parse $T examples/mail/northwind-3.eml examples/mail/northwind-4.eml --format csv

P=examples/parcelly.template.json
H=examples/mail/parcelly-1.eml
run show-parcelly show $H
runs add-parcelly \
  "add $P $H --field tracking --text PX4419027785 --occurrence 2" \
  "add $P $H --field eta --text 'Friday, November 6'" \
  "add $P $H --field history --lines 13-15"
run parse-parcelly parse $P examples/mail/parcelly-2.eml --format jsonl

# the HTTP endpoint, as an n8n "HTTP Request" step would call it
{
  echo "\$ eml-stencil serve $T --port 8025 &"
  eml-stencil serve $T --port 8025 & pid=$!
  for _ in $(seq 50); do curl -s http://127.0.0.1:8025/health >/dev/null && break; sleep 0.1; done
  echo "\$ curl -s --data-binary @examples/mail/northwind-3.eml http://127.0.0.1:8025/parse"
  curl -s --data-binary @examples/mail/northwind-3.eml http://127.0.0.1:8025/parse; echo
  kill $pid; wait $pid 2>/dev/null || true
} > "$out/serve.txt" 2>&1
