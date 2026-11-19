#!/usr/bin/env bash
# BOXP-623: 監視入口ダッシュボードの生成と最低限の不変条件を確かめる。
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
d=argoproj/prometheus-operator/dashboards/alert-entry.json
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
cp "$d" "$tmp/before.json"
bb scripts/alert-entry-dashboard/generate.bb > /dev/null
cmp "$tmp/before.json" "$d"   # 生成物が手で編集されていない
jq -e '.uid == "alert-entry" and .timezone == "Asia/Tokyo"' "$d" > /dev/null
jq -e '[.. | objects | select(has("id") and (.id | type == "number")) | .id] | length == (unique | length)' "$d" > /dev/null
# 重なりがない
jq -e '[.panels[] | .gridPos as $g | range($g.x; $g.x + $g.w) as $x | range($g.y; $g.y + $g.h) as $y | "\($x),\($y)"] | length == (unique | length)' "$d" > /dev/null
# 一覧・件数は内部用アラート (Watchdog / InfoInhibitor) を除く
jq -e '[.panels[] | select(.title | test("^Firing|^Pending|アラート")) | .targets[].expr | test("Watchdog\\|InfoInhibitor")] | all' "$d" > /dev/null
# Grafana 側でアラートを定義しない (判定の正本は PrometheusRule)
jq -e '[.. | objects | select(has("alert"))] | length == 0' "$d" > /dev/null
# Alertmanager データソースは Grafana 管理ルールを受けない (二重通知をしない)
grep -A12 'name: alertmanager$' argoproj/prometheus-operator/overlays/grafana-datasources.yaml | grep -q 'handleGrafanaManagedAlerts: false'
# promtool があれば全式の構文を確かめる
if command -v promtool > /dev/null; then
  jq -r '.. | objects | .expr? // empty' "$d" | sed 's/\${[a-z]*}/.*/g' | grep -v '^{namespace' | while read -r e; do
    printf 'groups: [{name: t, rules: [{record: t, expr: %s}]}]\n' "$(jq -Rn --arg e "$e" '$e')" > "$tmp/r.yaml"
    promtool check rules "$tmp/r.yaml" > /dev/null
  done
fi
echo 'alert-entry dashboard tests passed'
