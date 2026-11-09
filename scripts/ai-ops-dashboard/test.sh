#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
dashboard=argoproj/prometheus-operator/dashboards/ai-ops-quality.json
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
bb scripts/ai-ops-dashboard/generate.bb
cp "$dashboard" "$tmp/first.json"
cp docs/project_docs/BOXP-472/panel-map.md "$tmp/map.md"
bb scripts/ai-ops-dashboard/generate.bb
cmp "$tmp/first.json" "$dashboard"
cmp "$tmp/map.md" docs/project_docs/BOXP-472/panel-map.md
git show HEAD:"$dashboard" > "$tmp/original.json"
jq -S '[.. | objects | select(has("id") and (.id | type == "number") and .id < 1000 and .type != "row") | del(.gridPos)] | sort_by(.id)' "$tmp/original.json" > "$tmp/original-panels"
jq -S '[.. | objects | select(has("id") and (.id | type == "number") and .id < 1000 and .type != "row") | del(.gridPos)] | sort_by(.id)' "$dashboard" > "$tmp/current-panels"
cmp "$tmp/original-panels" "$tmp/current-panels"
jq -e '[.. | objects | select(has("id") and (.id | type == "number")) | .id] | length == (unique | length)' "$dashboard" > /dev/null
jq -e '.timezone == "Asia/Tokyo"' "$dashboard" > /dev/null
jq -e '[.. | objects | select(.datasource.type? == "grafana-clickhouse-datasource" and has("rawSql")) | .rawSql | (contains("ai_ops_usage_v1") and contains("$__timeFilter") and (test("\\b(input|output|metadata)\\b") | not))] | all' "$dashboard" > /dev/null
jq -e '[.. | objects | .expr? // empty | select(test("(rate|increase)\\([^)]*ai_subscription_window_used_percent"))] | length == 0' "$dashboard" > /dev/null
jq -r '[.. | objects | select(has("id") and (.id | type == "number") and .id < 1000 and .type != "row") | .id] | unique[]' "$tmp/original.json" > "$tmp/ids"
while read -r id; do rg -q "^\\| $id \\|" docs/project_docs/BOXP-472/panel-map.md; done < "$tmp/ids"
# 表示される top-level panel が重ならない (折りたたみ row の中身は Grafana が展開時に配置する)
jq -e '[.panels[] | select(.gridPos != null) | .gridPos as $g | range($g.x; $g.x + $g.w) as $x | range($g.y; $g.y + $g.h) as $y | "\($x),\($y)"] | length == (unique | length)' "$dashboard" > /dev/null
jq -e '[.. | objects | select(.type? == "state-timeline") | .fieldConfig.defaults.mappings | length > 0] | all' "$dashboard" > /dev/null
jq -e '[.. | objects | .rawSql? // empty | select(test("var-ticket"))] | length == 0' "$dashboard" > /dev/null
# bin を使う ClickHouse panel は時系列も表も同じ $bin 幅で集計する
jq -e '[.. | objects | select(.targets? // [] | any((.rawSql? // "") | test("__interval_s"))) | .interval == "$bin"] | (length > 0 and all)' "$dashboard" > /dev/null
# $__timeFilter は両端を含むので、bin の絞り込みリンクの終端は次の bin の開始と重ねない
jq -e '[.. | objects | .rawSql? // empty | select(test("bin_end_ms"))] | (length > 0 and all(test("\\*1000-1 AS bin_end_ms")))' "$dashboard" > /dev/null
# 判断可能性 (233) は export が 1=正常、measuring が 1=充足、insufficient/no_observations が 1=該当で、鮮度は別の時系列にある
jq -e '
  [.. | objects | select(.type? == "state-timeline" and .title? == "判断可能性（計測の鮮度・export・観測数）")] as $s |
  ($s | length == 1) and
  ($s[0].targets | map(.refId) == ["A", "C", "D", "E"]) and
  ($s[0].fieldConfig.defaults.mappings[0].options."1".text == "正常") and
  ($s[0].fieldConfig.defaults.thresholds.steps | map(.color) == ["red", "green"]) and
  ($s[0].fieldConfig.overrides | map(.matcher.options) == [".* measuring", ".* (insufficient|no_observations)"]) and
  ($s[0].fieldConfig.overrides[0].properties[1].value.steps | map(.color) == ["orange", "green"]) and
  ($s[0].fieldConfig.overrides[1].properties[1].value.steps | map(.color) == ["green", "orange"])
' "$dashboard" > /dev/null
jq -e '
  [.. | objects | select(.type? == "timeseries" and .title? == "計測の鮮度（outcome export からの経過）")] as $f |
  ($f | length == 1) and
  ($f[0].targets[0].refId == "B") and
  ($f[0].targets[0].expr | contains("ai_ops_outcome_generated_timestamp_seconds")) and
  ($f[0].fieldConfig.defaults.thresholds.steps[1].value == 7200.000001) and
  ($f[0].fieldConfig.defaults.custom.thresholdsStyle.mode == "line")
' "$dashboard" > /dev/null
# users.xml の GRANT に grantee を書くと ClickHouse が access control の読み込みに失敗する
if grep -Eq '<query>GRANT .* TO ' argoproj/langfuse/files/clickhouse-users.xml; then
  echo 'clickhouse-users.xml の GRANT に TO を書かないでください' >&2
  exit 1
fi
printf '%s\n' 'generator tests passed'
