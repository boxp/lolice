#!/usr/bin/env bash
set -euo pipefail
: "${KUBECONFIG:?KUBECONFIG is required}"
DEFERRED_DIR="${1:-${DEFERRED_DIR:?DEFERRED_DIR is required}}"
eso_started=$SECONDS
if ! kubectl wait deployment --all -n external-secrets --for=condition=Available --timeout=180s; then
  echo "ESO Deployment が Available になりませんでした" >&2
  kubectl get pods -n external-secrets -o wide >&2 || true
  kubectl logs -n external-secrets deployment/external-secrets-webhook --tail=20 >&2 || true
  exit 1
fi
echo "ESO Available まで $((SECONDS - eso_started))s"
for DEFERRED in "$DEFERRED_DIR"/*-eso.yaml; do
  [ -s "$DEFERRED" ] || continue
  echo "=== $(basename "$DEFERRED") を適用 (ESO webhook 経由) ==="
  # Available 直後は webhook の CA 注入が追いついていないことがあるので短く再試行する。
  for attempt in $(seq 1 12); do
    if kubectl apply --server-side --force-conflicts -f - < "$DEFERRED"; then break; fi
    [ "$attempt" -lt 12 ] || { echo "ESO webhook 経由の適用に失敗しました: $DEFERRED" >&2; exit 1; }
    sleep 5
  done
done
