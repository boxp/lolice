#!/usr/bin/env bash
# PrometheusRule マニフェストを promtool で検証する。
#
# argoproj/prometheus-operator/tests/<name>.test.yaml ごとに、対になる
# argoproj/prometheus-operator/<name>.yaml から spec.groups を取り出して
# `promtool check rules` と `promtool test rules` を実行する。
#
# 使い方:
#   scripts/test-prometheus-rules.sh                 # tests/ 配下の全ペア
#   scripts/test-prometheus-rules.sh longhorn-storage-rules   # 指定した名前だけ
#   PROMTOOL_BIN=/path/to/promtool scripts/test-prometheus-rules.sh
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
promtool_bin="${PROMTOOL_BIN:-promtool}"
rules_dir="$repo_root/argoproj/prometheus-operator"
tests_dir="$rules_dir/tests"
work_dir="$(mktemp -d)"
trap 'rm -rf "$work_dir"' EXIT

command -v "$promtool_bin" >/dev/null || {
  echo "promtool is required; set PROMTOOL_BIN to its path." >&2
  exit 127
}

names=("$@")
if [ "${#names[@]}" -eq 0 ]; then
  for test_file in "$tests_dir"/*.test.yaml; do
    name="$(basename "$test_file" .test.yaml)"
    names+=("$name")
  done
fi

failed=0
for name in "${names[@]}"; do
  rule_manifest="$rules_dir/$name.yaml"
  test_file="$tests_dir/$name.test.yaml"
  if [ ! -f "$rule_manifest" ] || [ ! -f "$test_file" ]; then
    echo "missing rule manifest or test file for '$name' ($rule_manifest, $test_file)" >&2
    failed=1
    continue
  fi

  generated_rule_file="$work_dir/$name.rules.json"
  generated_test_file="$work_dir/$name.test.yaml"

  # PrometheusRule is a Kubernetes CRD. Extract only spec.groups so promtool can
  # parse and evaluate the exact rules that Argo CD deploys. kubectl's JSON output
  # also normalizes the Kubernetes YAML without requiring a language runtime.
  kubectl create --dry-run=client --validate=false -f "$rule_manifest" -o json \
    | jq '{groups: .spec.groups}' > "$generated_rule_file"
  sed "s|__RULE_FILE__|$generated_rule_file|" "$test_file" > "$generated_test_file"

  echo "==> $name"
  "$promtool_bin" check rules "$generated_rule_file" || failed=1
  "$promtool_bin" test rules "$generated_test_file" || failed=1
done

exit "$failed"
