#!/usr/bin/env bash
set -euo pipefail
: "${REPO_ROOT:?}"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
if [[ -n ${MANIFEST_DIR:-} && -f $MANIFEST_DIR/codex-workspace.yaml ]]; then
  cp "$MANIFEST_DIR/codex-workspace.yaml" "$tmp/render.yaml"
else
  kustomize build "$REPO_ROOT/argoproj/codex-workspace" > "$tmp/render.yaml"
fi
yq -o=json -I=0 'select(.kind == "Deployment" or .kind == "ConfigMap" or .kind == "Service" or .kind == "NetworkPolicy")' "$tmp/render.yaml" | jq -s . > "$tmp/docs.json"
jq -e '
  ([.[] | select(.kind == "Deployment" and .metadata.name == "codex-workspace") | .spec.template.spec.containers[] | select(.name == "lolice-lineage-web")] | length == 1) and
  ([.[] | select(.kind == "Service" and .metadata.name == "lolice-lineage") | select(.spec.type == "ClusterIP" or .spec.type == null) | select(.spec.selector.app == "codex-workspace" and .spec.ports == [{"name":"lineage-web","port":8091,"protocol":"TCP","targetPort":8091}])] | length == 1) and
  ([.[] | select(.kind == "NetworkPolicy" and .metadata.name == "codex-workspace-network-policy") | .spec.ingress[] | select(.destination.ports | index(8091))] | length == 1) and
  ([.[] | select(.kind == "NetworkPolicy" and .metadata.name == "codex-workspace-network-policy") | .spec.ingress[] | select(.destination.ports | index(8091)) | select(.action == "Allow" and .protocol == "TCP" and .source.namespaceSelector == "kubernetes.io/metadata.name == '\''k8s'\''" and .source.selector == "app == '\''cloudflared'\''" and .destination.ports == [8091])] | length == 1)
' "$tmp/docs.json" >/dev/null || { echo 'lineage render assertions failed' >&2; exit 1; }
jq -e '
  ([.[] | select(.kind == "Deployment" and .metadata.name == "codex-workspace")][0].spec.template.spec) as $p |
  ($p.containers | map(select(.name == "lolice-lineage-web")) | .[0]) as $c |
  ($p.volumes | map(select(.name == "lolice-lineage-config")) | .[0]) as $v |
  ([.[] | select(.kind == "ConfigMap" and .metadata.name == $v.configMap.name)] | length == 1) and
  ($c.volumeMounts | any(.name == "home" and .subPath == "Documents/obsidian-headless/BOXP/Projects/lolice-infrastructure-lineage" and .mountPath == "/srv/lineage" and .readOnly == true)) and
  ($c.securityContext.runAsUser == 1000 and $c.securityContext.runAsGroup == 1000 and $c.securityContext.readOnlyRootFilesystem == true and $c.readinessProbe == null)
' "$tmp/docs.json" >/dev/null || { echo 'sidecar configuration assertions failed' >&2; exit 1; }
# Use real resources and preserve the real home subPath; only namespace and home volume change.
jq -c '
  ([.[] | select(.kind == "Deployment" and .metadata.name == "codex-workspace")][0].spec.template.spec) as $p |
  ($p.containers | map(select(.name == "lolice-lineage-web")) | .[0]) as $c |
  ($p.volumes | map(select(.name == "lolice-lineage-config" or .name == "lolice-lineage-tmp"))) as $v |
  (.[] | select((.kind == "ConfigMap" and .metadata.name == ($v[] | select(.name == "lolice-lineage-config") | .configMap.name)) or (.kind == "Service" and .metadata.name == "lolice-lineage") or (.kind == "NetworkPolicy" and .metadata.name == "codex-workspace-network-policy")) | .metadata.namespace = "lineage-test"),
  {apiVersion:"v1",kind:"Pod",metadata:{name:"lineage",namespace:"lineage-test",labels:{app:"codex-workspace"}},spec:{automountServiceAccountToken:false,securityContext:{fsGroup:1000},containers:[$c,{name:"seed-helper",image:"busybox:1.37",command:["sh","-c","while true; do nc -l -p 9999 -e echo; done"],volumeMounts:[{name:"home",mountPath:"/home"}]}],volumes:($v + [{name:"home",emptyDir:{}}]),initContainers:[{name:"seed",image:"busybox:1.37",command:["sh","-c","d=/home/Documents/obsidian-headless/BOXP/Projects/lolice-infrastructure-lineage; mkdir -p $d/data $d/tools; printf synthetic-lineage-marker > $d/lolice-lineage.html; echo synthetic-secret-marker > $d/request.md; cp $d/request.md $d/owner-summary.md; cp $d/request.md $d/data/secret.json; cp $d/request.md $d/tools/x.sh"],volumeMounts:[{name:"home",mountPath:"/home"}]}]}}
' "$tmp/docs.json" | jq -s '{apiVersion:"v1",kind:"List",items:.}' > "$tmp/minimal.json"
kubectl create namespace lineage-test --dry-run=client -o yaml | kubectl apply -f -
kubectl create namespace lineage-client --dry-run=client -o yaml | kubectl apply -f -
kubectl create namespace k8s --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f "$tmp/minimal.json"
for ns in k8s lineage-client; do
  kubectl -n "$ns" run lineage-probe --image=curlimages/curl:8.17.0 --labels=app=cloudflared --command -- sleep 1800
  kubectl -n "$ns" wait --for=condition=Ready pod/lineage-probe --timeout=180s
done
kubectl -n lineage-test wait --for=condition=Ready pod/lineage --timeout=180s
kubectl -n lineage-test exec lineage -c lolice-lineage-web -- nginx -t
kubectl -n lineage-test exec lineage -c lolice-lineage-web -- sh -c 'test "$(id -u)" = 1000; ! touch /root-test; ! touch /srv/lineage/write-test'
url=http://lolice-lineage.lineage-test.svc.cluster.local:8091
probe() { kubectl -n k8s exec lineage-probe -- curl -sS --path-as-is --max-time 5 "$@"; }
probe -D - -o /dev/null "$url/" > "$tmp/headers"
probe "$url/" > "$tmp/body"
test "$(sha256sum "$tmp/body" | cut -d' ' -f1)" = "$(printf synthetic-lineage-marker | sha256sum | cut -d' ' -f1)"
for header in 'HTTP/1.1 200' 'Content-Type: text/html; charset=utf-8' 'Cache-Control: private, no-store' 'X-Robots-Tag: noindex, nofollow' 'X-Content-Type-Options: nosniff' 'Referrer-Policy: no-referrer' "Content-Security-Policy: frame-ancestors 'none'"; do grep -Fi "$header" "$tmp/headers" >/dev/null; done
test "$(probe -I -o /dev/null -w '%{http_code}' "$url/")" = 200
code=$(probe -X POST -o /dev/null -w '%{http_code}' "$url/"); [[ $code == 403 || $code == 405 ]]
for path in /lolice-lineage.html /request.md /owner-summary.md /data/secret.json /tools/x.sh '/%2e%2e/request.md' '//request.md' '/%2e%2e%2frequest.md'; do
  code=$(probe -o /dev/null -w '%{http_code}' "$url$path"); test "$code" = 404
  probe "$url$path" > "$tmp/error"
  ! grep -E 'synthetic-(lineage|secret)-marker' "$tmp/error"
done
if [[ ${CHAINSAW_NETWORK_POLICY_TEST:-false} == true ]]; then
  # DNS is checked independently; a timeout (not connection refused) proves a policy drop.
  pod_ip=$(kubectl -n lineage-test get pod lineage -o jsonpath="{.status.podIP}")
  if kubectl -n k8s exec lineage-probe -- curl -sS --connect-timeout 3 "http://$pod_ip:9999/"; then echo "unexpected non-8091 access" >&2; exit 1; else test "$?" = 28; fi
  kubectl -n lineage-client exec lineage-probe -- nslookup lolice-lineage.lineage-test.svc.cluster.local >/dev/null
  if kubectl -n lineage-client exec lineage-probe -- curl -sS --connect-timeout 3 "$url/"; then echo 'unexpected network access' >&2; exit 1; else test "$?" = 28; fi
fi
# Remove through a temporary writable fixture mount, preserving the sidecar process.
kubectl -n lineage-test exec lineage -c lolice-lineage-web -- nginx -t
before=$(kubectl -n lineage-test get pod lineage -o jsonpath='{.status.containerStatuses[?(@.name=="lolice-lineage-web")].restartCount}')
# A debug container with the same emptyDir is avoided: seed container remains as a writable helper.
kubectl -n lineage-test exec lineage -c seed-helper -- rm /home/Documents/obsidian-headless/BOXP/Projects/lolice-infrastructure-lineage/lolice-lineage.html
test "$(probe -o /dev/null -w '%{http_code}' "$url/")" = 404
test "$(kubectl -n lineage-test get pod lineage -o jsonpath='{.status.containerStatuses[?(@.name=="lolice-lineage-web")].restartCount}')" = "$before"
echo 'lolice-lineage checks passed'
