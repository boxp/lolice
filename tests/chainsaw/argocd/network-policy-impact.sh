#!/usr/bin/env bash
# Argo CD NetworkPolicy の許可/拒否 matrix。section ごとに chainsaw の step から呼ぶ。
# 「policy 反映中に通った試行を拒否の証拠に数えない」時系列判定と、CHAINSAW_NETWORK_POLICY_TEST
# による preflight (kindnet) の除外が必要なので、この部分は標準 assert へ置き換えない。
set -euo pipefail

if [ "${CHAINSAW_NETWORK_POLICY_TEST:-false}" != true ]; then
  echo "NetworkPolicy通信matrix未実施: NetworkPolicy非対応のkindnet preflight suiteです"
  exit 0
fi

# 専用 kind だけで実行する。kubeconfig と接続先 API server まで照合して外部クラスタへの変更を防ぐ。
"${REPO_ROOT:?REPO_ROOT is required}/tests/workflows/require-isolated-kind.sh" --loopback
export KUBECONFIG="$CHAINSAW_KIND_KUBECONFIG"

SUITE="${REPO_ROOT}/tests/chainsaw/argocd"
repo_service=argocd-repo-server.argocd.svc.cluster.local
redis_service=argocd-redis.argocd.svc.cluster.local

probes=(
  "allowed-server argocd"
  "allowed-repo argocd"
  "allowed-controller argocd"
  "allowed-notifications argocd"
  "allowed-applicationset argocd"
  "denied-same-ns argocd"
  "spoof-cross-ns netpol-spoof"
  "cloudflare-proxy netpol-spoof"
  "tailscale-proxy netpol-spoof"
)

tcp_check() {
  local pod="$1" namespace="$2" service="$3" port="$4" expectation="$5"
  local status result consecutive=0 attempts=30
  for attempt in $(seq 1 "$attempts"); do
    if result="$(kubectl exec -i -n "$namespace" "$pod" -- python - "$service" "$port" "$expectation" < "$SUITE/tcp-check.py" 2>&1)"; then
      status=0
    else
      status=$?
    fi
    if [ "$expectation" = reachable ]; then
      if [ "$status" -eq 0 ]; then
        echo "$(date -u +%FT%TZ) ${namespace}/${pod} -> ${service}:${port} reachable (DNS成功、TCP timeout=2s)"
        return 0
      fi
      if [ "$status" -eq 4 ]; then sleep 2; continue; fi
      echo "${namespace}/${pod} must reach ${service}:${port}: ${result}" >&2
      return 1
    fi
    case "$status" in
      0)
        consecutive=$((consecutive + 1))
        if [ "$consecutive" -ge 2 ]; then
          echo "$(date -u +%FT%TZ) ${namespace}/${pod} -> ${service}:${port} blocked (DNS成功、TCP timeout=2sを2回連続)"
          return 0
        fi
        ;;
      4)
        consecutive=0 # policy反映中に通った試行は拒否の証拠に数えない
        ;;
      *)
        echo "${namespace}/${pod} ${service}:${port} failed without TCP timeout: ${result}" >&2
        return 1
        ;;
    esac
    sleep 2
  done
  echo "${namespace}/${pod} ${service}:${port} did not produce two TCP timeouts" >&2
  return 1
}

create_probes() {
  # 9 個を先に全部作ってから待つ (image pull と起動を重ねる)。
  kubectl apply -f "$SUITE/network-policy-probes.yaml"
  local name namespace
  for probe in "${probes[@]}"; do
    read -r name namespace <<< "$probe"
    kubectl wait -n "$namespace" --for=jsonpath='{.status.phase}'=Running "pod/$name" --timeout=180s
    test "$(kubectl get pod "$name" -n "$namespace" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}')" != "True"
    test "$(kubectl get pod "$name" -n "$namespace" -o jsonpath='{.spec.hostNetwork}')" != "true"
  done
}

baseline_endpoints() {
  echo "baselineの前提: DNS解決とService endpointを確認"
  local service port
  for endpoint in "$repo_service 8081" "$redis_service 6379"; do
    read -r service port <<< "$endpoint"
    kubectl exec -i -n argocd allowed-server -- python - "$service" "$port" resolve < "$SUITE/tcp-check.py" >/dev/null
    test -n "$(kubectl get endpointslice -n argocd -l kubernetes.io/service-name="${service%%.*}" -o jsonpath='{.items[*].endpoints[?(@.conditions.ready==true)].addresses[*]}')"
  done
}

policies_removed_all_probes_reach() {
  # 変更はこのガード済み使い捨てkindクラスタの2 policyに限定。
  kubectl delete networkpolicy argocd-repo-server-network-policy argocd-redis-network-policy -n argocd
  echo "NetworkPolicy除去中: 全probeからService portへのTCP接続が成功することを確認"
  local service port pod
  for endpoint in "$repo_service 8081" "$redis_service 6379"; do
    read -r service port <<< "$endpoint"
    for pod in allowed-server allowed-repo allowed-controller allowed-notifications allowed-applicationset denied-same-ns; do
      tcp_check "$pod" argocd "$service" "$port" reachable
    done
    tcp_check spoof-cross-ns netpol-spoof "$service" "$port" reachable
  done
}

policies_restored_matrix() {
  kustomize build "${REPO_ROOT}/argoproj/argocd" \
    | yq 'select(.kind == "NetworkPolicy" and (.metadata.name == "argocd-repo-server-network-policy" or .metadata.name == "argocd-redis-network-policy"))' \
    | kubectl apply -f -

  echo "復元後: 許可peerのTCP成功、同ns非許可/別ns spoofのTCP拒否を確認"
  tcp_check allowed-server argocd "$repo_service" 8081 reachable
  tcp_check allowed-server argocd "$redis_service" 6379 reachable
  tcp_check allowed-repo argocd "$redis_service" 6379 reachable
  tcp_check allowed-controller argocd "$repo_service" 8081 reachable
  tcp_check allowed-controller argocd "$redis_service" 6379 reachable
  tcp_check allowed-notifications argocd "$repo_service" 8081 reachable
  tcp_check allowed-applicationset argocd "$repo_service" 8081 reachable
  # blocked 判定は 1 件あたり TCP timeout 2s × 2 回連続 + sleep で 6 秒以上かかるため、
  # 4 件を同時に走らせて結果を個別に回収する (直列だと約 24 秒)。
  local blocked_pids=() blocked_failed=0 pid
  tcp_check denied-same-ns argocd "$repo_service" 8081 blocked & blocked_pids+=($!)
  tcp_check denied-same-ns argocd "$redis_service" 6379 blocked & blocked_pids+=($!)
  tcp_check spoof-cross-ns netpol-spoof "$repo_service" 8081 blocked & blocked_pids+=($!)
  tcp_check spoof-cross-ns netpol-spoof "$redis_service" 6379 blocked & blocked_pids+=($!)
  for pid in "${blocked_pids[@]}"; do
    wait "$pid" || blocked_failed=1
  done
  test "$blocked_failed" -eq 0 || { echo "blocked 判定のいずれかが失敗しました" >&2; exit 1; }
  tcp_check spoof-cross-ns netpol-spoof "$repo_service" 8084 reachable

  # serverはupstream policyで全ingress許可。Tunnel proxy相当のcross-namespace
  # clientからserver ServiceのHTTP/HTTPS portが到達可能なことも確認する。
  tcp_check cloudflare-proxy netpol-spoof argocd-server.argocd.svc.cluster.local 443 reachable
  tcp_check cloudflare-proxy netpol-spoof argocd-server.argocd.svc.cluster.local 80 reachable
  tcp_check tailscale-proxy netpol-spoof argocd-server.argocd.svc.cluster.local 443 reachable
}

fixture_resync_and_teardown() {
  # NetworkPolicy復元後も通常のmanifest生成/syncが成功していることを再確認。
  local initial_reconcile current_reconcile refresh
  initial_reconcile="$(kubectl get application netpol-impact-fixture -n argocd -o jsonpath='{.status.reconciledAt}')"
  kubectl annotate application netpol-impact-fixture -n argocd argocd.argoproj.io/refresh=hard --overwrite
  for attempt in $(seq 1 60); do
    current_reconcile="$(kubectl get application netpol-impact-fixture -n argocd -o jsonpath='{.status.reconciledAt}')"
    refresh="$(kubectl get application netpol-impact-fixture -n argocd -o jsonpath='{.metadata.annotations.argocd\.argoproj\.io/refresh}')"
    [ -z "$refresh" ] && [ -n "$current_reconcile" ] && [ "$current_reconcile" != "$initial_reconcile" ] && break
    sleep 5
  done
  test -z "$refresh"
  test "$current_reconcile" != "$initial_reconcile"
  kubectl wait -n argocd --for=jsonpath='{.status.sync.status}'=Synced application/netpol-impact-fixture --timeout=300s
  kubectl wait -n argocd --for=jsonpath='{.status.health.status}'=Healthy application/netpol-impact-fixture --timeout=300s
  test "$(kubectl get configmap appset-demo -n netpol-impact -o jsonpath='{.data.source}')" = app-a
  kubectl delete application netpol-impact-fixture -n argocd --wait=true --timeout=180s
  kubectl delete namespace netpol-impact netpol-spoof --wait=true --timeout=180s
  echo "Argo CD NetworkPolicy impact matrix passed"
}

"${1:?section is required}"
