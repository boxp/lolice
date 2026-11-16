#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
: "${GRAFANA_URL:?GRAFANA_URL is required}" "${GRAFANA_SERVICE_ACCOUNT_TOKEN:?GRAFANA_SERVICE_ACCOUNT_TOKEN is required}"
dashboard=argoproj/prometheus-operator/dashboards/ai-ops-ticket-usage.json
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
jq -c '.panels[] | select(any(.targets[]?; has("rawSql"))) | {title,sql:.targets[0].rawSql}' "$dashboard" | while IFS= read -r panel; do
  title=$(jq -r .title <<<"$panel")
  sql=$(jq -r '.sql | gsub("\\$__timeFilter\\(start_time\\)"; "start_time >= now() - INTERVAL 7 DAY") | gsub("\\$__interval_s"; "3600") | gsub(" AND (model|route|provider) IN \\(\\$\\{(model|route|provider):singlequote\\}\\)"; "") | gsub(" AND if\\(ticket[^$]*\\$\\{ticket:singlequote\\}\\)"; "")' <<<"$panel")
  jq -n --arg sql "$sql" '{queries:[{refId:"A",datasource:{uid:"langfuse-clickhouse",type:"grafana-clickhouse-datasource"},rawSql:$sql,format:1,queryType:"sql",editorType:"sql"}],from:"now-7d",to:"now"}' > "$tmp/request"
  curl -fsS -H "Authorization: Bearer $GRAFANA_SERVICE_ACCOUNT_TOKEN" -H 'Content-Type: application/json' --data-binary @"$tmp/request" "$GRAFANA_URL/api/ds/query" > "$tmp/response"
  if ! jq -e '(.results.A != null) and ((.results.A.error // .message // "") == "") and (.results.A.status // 200) < 400' "$tmp/response" >/dev/null; then jq -r '.results.A.error // .message // "query error"' "$tmp/response" >&2; exit 1; fi
  rows=$(jq '[.results.A.frames[]?.data.values[0] | length] | add // 0' "$tmp/response")
  printf '%s: %s 行\n' "$title" "$rows"
  if [[ "$title" == '選択期間の照合（推定）' ]]; then
    jq -e '[.results.A.frames[] | .schema.fields as $f | .data.values as $v | range(0; $f|length) | select($f[.].name | contains("差分")) | $v[.][] | select(. != null and (if . < 0 then -. else . end) > 0.00000001)] | length == 0' "$tmp/response" >/dev/null
    echo '照合差分: 0（浮動小数点誤差 1e-8 以下）'
  fi
done
