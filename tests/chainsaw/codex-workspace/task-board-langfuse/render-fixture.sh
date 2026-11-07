#!/usr/bin/env bash
set -euo pipefail
: "${REPO_ROOT:?}"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
# The digest pins the fetched contents of this merged revision (arch#14473, arch#14489, arch#14495, arch#14545, arch#14548, arch#14596).
arch_revision=9982b1cd019b36b0655e1a27614517fe84c4829a
curl -fsSL "https://raw.githubusercontent.com/boxp/arch/${arch_revision}/docker/codex-workspace/task-board/langfuse_exporter.bb" > "$tmp/exporter.bb"
echo "970c4827b8488b6bb59d6e3303875c8b4b3b9fd2e41ba04691c51e532ad6df47  $tmp/exporter.bb" | sha256sum -c >&2
if [[ -n ${MANIFEST_DIR:-} && -f $MANIFEST_DIR/codex-workspace.yaml ]]; then
  cp "$MANIFEST_DIR/codex-workspace.yaml" "$tmp/render.yaml"
else
  kustomize build "$REPO_ROOT/argoproj/codex-workspace" > "$tmp/render.yaml"
fi
yq -o=json -I=0 'select(.kind == "Deployment" and .metadata.name == "codex-workspace")' "$tmp/render.yaml" > "$tmp/deploy.json"
jq -e '
  .spec.template.spec as $p |
  ([$p.containers[] | select(.name == "task-board-langfuse-exporter")] | length == 1) and
  ([$p.volumes[] | select(.name == "langfuse-ingest-key" and .secret.optional == true and .secret.secretName == "langfuse-ingest-key")] | length == 1)
' "$tmp/deploy.json" >/dev/null || { echo 'FAIL: production exporter or optional key volume is missing' >&2; exit 1; }
# Fixture substitutions: public bb image, local sink (closes h2c upgrades like Langfuse), short polling and synthetic env;
# the executable, sidecar command, mounts, Secret volume and directory init come from production.
# Like a new PVC, home holds no exporter directories and is not world-writable, so kubelet
# creates the subPath directories as root unless the production init prepares them first.
jq -n --slurpfile d "$tmp/deploy.json" --rawfile exporter "$tmp/exporter.bb" --rawfile sink "$REPO_ROOT/tests/chainsaw/hermes-agent/otlp-sink.py" '
  $d[0].spec.template.spec as $p |
  ($p.containers[] | select(.name == "task-board-langfuse-exporter") |
    .image = "docker.io/babashka/babashka:1.13.219" | .imagePullPolicy = "IfNotPresent" |
    .env |= map(if .name == "LANGFUSE_URL" then .value = "http://sink:8080"
                elif .name == "LANGFUSE_POLL_SECONDS" then .value = "2"
                elif .name == "GEMINI_API_KEY" then del(.valueFrom) | .value = "syntheticgeminivalue428"
                elif .name == "GRAFANA_SERVICE_ACCOUNT_TOKEN" then del(.valueFrom) | .value = "syntheticgrafanavalue428"
                else . end) |
    .volumeMounts += [{name:"scripts",mountPath:"/opt/codex-workspace/task-board",readOnly:true}]) as $c |
  [$p.volumes[] | select(.name == "langfuse-ingest-key" or .name == "task-board-langfuse-tmp")] as $v |
  {apiVersion:"v1",kind:"List",items:[
    {apiVersion:"v1",kind:"Namespace",metadata:{name:"task-board-langfuse-fixture"}},
    {apiVersion:"v1",kind:"ConfigMap",metadata:{name:"scripts",namespace:"task-board-langfuse-fixture"},data:{"langfuse_exporter.bb":$exporter,"otlp-sink.py":$sink}},
    {apiVersion:"v1",kind:"Service",metadata:{name:"sink",namespace:"task-board-langfuse-fixture"},spec:{selector:{app:"sink"},ports:[{port:8080}]}},
    {apiVersion:"apps/v1",kind:"Deployment",metadata:{name:"sink",namespace:"task-board-langfuse-fixture"},spec:{replicas:1,selector:{matchLabels:{app:"sink"}},template:{metadata:{labels:{app:"sink"}},spec:{containers:[{name:"sink",image:"python:3.13-alpine",command:["python","-u","/scripts/otlp-sink.py"],volumeMounts:[{name:"scripts",mountPath:"/scripts"}]}],volumes:[{name:"scripts",configMap:{name:"scripts"}}]}}}},
    {apiVersion:"v1",kind:"Pod",metadata:{name:"exporter",namespace:"task-board-langfuse-fixture"},spec:{automountServiceAccountToken:false,initContainers:([{name:"seed",image:"busybox:1.37",command:["sh","-c","mkdir /home/.ssh; echo synthetic > /home/.ssh/marker; chown -R 1000:1000 /home; chmod 755 /home"],volumeMounts:[{name:"home",mountPath:"/home"}]}] + [$p.initContainers[] | select(.name == "prepare-task-board-langfuse-dirs")]),containers:[$c,{name:"helper",image:"python:3.13-alpine",command:["sleep","3600"],volumeMounts:[{name:"home",mountPath:"/home"}]}],volumes:($v + [{name:"home",emptyDir:{}},{name:"scripts",configMap:{name:"scripts"}}])}}
  ]}'
