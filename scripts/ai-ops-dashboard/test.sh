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
printf '%s\n' 'generator tests passed'
