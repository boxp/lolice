#!/usr/bin/env bash
set -euo pipefail

if [ "${CHAINSAW_NETWORK_POLICY_TEST:-false}" != true ]; then
  echo "NetworkPolicy通信matrix未実施: NetworkPolicy非対応のkindnet preflight suiteです"
  exit 0
fi

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

kubectl create namespace netpol-spoof --dry-run=client -o yaml | kubectl apply -f -
initial_reconcile="$(kubectl get application netpol-impact-fixture -n argocd -o jsonpath='{.status.reconciledAt}')"
test -n "$initial_reconcile"

# same namespaceの許可ラベルを持つPodと通常Pod、別namespaceから同じ許可ラベルを
# 試すPodを作る。DNSが引ける状態でService portのTCPだけを比較する。
probe() {
  local name="$1" namespace="$2" labels="$3"
  cat <<YAML | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: ${name}
  namespace: ${namespace}
  labels:
    ${labels%%=*}: "${labels#*=}"
spec:
  hostNetwork: false
  containers:
    - name: probe
      image: python:3.13-alpine
      command: ["python", "-c", "import time; time.sleep(1800)"]
      readinessProbe:
        exec:
          command: ["false"]
        periodSeconds: 5
YAML
  kubectl wait -n "$namespace" --for=jsonpath='{.status.phase}'=Running "pod/$name" --timeout=180s
  test "$(kubectl get pod "$name" -n "$namespace" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}')" != "True"
  test "$(kubectl get pod "$name" -n "$namespace" -o jsonpath='{.spec.hostNetwork}')" != "true"
}

probe allowed-server argocd app.kubernetes.io/name=argocd-server
probe allowed-repo argocd app.kubernetes.io/name=argocd-repo-server
probe allowed-controller argocd app.kubernetes.io/name=argocd-application-controller
probe allowed-notifications argocd app.kubernetes.io/name=argocd-notifications-controller
probe allowed-applicationset argocd app.kubernetes.io/name=argocd-applicationset-controller
probe denied-same-ns argocd app.kubernetes.io/name=network-policy-probe
probe spoof-cross-ns netpol-spoof app.kubernetes.io/name=argocd-server
probe cloudflare-proxy netpol-spoof app=cloudflared
probe tailscale-proxy netpol-spoof tailscale.com/managed=true

repo_service=argocd-repo-server.argocd.svc.cluster.local
redis_service=argocd-redis.argocd.svc.cluster.local

tcp_check() {
  local pod="$1" namespace="$2" service="$3" port="$4" expectation="$5"
  local status result consecutive=0 attempts=30
  [ "$expectation" = blocked ] && attempts=30
  for attempt in $(seq 1 "$attempts"); do
    if result="$(kubectl exec -n "$namespace" "$pod" -- python -c '
import socket, sys
host, port, expected = sys.argv[1], int(sys.argv[2]), sys.argv[3]
try:
    socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
except Exception as exc:
    print(f"DNS failure: {exc}", file=sys.stderr)
    sys.exit(3)
try:
    conn = socket.create_connection((host, port), timeout=2)
    conn.close()
except TimeoutError:
    sys.exit(0 if expected == "blocked" else 4)
except OSError as exc:
    print(f"TCP failed without policy-drop timeout: {exc}", file=sys.stderr)
    sys.exit(5)
else:
    sys.exit(4 if expected == "blocked" else 0)
' "$service" "$port" "$expectation" 2>&1)"; then
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

echo "baselineの前提: DNS解決とService endpointを確認"
for endpoint in "$repo_service 8081" "$redis_service 6379"; do
  read -r service port <<< "$endpoint"
  kubectl exec -n argocd allowed-server -- python -c 'import socket, sys; socket.getaddrinfo(sys.argv[1], None, type=socket.SOCK_STREAM)' "$service" >/dev/null
  test -n "$(kubectl get endpointslice -n argocd -l kubernetes.io/service-name="${service%%.*}" -o jsonpath='{.items[*].endpoints[?(@.conditions.ready==true)].addresses[*]}')"
done

# 変更はこのガード済み使い捨てkindクラスタの2 policyに限定。
kubectl delete networkpolicy argocd-repo-server-network-policy argocd-redis-network-policy -n argocd
echo "NetworkPolicy除去中: 全probeからService portへのTCP接続が成功することを確認"
for endpoint in "$repo_service 8081" "$redis_service 6379"; do
  read -r service port <<< "$endpoint"
  for pod in allowed-server allowed-repo allowed-controller allowed-notifications allowed-applicationset denied-same-ns; do
    tcp_check "$pod" argocd "$service" "$port" reachable
  done
  tcp_check spoof-cross-ns netpol-spoof "$service" "$port" reachable
done

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
tcp_check denied-same-ns argocd "$repo_service" 8081 blocked
tcp_check denied-same-ns argocd "$redis_service" 6379 blocked
tcp_check spoof-cross-ns netpol-spoof "$repo_service" 8081 blocked
tcp_check spoof-cross-ns netpol-spoof "$redis_service" 6379 blocked
tcp_check spoof-cross-ns netpol-spoof "$repo_service" 8084 reachable

# serverはupstream policyで全ingress許可。Tunnel proxy相当のcross-namespace
# clientからserver ServiceのHTTP/HTTPS portが到達可能なことも確認する。
tcp_check cloudflare-proxy netpol-spoof argocd-server.argocd.svc.cluster.local 443 reachable
tcp_check cloudflare-proxy netpol-spoof argocd-server.argocd.svc.cluster.local 80 reachable
tcp_check tailscale-proxy netpol-spoof argocd-server.argocd.svc.cluster.local 443 reachable

# NetworkPolicy復元後も通常のmanifest生成/syncが成功していることを再確認。
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
