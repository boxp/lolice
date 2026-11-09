#!/usr/bin/env bash
# Chainsaw suite の先頭 step と変更を行う補助 script が共有する隔離ガード。
# 通過後に実行される操作はクラスタを変更するので、kind の名前だけでなく独立に export
# した kubeconfig と実際の接続先 API server の一致まで確認する。
#   --loopback: 接続先が kind のローカル API server (127.0.0.1 / localhost) であることも要求する。
set -eu

require_loopback=false
case "${1:-}" in
  "") ;;
  --loopback) require_loopback=true ;;
  *) echo "usage: require-isolated-kind.sh [--loopback]" >&2; exit 2 ;;
esac

case "${CHAINSAW_KIND_CONTEXT:-}" in
  kind-*) ;;
  *) echo "An explicit isolated kind context is required; this suite is not read-only." >&2; exit 1 ;;
esac
: "${CHAINSAW_KIND_KUBECONFIG:?An isolated kubeconfig is required}"
test -r "$CHAINSAW_KIND_KUBECONFIG"
test "$(kubectl --kubeconfig "$CHAINSAW_KIND_KUBECONFIG" config current-context)" = "$CHAINSAW_KIND_CONTEXT"
expected_server="$(kubectl --kubeconfig "$CHAINSAW_KIND_KUBECONFIG" config view --minify -o jsonpath='{.clusters[0].cluster.server}')"
actual_server="$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')"
test -n "$expected_server"
test "$actual_server" = "$expected_server"
if [ "$require_loopback" = true ]; then
  case "$expected_server" in
    https://127.0.0.1:*|https://localhost:*) ;;
    *) echo "kind API server must be local" >&2; exit 1 ;;
  esac
fi
