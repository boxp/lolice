#!/usr/bin/env bash
# Hermes Langfuse observer の時系列シナリオ。section ごとに chainsaw の step から呼ぶ。
# Pod の同一性 (uid/restart/phase) は scenario-state ConfigMap に記録し、step 間で比較する。
set -eu
: "${REPO_ROOT:?REPO_ROOT is required}"
ns=hermes-langfuse-fixture
suite="${REPO_ROOT}/tests/chainsaw/hermes-agent"

state() { kubectl get configmap scenario-state -n "$ns" -o jsonpath="{.data.$1}"; }
pod_name() { state pod; }
identity() { kubectl get pod -n "$ns" "$1" -o jsonpath='{.metadata.uid}:{.status.containerStatuses[0].restartCount}:{.status.phase}'; }
same_pod_as_baseline() { test "$(identity "$(pod_name)")" = "$(state identity)" || { echo "FAIL: $1"; exit 1; }; }
sink_query() { kubectl exec -i -n "$ns" deployment/sink -- python - "$@" < "$suite/sink-query.py"; }
round_count() { kubectl logs -n "$ns" "$1" | grep -c '^round ' || true; }
auth_header() {
  local pk sk
  pk="$(kubectl get secret langfuse-ingest-key -n "$ns" -o jsonpath='{.data.LANGFUSE_PUBLIC_KEY}' | base64 -d)"
  sk="$(kubectl get secret langfuse-ingest-key -n "$ns" -o jsonpath='{.data.LANGFUSE_SECRET_KEY}' | base64 -d)"
  printf 'Basic %s' "$(printf '%s:%s' "$pk" "$sk" | base64 | tr -d '\n')"
}

deploy_fixture() {
  manifest="$(mktemp)"
  trap 'rm -f "$manifest"' EXIT
  "$suite/render-fixture.sh" > "$manifest"
  kubectl apply --server-side -f "$manifest"
}

keyless_rounds_do_not_send() {
  local pod deadline before
  pod="$(kubectl get pod -n "$ns" -l app=driver -o jsonpath='{.items[0].metadata.name}')"
  deadline=$(( $(date +%s) + 60 ))
  until [ "$(round_count "$pod")" -ge 3 ]; do
    [ "$(date +%s)" -lt "$deadline" ] || { echo 'FAIL: driver が round を出さない'; exit 1; }
    sleep 1
  done
  sink_query no-requests || { echo 'FAIL: 鍵なしで送信した'; exit 1; }
  before="$(identity "$pod")"
  case "$before" in *:Running) ;; *) exit 1 ;; esac
  kubectl create configmap scenario-state -n "$ns" --from-literal=pod="$pod" --from-literal=identity="$before"
}

create_ingest_key() {
  local pk sk
  pk="pk-lf-chainsaw$(openssl rand -hex 16)"
  sk="sk-lf-chainsaw$(openssl rand -hex 16)"
  kubectl create secret generic langfuse-ingest-key -n "$ns" --from-literal=LANGFUSE_PUBLIC_KEY="$pk" --from-literal=LANGFUSE_SECRET_KEY="$sk"
}

late_key_starts_sending() {
  local auth deadline
  auth="$(auth_header)"
  deadline=$(( $(date +%s) + 180 ))
  until sink_query authenticated-marker "$auth"; do
    [ "$(date +%s)" -lt "$deadline" ] || { echo 'FAIL: 後発の鍵で送信が始まらない'; exit 1; }
    sleep 2
  done
  same_pod_as_baseline '鍵の反映で Pod が再起動した'
}

secrets_withheld() {
  local pk sk
  pk="$(kubectl get secret langfuse-ingest-key -n "$ns" -o jsonpath='{.data.LANGFUSE_PUBLIC_KEY}' | base64 -d)"
  sk="$(kubectl get secret langfuse-ingest-key -n "$ns" -o jsonpath='{.data.LANGFUSE_SECRET_KEY}' | base64 -d)"
  sink_query secrets-withheld "$pk" "$sk" || { echo 'FAIL: 機密値が sink に届いた'; exit 1; }
}

harmless_bodies_delivered() {
  local deadline
  deadline=$(( $(date +%s) + 90 ))
  until sink_query positive-bodies; do
    [ "$(date +%s)" -lt "$deadline" ] || { echo 'FAIL: 無害な tool 入出力・応答・次ターン・委譲本文が届かない(全保留の退行)'; exit 1; }
    sleep 2
  done
  sink_query body-policy || { echo 'FAIL: body_policy が期待どおりでない'; exit 1; }
  sink_query child-trace || { echo 'FAIL: 委譲先セッションの trace が届かない'; exit 1; }
  sink_query delegated-goal || { echo 'FAIL: 委譲の本文が届かない'; exit 1; }
}

producer_continues_without_sink() {
  local pod rounds deadline
  pod="$(pod_name)"
  rounds="$(round_count "$pod")"
  deadline=$(( $(date +%s) + 30 ))
  until [ "$(round_count "$pod")" -ge $((rounds + 3)) ]; do
    [ "$(date +%s)" -lt "$deadline" ] || { echo 'FAIL: sink 停止で producer が止まった'; exit 1; }
    sleep 1
  done
  same_pod_as_baseline 'sink 停止で producer が再起動した'
}

"${1:?section is required}"
