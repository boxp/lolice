#!/usr/bin/env bash
# 本番 render を隔離 kind で動く形にする。置き換えるのは kind に存在しない依存境界だけ:
# - cloudflared と langfuse-cloudflared (Cloudflare tunnel / AWS Parameter Store が必要。本番で確認)
# - ExternalSecret langfuse-oidc (Parameter Store。Access for SaaS の値なので合成値の Secret に置換。実経路は本番で確認)
# - NEXTAUTH_URL (kind に TLS の公開 hostname が無いので Service の URL)。MCP proxy の Host も同じ値に揃える
# - Longhorn StorageClass (kind は local-path。2 replica・別 node 強制、reclaim と backup 対象外は下の静的検査と本番で確認)
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
  (.[] | select(.kind == "StorageClass") | .reclaimPolicy == "Delete" and .parameters.numberOfReplicas == "2"
     and .parameters.replicaSoftAntiAffinity == "disabled"
     and ((.parameters.recurringJobSelector // "") == "")) and
  ([.[] | select(.kind == "StatefulSet") | .spec.volumeClaimTemplates[].metadata.labels // {} | keys[]
     | select(test("recurring-job"))] | length == 0)
' "$tmp/docs.json" >/dev/null || { echo 'StorageClass が 2 replica・短期保持・backup 対象外の設計から外れています' >&2; exit 1; }
# Calico はホスト名で絞れず kind から Cloudflare にも届かないので、web だけが Cloudflare の範囲の :443 に出られる形をここで固定する。
jq -e '
  [.[] | select(.kind == "NetworkPolicy" and .metadata.name == "langfuse-web-oidc-egress")] | length == 1 and
  (.[0].spec.selector == "app == '"'"'langfuse-web'"'"'") and (.[0].spec.egress | length == 1) and
  (.[0].spec.egress[0].destination.ports == [443]) and
  (.[0].spec.egress[0].destination.nets | length > 0 and all(. != "0.0.0.0/0" and (split("/")[1] | tonumber) >= 13))
' "$tmp/docs.json" >/dev/null || { echo 'web の OIDC egress が想定より広がっています' >&2; exit 1; }
jq '[.[]
  | select((.kind == "StorageClass") | not)
  | select((.metadata.name == "langfuse-cloudflared" and (.kind == "Deployment" or .kind == "ExternalSecret")) | not)
  | select((.metadata.name == "langfuse-oidc" and .kind == "ExternalSecret") | not)
  | if (.kind == "Deployment" and (.metadata.name == "langfuse-web" or .metadata.name == "langfuse-sign-in-gateway"))
    then .spec.template.spec.containers[0].env |= map(if .name == "NEXTAUTH_URL" then .value = "http://langfuse-web.langfuse.svc.cluster.local:3000" else . end) else . end
  | if (.kind == "Deployment" and .metadata.name == "langfuse-mcp-readonly")
    then .spec.template.spec.containers[0].env |= map(if .name == "LANGFUSE_PUBLIC_HOST" then .value = "langfuse-web.langfuse.svc.cluster.local:3000" else . end) else . end
  | if .kind == "StatefulSet" then .spec.volumeClaimTemplates |= map(.spec.storageClassName = "standard") else . end]
  | . + [{apiVersion: "v1", kind: "Secret", metadata: {name: "langfuse-oidc", namespace: "langfuse"}, stringData: {
      ISSUER: "http://langfuse-oidc-fixture.langfuse.svc.cluster.local:8080", CLIENT_ID: "synthetic-client",
      CLIENT_SECRET: "synthetic-client-secret", OWNER_EMAIL: "owner@example.invalid"}}]
  | {apiVersion: "v1", kind: "List", items: .}' "$tmp/docs.json"
