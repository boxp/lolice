#!/usr/bin/env bash
# 配備済み Grafana 経由で dashboard の全 query を直近 7 日（集計間隔 1 時間、選択はすべて）で実行する。
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
: "${GRAFANA_URL:?GRAFANA_URL is required}" "${GRAFANA_SERVICE_ACCOUNT_TOKEN:?GRAFANA_SERVICE_ACCOUNT_TOKEN is required}"
dashboard=argoproj/prometheus-operator/dashboards/ai-ops-ticket-usage.json
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
prom_uid=$(curl -fsS -H "Authorization: Bearer $GRAFANA_SERVICE_ACCOUNT_TOKEN" "$GRAFANA_URL/api/datasources" | jq -r '[.[] | select(.type == "prometheus" and .isDefault)][0].uid')
query() { curl -fsS -H "Authorization: Bearer $GRAFANA_SERVICE_ACCOUNT_TOKEN" -H 'Content-Type: application/json' --data-binary @"$1" "$GRAFANA_URL/api/ds/query"; }
ok() { jq -e '(.results | to_entries | length > 0) and all(.results[]; (.error // "") == "" and (.status // 200) < 400)' "$1" >/dev/null || { jq -r '[.results[]?.error // empty] + [.message // empty] | join("; ")' "$1" >&2; exit 1; }; }
# 「すべて」選択時の Grafana と同じく、allValue を書式なしでそのまま埋め込む
ticket_all=$(jq -r '.templating.list[] | select(.name == "ticket") | .allValue' "$dashboard")
jq -c '.panels[] | . as $p | .targets[]? | select(has("rawSql")) | {title: $p.title, sql: .rawSql}' "$dashboard" | while IFS= read -r panel; do
  title=$(jq -r .title <<<"$panel")
  sql=$(jq -r '.sql | gsub("\\$__timeFilter\\(start_time\\)"; "start_time >= now() - INTERVAL 7 DAY") | gsub("\\$__interval_s"; "3600") | gsub(" AND (model|route|provider) IN \\(\\$\\{(model|route|provider):singlequote\\}\\)"; "") | gsub("\\$\\{ticket:singlequote\\}"; $t)' --arg t "$ticket_all" <<<"$panel")
  jq -n --arg sql "$sql" '{queries:[{refId:"A",datasource:{uid:"langfuse-clickhouse",type:"grafana-clickhouse-datasource"},rawSql:$sql,format:1,queryType:"sql",editorType:"sql"}],from:"now-7d",to:"now"}' > "$tmp/request"
  query "$tmp/request" > "$tmp/response"; ok "$tmp/response"
  printf 'ClickHouse %s: %s 行\n' "$title" "$(jq '[.results.A.frames[]?.data.values[0] | length] | add // 0' "$tmp/response")"
  if [[ "$title" == 推定の合計と実測の比較* ]]; then
    jq -r '.results.A.frames[0] | .schema.fields as $f | .data.values as $v | range(0; $v[0]|length) as $i | [$f, $v] | "\(.[1][0][$i])\t推定の合計=\(.[1][1][$i])\t票あり=\(.[1][3][$i])\t票なし=\(.[1][4][$i])"' "$tmp/response"
    jq -e '.results.A.frames[0].data.values as $v | [range(0; $v[0]|length) as $i | ($v[1][$i] - $v[3][$i] - $v[4][$i]) | fabs < 1e-8] | all' "$tmp/response" >/dev/null
    echo '推定の合計 = 票あり + 票なし（差 1e-8 以下）'
  fi
done
jq -c '.panels[] | . as $p | .targets[]? | select(has("expr")) | {title: $p.title, expr: .expr}' "$dashboard" | while IFS= read -r panel; do
  title=$(jq -r .title <<<"$panel")
  jq -n --arg expr "$(jq -r '.expr | gsub("\\$__range"; "7d")' <<<"$panel")" --arg uid "$prom_uid" '{queries:[{refId:"A",datasource:{uid:$uid,type:"prometheus"},expr:$expr,instant:true,range:false}],from:"now-7d",to:"now"}' > "$tmp/request"
  query "$tmp/request" > "$tmp/response"; ok "$tmp/response"
  printf 'Prometheus %s: ' "$title"; jq -r '[.results.A.frames[] | "\(.schema.fields[1].labels.w // "?")=\(.data.values[1][0])"] | join(", ")' "$tmp/response"
done
