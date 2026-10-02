#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
promtool_bin="${PROMTOOL_BIN:-promtool}"
rule_manifest="$repo_root/argoproj/prometheus-operator/control-plane-node-rules.yaml"
test_file="$repo_root/argoproj/prometheus-operator/tests/control-plane-node-rules.test.yaml"
generated_rule_file="$(mktemp)"
generated_test_file="$(mktemp)"
trap 'rm -f "$generated_rule_file" "$generated_test_file"' EXIT

command -v "$promtool_bin" >/dev/null || {
  echo "promtool is required; set PROMTOOL_BIN to its path." >&2
  exit 127
}

# PrometheusRule is a Kubernetes CRD. Extract only spec.groups so promtool can
# parse and evaluate the exact rules that Argo CD deploys. kubectl's JSON output
# also normalizes the Kubernetes YAML without requiring a language runtime.
kubectl create --dry-run=client --validate=false -f "$rule_manifest" -o json \
  | jq '{groups: .spec.groups}' > "$generated_rule_file"
sed "s|__RULE_FILE__|$generated_rule_file|" "$test_file" > "$generated_test_file"

"$promtool_bin" check rules "$generated_rule_file"
"$promtool_bin" test rules "$generated_test_file"
