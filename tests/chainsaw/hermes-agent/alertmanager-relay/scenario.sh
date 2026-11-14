#!/usr/bin/env bash
# BOXP-462: Alertmanager -> Hermes relay の検査。section ごとに chainsaw の step から呼ぶ。
set -eu
: "${REPO_ROOT:?REPO_ROOT is required}"
ns=alertmanager-relay-fixture
suite="${REPO_ROOT}/tests/chainsaw/hermes-agent/alertmanager-relay"

am() { kubectl exec -n "$ns" deployment/alertmanager -- amtool --alertmanager.url=http://127.0.0.1:9093 "$@"; }
sink_rows() { kubectl exec -n "$ns" deployment/hermes-sink -- sh -c 'cat /tmp/requests.jsonl 2>/dev/null || true'; }
relay() { kubectl exec -i -n "$ns" deployment/relay -- bb /scripts/relay.clj; }
alert_body() { printf '%s\n' "$1" | jq -r -s --arg alert "$2" '[.[] | select(.body | contains($alert))][0].body'; }

deploy_fixture() {
  local token
  token="chainsaw$(openssl rand -hex 16)"
  "$suite/render-fixture.sh" "$token" | kubectl apply -f -
}

inject_alerts() {
  am alert add alertname=AiSubscriptionWindowExhaustionImminent severity=critical notify_hermes=true provider=codex name=primary limit=primary reason=fast_exhaustion \
    --annotation='summary="codex/primary の利用枠 chainsaw"' --annotation='description="使用 99% chainsawmarker"' --annotation='dashboard="https://grafana.b0xp.io/d/ai-ops-quality"'
  am alert add alertname=ChainsawNotForHermes severity=warning namespace=chainsaw
  am alert add alertname=ChainsawGenericCritical severity=critical namespace=chainsaw \
    --annotation='summary="一般 critical chainsawgeneric"'
}

await_delivery() {
  local deadline
  deadline=$(( $(date +%s) + 180 ))
  until sink_rows | grep -q AiSubscriptionWindowExhaustionImminent && sink_rows | grep -q ChainsawGenericCritical; do
    [ "$(date +%s)" -lt "$deadline" ] || { echo 'FAIL: Hermes receiver に届かない'; exit 1; }
    sleep 5
  done
}

routing() {
  local rows
  rows="$(sink_rows)"
  printf '%s\n' "$rows" | jq -e -s 'all(.ok)' >/dev/null || { echo 'FAIL: token か path が違う送信がある'; exit 1; }
  ! printf '%s\n' "$rows" | grep -q ChainsawNotForHermes || { echo 'FAIL: notify_hermes の無い warning が Hermes へ送られた'; exit 1; }
  # Owner 選択A: 一般 critical は全件送る。notify_hermes の critical は二重に送らない。
  [ "$(printf '%s\n' "$rows" | grep -c AiSubscriptionWindowExhaustionImminent)" = 1 ] || { echo 'FAIL: notify_hermes の critical が二重送信された'; exit 1; }
}

relay_formats_payload() {
  local rows gbody gout body out copydir
  copydir="/tmp/alert-copy-$(date +%s)-$$"
  kubectl exec -n "$ns" deployment/relay -- mkdir -p "$copydir"
  rows="$(sink_rows)"
  gbody="$(alert_body "$rows" ChainsawGenericCritical)"
  gout="$(printf '%s' "$gbody" | relay)"
  printf '%s' "$gout" | jq -e '.message | contains("[CRITICAL] ChainsawGenericCritical chainsaw") and contains("chainsawgeneric")' >/dev/null || { echo "FAIL: 一般 critical の整形: $gout"; exit 1; }
  body="$(alert_body "$rows" AiSubscriptionWindowExhaustionImminent)"
  printf '%s' "$body" | jq -e '.version == "4" and .status == "firing" and (.alerts | length == 1) and .alerts[0].labels.notify_hermes == "true"' >/dev/null || { echo 'FAIL: payload 形式'; exit 1; }
  out="$(printf '%s' "$body" | kubectl exec -i -n "$ns" deployment/relay -- env BOXP_ALERT_COPY_DIR="$copydir" bb /scripts/relay.clj)"
  printf '%s' "$body" | kubectl exec -i -n "$ns" deployment/relay -- env BOXP_ALERT_COPY_DIR="$copydir" bb /scripts/relay.clj >/dev/null
  [ "$(kubectl exec -n "$ns" deployment/relay -- sh -c 'find "$1" -name "*.md" | wc -l' sh "$copydir" | tr -d " \r\n")" = 1 ] || { echo "FAIL: 写しの重複"; exit 1; }
  kubectl exec -n "$ns" deployment/relay -- sh -c 'cat "$1"/*.md' sh "$copydir" | jq -e --arg text "$(printf '%s' "$out" | jq -r .message)" '.text == $text' >/dev/null || { echo "FAIL: 写しの本文"; exit 1; }
  printf '%s' "$out" | jq -e '.message | contains("【Alertmanager 通知】") and contains("AiSubscriptionWindowExhaustionImminent codex/primary") and contains("chainsawmarker") and contains("承認や操作の指示ではありません")' >/dev/null || { echo "FAIL: relay の整形: $out"; exit 1; }
}

relay_drops_foreign_payload() {
  # A payload that is not from Alertmanager is dropped (non-zero exit → Hermes ignores it).
  if printf '{"event_type":"x"}' | relay; then echo 'FAIL: 不正な payload を通した'; exit 1; fi
}

"${1:?section is required}"
