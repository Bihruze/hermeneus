#!/usr/bin/env bash
# Local quality check. Run from the repo root: scripts/check.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${HERMENEUS_PY:-.venv/bin/python}"

echo "== tests"
"$PY" -m pytest -q 2>&1 | tail -8

echo "== em dash scan"
if grep -rn --exclude-dir=.git --exclude-dir=.venv --exclude-dir=node_modules $'\xe2\x80\x94' . ; then
  echo "FAIL: em dash found"; exit 1
else
  echo "ok: no em dashes"
fi

echo "== binary scan (rule 17)"
found=$(git ls-files -z | xargs -0 file | grep -E "Mach-O|ELF|PE32|WebAssembly|byte-compiled" || true)
if [ -n "$found" ]; then echo "$found"; echo "FAIL: binary tracked"; exit 1; else echo "ok: no binaries tracked"; fi

echo "== attribution scan"
if git log --format=%B | grep -nE "Co-Authored-By|Generated with" ; then
  echo "FAIL: attribution line in history"; exit 1
else
  echo "ok: no attribution lines"
fi

if command -v hermes >/dev/null 2>&1; then
  echo "== hermes plugins validate"
  hermes plugins validate . 2>&1 | tail -25
fi
