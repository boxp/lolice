#!/usr/bin/env bash
set -euo pipefail
cd "${REPO_ROOT:-$(git rev-parse --show-toplevel)}"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
for component in kohako-studio codex-workspace hermes-agent; do
  kustomize build "argoproj/$component" > "$tmp/$component"
done
python3 tests/chainsaw/kohako-studio/render-check.py "$tmp"
