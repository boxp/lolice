#!/usr/bin/env bash
# 本番 kustomize render から抽出した最小 manifest を server-side apply する。
# strict field validation と生成 ConfigMap 名の実物を保つため native apply を使わない。
set -euo pipefail
: "${REPO_ROOT:?REPO_ROOT is required}"
manifest="$(mktemp)"
trap 'rm -f "$manifest"' EXIT
"${REPO_ROOT}/tests/chainsaw/codex-workspace/render-minimal.sh" > "$manifest"
kubectl apply --server-side -f "$manifest"
