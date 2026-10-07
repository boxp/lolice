#!/usr/bin/env bash
set -euo pipefail

# Chainsawのpreflightも標準kindnetで実施するため、fixture検証は
# NetworkPolicy enforcement matrixとは独立して全suiteで行う。
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

cat <<'YAML' | kubectl apply -f -
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: netpol-impact-fixture
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://github.com/argoproj/argocd-example-apps.git
    targetRevision: 8088f4c0d970abb09e250248cc97e35623447cb5
    path: guestbook
  destination:
    server: https://kubernetes.default.svc
    namespace: netpol-impact
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
YAML

echo "隔離kind内のfixture Applicationでmanifest生成と通常syncを確認"
kubectl wait -n argocd --for=jsonpath='{.status.sync.status}'=Synced application/netpol-impact-fixture --timeout=600s
kubectl wait -n argocd --for=jsonpath='{.status.health.status}'=Healthy application/netpol-impact-fixture --timeout=600s
kubectl rollout status deployment/guestbook-ui -n netpol-impact --timeout=300s
test -n "$(kubectl get application netpol-impact-fixture -n argocd -o jsonpath='{.status.reconciledAt}')"
