#!/usr/bin/env bash
set -euo pipefail

# Grafana Alloy -> Loki のログ収集経路を隔離 kind クラスターで動的に検証する。
#
# BOXP-254 の再発防止テスト。loki.source.file は __path__ の glob を展開しないため、
# local.file_match を挟まない設定は 1 ファイルも tail せず（エラーも出さず）
# Loki に 1 行も届かなくなる。この suite は本番の ConfigMap と Helm values を
# そのまま使い、全ノードのコンテナログが Loki に到達することを確認する。
#
# Loki 本体は使い捨ての single-binary (loki-fixture.yaml) を
# Service monitoring/loki-gateway として立て、ConfigMap 内の push URL を
# 変更せずに済むようにしている。

# このテストは専用kindクラスター内だけで実行する。名前だけでなく
# kubeconfigと接続先API Serverも照合し、外部クラスタへの変更を防ぐ。
expected_context="${CHAINSAW_KIND_CONTEXT:-}"
kubeconfig="${CHAINSAW_KIND_KUBECONFIG:-}"
case "$expected_context" in kind-*) ;; *) echo "An explicit kind context is required" >&2; exit 1 ;; esac
test -n "$kubeconfig" && test -r "$kubeconfig"
test "$(kubectl --kubeconfig "$kubeconfig" config current-context)" = "$expected_context"
expected_server="$(kubectl --kubeconfig "$kubeconfig" config view --minify -o jsonpath='{.clusters[0].cluster.server}')"
actual_server="$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')"
test -n "$expected_server" && test "$actual_server" = "$expected_server"
case "$expected_server" in https://127.0.0.1:*|https://localhost:*) ;; *) echo "kind API server must be local" >&2; exit 1 ;; esac
export KUBECONFIG="$kubeconfig"

REPO_ROOT="${REPO_ROOT:?REPO_ROOT is required}"
COMPONENT_DIR="${REPO_ROOT}/argoproj/grafana-alloy"
FIXTURE_DIR="${REPO_ROOT}/tests/chainsaw/grafana-alloy"
NS=monitoring
RELEASE=grafana-alloy   # Argo CD Application 名 = Helm release 名
CURL_POD=chainsaw-curl
LOKI_URL=http://loki-gateway.${NS}.svc.cluster.local

# 失敗時の診断は隔離クラスター内の Alloy / Loki のログと状態だけ。
diagnose_failure() {
  test "$?" -eq 0 && return
  echo "=== 診断: DaemonSet / Pod 状態 ==="
  kubectl get daemonset -n "$NS" || true
  kubectl get pods -n "$NS" -o wide || true
  echo "=== 診断: Alloy ログ ==="
  kubectl logs -n "$NS" "daemonset/$RELEASE" -c alloy --all-pods --tail=80 2>/dev/null \
    || kubectl logs -n "$NS" "daemonset/$RELEASE" -c alloy --tail=80 || true
  echo "=== 診断: Loki ログ ==="
  kubectl logs -n "$NS" deployment/chainsaw-loki --tail=60 || true
}
trap diagnose_failure EXIT

echo "=== 1. ConfigMap の配線を静的に確認 (local.file_match が glob を展開すること) ==="
kubectl apply --server-side --force-conflicts -f "${COMPONENT_DIR}/configmap.yaml"
config="$(kubectl get configmap alloy-config -n "$NS" -o jsonpath='{.data.config\.alloy}')"
grep -Eq '^\s*local\.file_match\s+"pods"\s*\{' <<<"$config" \
  || { echo 'alloy-config に local.file_match "pods" がありません' >&2; exit 1; }
grep -Eq '^\s*path_targets\s*=\s*discovery\.relabel\.pods\.output' <<<"$config" \
  || { echo 'local.file_match.pods が discovery.relabel.pods.output を受けていません' >&2; exit 1; }
grep -Eq '^\s*targets\s*=\s*local\.file_match\.pods\.targets' <<<"$config" \
  || { echo 'loki.source.file.pods の targets が local.file_match.pods.targets ではありません (glob が展開されず tail 0 件になります)' >&2; exit 1; }
if grep -Eq '^\s*targets\s*=\s*discovery\.relabel\.pods\.output' <<<"$config"; then
  echo 'loki.source.file が discovery.relabel.pods.output を直接受けています (BOXP-254 の退行)' >&2
  exit 1
fi
grep -Eq "url\s*=\s*\"${LOKI_URL}/loki/api/v1/push\"" <<<"$config" \
  || { echo "loki.write の URL が ${LOKI_URL}/loki/api/v1/push ではありません。fixture の Service 名と合わせてください" >&2; exit 1; }

echo "=== 2. 使い捨て Loki を loki-gateway Service として起動 ==="
kubectl apply --server-side --force-conflicts -f "${FIXTURE_DIR}/loki-fixture.yaml"
kubectl rollout status deployment/chainsaw-loki -n "$NS" --timeout=240s

echo "=== 3. Alloy chart を本番 values で install ==="
chart_repo="$(yq -r '.spec.sources[] | select(.chart == "alloy") | .repoURL' "${COMPONENT_DIR}/application.yaml")"
chart_version="$(yq -r '.spec.sources[] | select(.chart == "alloy") | .targetRevision' "${COMPONENT_DIR}/application.yaml")"
[[ "$chart_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
  echo 'alloy chart targetRevision must be a fixed chart version.' >&2
  exit 1
}
test -n "$chart_repo"
helm upgrade --install "$RELEASE" alloy \
  --repo "$chart_repo" --version "$chart_version" \
  --namespace "$NS" \
  --values "${COMPONENT_DIR}/helm/values.yaml" \
  --wait --timeout 300s

node_count="$(kubectl get nodes --no-headers | wc -l)"
desired="$(kubectl get daemonset "$RELEASE" -n "$NS" -o jsonpath='{.status.desiredNumberScheduled}')"
ready="$(kubectl get daemonset "$RELEASE" -n "$NS" -o jsonpath='{.status.numberReady}')"
echo "nodes=${node_count} desired=${desired} ready=${ready}"
test "$desired" -eq "$node_count" || { echo "Alloy DaemonSet が全ノードに配置されていません (tolerations を確認)" >&2; exit 1; }
test "$ready" -eq "$desired" || { echo "Alloy DaemonSet の Pod が Ready ではありません" >&2; exit 1; }

echo "=== 4. クラスター内 curl Pod を用意 ==="
cat <<YAML | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: ${CURL_POD}
  namespace: ${NS}
spec:
  restartPolicy: Never
  containers:
    - name: curl
      image: curlimages/curl:8.14.1
      command: ["sleep", "1800"]
YAML
kubectl wait -n "$NS" --for=condition=Ready "pod/${CURL_POD}" --timeout=180s

incurl() {
  kubectl exec -n "$NS" "$CURL_POD" -- curl -sS --max-time 20 "$@"
}
loki_streams() {
  # query_range の結果ストリーム数を返す。$1...: --data-urlencode 引数
  incurl -G "${LOKI_URL}/loki/api/v1/query_range" "$@" | yq -p=json -r '.data.result | length'
}

echo "=== 5. 全ノードのコンテナログが Loki に届くまで待機 ==="
mapfile -t nodes < <(kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')
deadline=$((SECONDS + 300))
while :; do
  missing=()
  for node in "${nodes[@]}"; do
    n="$(loki_streams --data-urlencode "query={node=\"${node}\"}" --data-urlencode limit=1 2>/dev/null || echo 0)"
    [ "${n:-0}" -gt 0 ] || missing+=("$node")
  done
  if [ "${#missing[@]}" -eq 0 ]; then
    break
  fi
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo "Loki にログが届いていないノード: ${missing[*]}" >&2
    echo "labels: $(incurl -G "${LOKI_URL}/loki/api/v1/labels" || true)" >&2
    exit 1
  fi
  echo "waiting... (no logs yet from: ${missing[*]})"
  sleep 10
done
echo "全ノード (${nodes[*]}) のログが Loki に到達"

echo "=== 6. ラベルと monitoring namespace のログを確認 ==="
labels="$(incurl -G "${LOKI_URL}/loki/api/v1/labels")"
echo "labels: ${labels}"
for label in namespace pod container node job; do
  yq -p=json -e ".data[] | select(. == \"${label}\")" <<<"$labels" >/dev/null \
    || { echo "Loki に label '${label}' がありません" >&2; exit 1; }
done
n="$(loki_streams --data-urlencode 'query={namespace="monitoring"}' --data-urlencode limit=5)"
test "$n" -gt 0 || { echo '{namespace="monitoring"} のログが 0 件です' >&2; exit 1; }
incurl -G "${LOKI_URL}/loki/api/v1/query_range" --data-urlencode 'query={namespace="monitoring"}' --data-urlencode limit=3 \
  | yq -p=json '.data.result[] | {"stream": .stream, "first_line": .values[0][1]}'

echo "=== 7. Alloy 自身のメトリクスで tail 対象ファイル数と送信エントリ数を確認 ==="
# ServiceMonitor (servicemonitor.yaml) が Prometheus に取り込ませる予定のメトリクス。
# 本番で「ログ 0 件」を検知する指標なので、kind でも 0 より大きいことを確認する。
mapfile -t alloy_pods < <(kubectl get pods -n "$NS" -l "app.kubernetes.io/name=alloy,app.kubernetes.io/instance=${RELEASE}" \
  -o jsonpath='{range .items[*]}{.metadata.name}={.status.podIP}{"\n"}{end}')
test "${#alloy_pods[@]}" -eq "$node_count"
for entry in "${alloy_pods[@]}"; do
  pod="${entry%%=*}"; ip="${entry#*=}"
  metrics="$(incurl "http://${ip}:12345/metrics")"
  files_active="$(awk '/^loki_source_file_files_active_total/ {print $2}' <<<"$metrics" | head -n1)"
  sent="$(awk '/^loki_write_sent_entries_total/ {sum += $2} END {print sum + 0}' <<<"$metrics")"
  echo "${pod}: loki_source_file_files_active_total=${files_active:-<missing>} loki_write_sent_entries_total=${sent}"
  test -n "$files_active" && awk -v v="$files_active" 'BEGIN { exit !(v > 0) }' \
    || { echo "${pod} が tail しているファイルが 0 件です (glob が展開されていません)" >&2; exit 1; }
  awk -v v="$sent" 'BEGIN { exit !(v > 0) }' \
    || { echo "${pod} から Loki へ送信されたエントリが 0 件です" >&2; exit 1; }
done

echo "grafana-alloy 動的検証: 全ノードのコンテナログが local.file_match 経由で Loki に到達"
