#!/bin/bash
# Run the selector natively (no Docker) on a sample dir and score it.
set -e
HERE="$(cd "$(dirname "$0")/.." && pwd)"
SAMPLE="${1:-$HERE/../kit/sample}"
OUTDIR="${2:-/tmp/br-local}"
rm -rf "$OUTDIR/tmp"; mkdir -p "$OUTDIR/tmp"
cd "$HERE"
/usr/bin/time -p env BR_SRC="$HERE/../sqlglot" BR_MAP="$HERE/map.json" BR_DATA="$SAMPLE" BR_TMP="$OUTDIR/tmp" \
  OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES PATH="$HERE/../venv/bin:$PATH" \
  "$HERE/../venv/bin/python" selector.py > "$OUTDIR/out.jsonl" 2> "$OUTDIR/err.log" || true
grep -v "__version__" "$OUTDIR/err.log" | tail -45
python3 score.py "$SAMPLE" "$SAMPLE/answers.json" "$OUTDIR/out.jsonl"
