#!/usr/bin/env bash
# 本番 render を隔離 kind で動く形にする。置き換えるのは kind に存在しない依存境界だけ:
# - cloudflared と langfuse-cloudflared (Cloudflare tunnel / AWS Parameter Store が必要。本番で確認)
# - Longhorn StorageClass (kind は local-path。reclaim と backup 対象外は下の静的検査と本番で確認)
set -euo pipefail
: "${REPO_ROOT:?REPO_ROOT is required}"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
if [ -n "${MANIFEST_DIR:-}" ] && [ -f "$MANIFEST_DIR/langfuse.yaml" ]; then
  cp "$MANIFEST_DIR/langfuse.yaml" "$tmp/render.yaml"
else
  kustomize build "$REPO_ROOT/argoproj/langfuse/" > "$tmp/render.yaml"
fi
yq -o=json -I=0 '.' "$tmp/render.yaml" | jq -s '.' > "$tmp/docs.json"
jq -e '
  ([.[] | select(.kind == "StorageClass" and .metadata.name == "langfuse-longhorn")] | length == 1) and
  (.[] | select(.kind == "StorageClass") | .reclaimPolicy == "Delete" and .parameters.numberOfReplicas == "1"
     and ((.parameters.recurringJobSelector // "") == "")) and
  ([.[] | select(.kind == "StatefulSet") | .spec.volumeClaimTemplates[].metadata.labels // {} | keys[]
     | select(test("recurring-job"))] | length == 0)
' "$tmp/docs.json" >/dev/null || { echo 'StorageClass が短期保持・backup 対象外の設計から外れています' >&2; exit 1; }
jq '[.[]
  | select((.kind == "StorageClass") | not)
  | select((.metadata.name == "langfuse-cloudflared" and (.kind == "Deployment" or .kind == "ExternalSecret")) | not)
  | if .kind == "StatefulSet" then .spec.volumeClaimTemplates |= map(.spec.storageClassName = "standard") else . end]
  | {apiVersion: "v1", kind: "List", items: .}' "$tmp/docs.json"
