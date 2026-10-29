#!/usr/bin/env bash
# 本番 render からメトリクスサイドカーの起動に必要な資源だけを取り出す。
set -euo pipefail
: "${REPO_ROOT:?REPO_ROOT is required}"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
if [ -n "${MANIFEST_DIR:-}" ] && [ -f "$MANIFEST_DIR/codex-workspace.yaml" ]; then
  cp "$MANIFEST_DIR/codex-workspace.yaml" "$tmp/render.yaml"
else
  kustomize build "$REPO_ROOT/argoproj/codex-workspace/" > "$tmp/render.yaml"
fi
yq -o=json -I=0 'select((.kind == "Namespace" and .metadata.name == "codex-workspace") or (.kind == "ConfigMap" and (.metadata.name | test("^codex-workspace-ai-ops-metrics-"))) or (.kind == "Deployment" and .metadata.name == "codex-workspace"))' "$tmp/render.yaml" | jq -s '.' > "$tmp/docs.json"
jq -e '
  (.[ ] | select(.kind == "Deployment") | .spec.template.spec.volumes[] | select(.name == "ai-ops-metrics-app") | .configMap.name) as $ref |
  ([.[] | select(.kind == "Namespace")] | length == 1) and
  ([.[] | select(.kind == "Deployment")] | length == 1) and
  ([.[] | select(.kind == "ConfigMap")] | length == 1) and
  any(.[]; .kind == "ConfigMap" and .metadata.namespace == "codex-workspace" and .metadata.name == $ref)
' "$tmp/docs.json" >/dev/null || { echo '生成 ConfigMap と volume の参照が一致しません' >&2; exit 1; }
{ jq -c '.[] | select(.kind != "Deployment")' "$tmp/docs.json"
jq -c '.[] | select(.kind == "Deployment") |
  .spec.template.spec as $pod |
  ($pod.containers | map(select(.name == "ai-ops-metrics")) | .[0]) as $container |
  ($pod.volumes | map(select(.name == "ai-ops-metrics-app")) | .[0]) as $volume |
  ($container.env | map(select(.name == "AI_OPS_METRICS_FILE")) | .[0].value) as $path |
  {apiVersion, kind, metadata: {name: .metadata.name, namespace: .metadata.namespace},
   spec: {selector: .spec.selector, template: {metadata: {labels: .spec.template.metadata.labels},
     spec: {automountServiceAccountToken: false, securityContext: {fsGroup: 1000},
       containers: [$container], volumes: [$volume, {name: "home", emptyDir: {}}],
       initContainers: [{name: "stub-metrics", image: $container.image,
         command: ["python", "-c", "import os; p=os.environ[\"AI_OPS_METRICS_FILE\"]; os.makedirs(os.path.dirname(p), exist_ok=True); open(p, \"w\").write(\"ai_ops_stub_metric 1\\n\")"],
         env: [{name: "AI_OPS_METRICS_FILE", value: $path}], securityContext: $container.securityContext,
         volumeMounts: [{name: "home", mountPath: "/home/boxp"}]}]}}}}' "$tmp/docs.json"

} | jq -s '{apiVersion: "v1", kind: "List", items: .}'
