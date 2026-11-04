#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT
python3 -I - "$root/ai-ops-outcome-rules.yaml" "$tmpdir/rules.yaml" <<'PY'
import pathlib, sys
text = pathlib.Path(sys.argv[1]).read_text()
spec = text.split('\nspec:\n', 1)[1]
pathlib.Path(sys.argv[2]).write_text(''.join(line[2:] if line.startswith('  ') else line for line in spec.splitlines(keepends=True)))
PY
cp "$root/tests/ai-ops-outcome-rules.test.yaml" "$tmpdir/test.yaml"
if command -v promtool >/dev/null 2>&1; then
  (cd "$tmpdir" && promtool test rules test.yaml)
else
  docker run --rm -v "$tmpdir:/work" --entrypoint promtool prom/prometheus:v3.9.1 test rules /work/test.yaml
fi
