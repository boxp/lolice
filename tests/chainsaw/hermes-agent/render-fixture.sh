#!/usr/bin/env bash
set -euo pipefail
: "${REPO_ROOT:?REPO_ROOT is required}"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
if [ -n "${MANIFEST_DIR:-}" ] && [ -f "$MANIFEST_DIR/hermes-agent.yaml" ]; then
  cp "$MANIFEST_DIR/hermes-agent.yaml" "$tmp/render.yaml"
else
  kustomize build "$REPO_ROOT/argoproj/hermes-agent" > "$tmp/render.yaml"
fi
yq -o=json -I=0 'select((.kind == "ConfigMap" and (.metadata.name | test("^hermes-agent-boxp-langfuse-observer-plugin-"))) or (.kind == "Deployment" and .metadata.name == "hermes-agent"))' "$tmp/render.yaml" | jq -s '.' > "$tmp/docs.json"
jq -e '
  ([.[] | select(.kind == "Deployment")] | length == 1) and
  ([.[] | select(.kind == "ConfigMap")] | length == 1) and
  ((.[ ] | select(.kind == "Deployment") | .spec.template.spec) as $pod |
  ([$pod.volumes[] | select(.name == "boxp-langfuse-observer-plugin")] | length == 1) and
  ([$pod.volumes[] | select(.name == "langfuse-ingest-key" and .secret.secretName == "langfuse-ingest-key" and .secret.optional == true)] | length == 1) and
  ([$pod.containers[] | select(.name == "hermes-agent") | .volumeMounts[] | select(.name == "langfuse-ingest-key" and .mountPath == "/etc/langfuse-ingest")] | length == 1) and
  ($pod.securityContext.fsGroup != null) and
  any(.[]; .kind == "ConfigMap" and .metadata.namespace == "hermes-agent" and .metadata.name == ($pod.volumes[] | select(.name == "boxp-langfuse-observer-plugin") | .configMap.name)))
' "$tmp/docs.json" >/dev/null || { echo 'production plugin references or key volume are invalid' >&2; exit 1; }
jq -n --slurpfile docs "$tmp/docs.json" --rawfile driver "$REPO_ROOT/tests/chainsaw/hermes-agent/plugin-driver.py" --rawfile sink "$REPO_ROOT/tests/chainsaw/hermes-agent/otlp-sink.py" '
  ($docs[0][] | select(.kind == "Deployment") | .spec.template.spec) as $pod |
  ($pod.containers[] | select(.name == "hermes-agent") | .volumeMounts[] | select(.name == "langfuse-ingest-key")) as $keyMount |
  ($pod.volumes[] | select(.name == "langfuse-ingest-key")) as $keyVolume |
  ($pod.volumes[] | select(.name == "boxp-langfuse-observer-plugin")) as $pluginVolume |
  {apiVersion:"v1", kind:"List", items:[
    {apiVersion:"v1", kind:"Namespace", metadata:{name:"hermes-langfuse-fixture"}},
    ($docs[0][] | select(.kind == "ConfigMap") | .metadata.namespace = "hermes-langfuse-fixture"),
    {apiVersion:"v1", kind:"ConfigMap", metadata:{name:"fixture-scripts", namespace:"hermes-langfuse-fixture"}, data:{"plugin-driver.py":$driver, "otlp-sink.py":$sink}},
    {apiVersion:"v1", kind:"Service", metadata:{name:"sink", namespace:"hermes-langfuse-fixture"}, spec:{selector:{app:"sink"}, ports:[{port:8080}]}},
    {apiVersion:"apps/v1", kind:"Deployment", metadata:{name:"sink", namespace:"hermes-langfuse-fixture"}, spec:{replicas:1, selector:{matchLabels:{app:"sink"}}, template:{metadata:{labels:{app:"sink"}}, spec:{automountServiceAccountToken:false, containers:[{name:"sink", image:"python:3.13-alpine", command:["python", "-u", "/scripts/otlp-sink.py"], volumeMounts:[{name:"scripts", mountPath:"/scripts"}]}], volumes:[{name:"scripts", configMap:{name:"fixture-scripts"}}]}}}},
    {apiVersion:"apps/v1", kind:"Deployment", metadata:{name:"driver", namespace:"hermes-langfuse-fixture"}, spec:{replicas:1, selector:{matchLabels:{app:"driver"}}, template:{metadata:{labels:{app:"driver"}}, spec:{automountServiceAccountToken:false, securityContext:{runAsUser:1000, runAsGroup:10000, fsGroup:$pod.securityContext.fsGroup}, containers:[{name:"driver", image:"python:3.13-alpine", command:["python", "-u", "/scripts/plugin-driver.py"], env:[{name:"LANGFUSE_URL",value:"http://sink:8080"}, {name:"LANGFUSE_INGEST_KEY_DIR",value:$keyMount.mountPath}], volumeMounts:[$keyMount, {name:"boxp-langfuse-observer-plugin",mountPath:"/plugin"}, {name:"scripts",mountPath:"/scripts"}]}], volumes:[$keyVolume,$pluginVolume,{name:"scripts",configMap:{name:"fixture-scripts"}}]}}}}
  ]}'
