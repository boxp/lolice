#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
dashboard=argoproj/prometheus-operator/dashboards/ai-ops-ticket-usage.json
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
bb scripts/ai-ops-dashboard/ticket-usage.bb
cp "$dashboard" "$tmp/first"
bb scripts/ai-ops-dashboard/ticket-usage.bb
cmp "$tmp/first" "$dashboard"
jq -e '.uid == "ai-ops-ticket-usage" and .timezone == "Asia/Tokyo"' "$dashboard" >/dev/null
jq -e '[.panels[].id] | length == (unique | length)' "$dashboard" >/dev/null
jq -e '[.. | objects | .rawSql? // empty | contains("ai_ops_usage_v1") and contains("$__timeFilter") and (test("\\b(input|output|metadata)\\b") | not)] | length > 0 and all' "$dashboard" >/dev/null
jq -e '[.. | objects | .rawSql? // empty | gsub("'"'"'[^'"'"']*'"'"'|`[^`]*`"; "") | test("[^\\x00-\\x7F]") | not] | length > 0 and all' "$dashboard" >/dev/null
jq -e '[.. | objects | .rawSql? // empty | test("\\bAS (start_time|trace_id|observation_id|trace_name|model|provider|route|ticket|input_tokens|cache_read_tokens|cache_write_tokens|output_tokens|reasoning_tokens|total_tokens|other_tokens|has_usage)\\b"; "i") | not] | length > 0 and all' "$dashboard" >/dev/null
bb -e '(require (quote [clojure.edn :as e]) (quote [cheshire.core :as j]) (quote [clojure.string :as s])) (let [c (e/read-string (slurp "scripts/ai-ops-dashboard/quota-coefficients.edn")) d (j/parse-string (slurp "argoproj/prometheus-operator/dashboards/ai-ops-ticket-usage.json") true) sql (s/join " " (keep :rawSql (mapcat :targets (:panels d)))) text (get-in d [:panels 0 :options :content])] (doseq [n (mapcat val (mapcat (comp seq :coef) (:windows c)))] (assert (s/includes? sql (str (bigdec n))))) (doseq [k [:valid-from :valid-to :estimated-at]] (assert (s/includes? text (k c)))))'
jq -e '[.. | objects | .expr? // empty | select(test("(rate|increase)\\([^)]*ai_subscription_window_used_percent"))] | length == 0' "$dashboard" >/dev/null
jq -e '[.panels[].gridPos as $g | range($g.x; $g.x+$g.w) as $x | range($g.y; $g.y+$g.h) as $y | "\($x),\($y)"] | length == (unique | length)' "$dashboard" >/dev/null
jq -e '[.panels[] | select(any(.targets[]?; (.rawSql // "") | contains("$__interval_s"))) | .interval == "$bin"] | length > 0 and all' "$dashboard" >/dev/null
if command -v kustomize >/dev/null; then kustomize build argoproj/prometheus-operator >/dev/null; else echo 'kustomize がないため build をスキップ'; fi
echo 'ticket usage tests passed'
