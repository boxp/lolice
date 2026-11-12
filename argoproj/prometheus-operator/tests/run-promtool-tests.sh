#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"

run_one() {
  local rules="$1" test="$2" tmpdir
  tmpdir="$(mktemp -d)"
  # The prom/prometheus image runs promtool as "nobody"; mktemp creates a 0700 dir.
  chmod 755 "$tmpdir"
  python3 -I - "$root/$rules" "$tmpdir/rules.yaml" <<'PY'
import pathlib, sys
text = pathlib.Path(sys.argv[1]).read_text()
spec = text.split('\nspec:\n', 1)[1]
pathlib.Path(sys.argv[2]).write_text(''.join(line[2:] if line.startswith('  ') else line for line in spec.splitlines(keepends=True)))
PY
  sed "s|__RULE_FILE__|rules.yaml|" "$root/tests/$test" > "$tmpdir/test.yaml"
  chmod 644 "$tmpdir"/*.yaml
  local status=0
  if command -v promtool >/dev/null 2>&1; then
    (cd "$tmpdir" && promtool test rules test.yaml) || status=$?
  else
    docker run --rm -v "$tmpdir:/work" --entrypoint promtool prom/prometheus:v3.9.1 test rules /work/test.yaml || status=$?
  fi
  rm -rf "$tmpdir"
  return "$status"
}

run_one ai-ops-outcome-rules.yaml ai-ops-outcome-rules.test.yaml
run_one ai-provider-quota-rules.yaml ai-provider-quota-rules.test.yaml
run_one ai-subscription-forecast-rules.yaml ai-subscription-forecast-rules.test.yaml

run_one local-llm-gpu-observability-rules.yaml local-llm-gpu-observability-rules.test.yaml
