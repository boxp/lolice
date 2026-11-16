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
bb -e '(require (quote [clojure.edn :as e]) (quote [cheshire.core :as j]) (quote [clojure.string :as s])) (let [c (e/read-string (slurp "scripts/ai-ops-dashboard/quota-coefficients.edn")) d (j/parse-string (slurp "argoproj/prometheus-operator/dashboards/ai-ops-ticket-usage.json") true) sql (s/join " " (keep :rawSql (mapcat :targets (:panels d)))) text (s/join " " (keep (comp :content :options) (:panels d)))] (doseq [n (mapcat val (mapcat (comp seq :coef) (:windows c)))] (assert (s/includes? sql (str (bigdec n))))) (doseq [w (:windows c)] (assert (s/includes? text (:label w)))) (doseq [k [:estimated-at :source]] (assert (s/includes? text (k c)))) (assert (s/includes? text "2026-11-09 16:00")) (assert (s/includes? text "2026-11-16 16:00")))'
jq -e '[.. | objects | .expr? // empty | select(test("(rate|increase)\\([^)]*ai_subscription_window_used_percent"))] | length == 0' "$dashboard" >/dev/null
jq -e '[.panels[].gridPos as $g | range($g.x; $g.x+$g.w) as $x | range($g.y; $g.y+$g.h) as $y | "\($x),\($y)"] | length == (unique | length)' "$dashboard" >/dev/null
jq -e '[.panels[] | select(any(.targets[]?; (.rawSql // "") | contains("$__interval_s"))) | .interval == "$bin"] | length > 0 and all' "$dashboard" >/dev/null
jq -e '[.panels[] | select(.type == "timeseries" and .datasource.type != "prometheus") | .fieldConfig.defaults.custom.stacking.mode == "none"] | length > 0 and all' "$dashboard" >/dev/null
# 下段（アカウント全体の実測）は上の選択に連動しない
jq -e '(.panels | map(select(.type == "row" and (.title | startswith("アカウント全体の実測")))) | .[0].gridPos.y) as $y | [.panels[] | select(.gridPos.y > $y and .type != "text") | .targets[] | (.rawSql // .expr) | test("\\$\\{(ticket|model|route|provider)")] | length > 0 and (any | not)' "$dashboard" >/dev/null
# 見出し・列名・凡例に略語や英語の内部名を出さない
jq -e '[(.panels[].title), (.templating.list[].label), (.. | objects | .rawSql? // empty | [scan("AS `([^`]*)`")[0]] | .[])] | map(select(test("\\b(bin|pt|used%|ticket_label|runs|models|routes|first_JST|last_JST)\\b|_sum\\b"; "i"))) | length == 0' "$dashboard" >/dev/null
if command -v kustomize >/dev/null; then kustomize build argoproj/prometheus-operator >/dev/null; else echo 'kustomize がないため build をスキップ'; fi
# organize は単一フレームにしか効かないため、複数クエリのパネルは先に結合する（題名の対応だけに使う T は結合しない）
jq -e '[.panels[] | select((.targets | length) > 1 and all(.targets[]; .refId != "T")) | .transformations[0].id == "joinByField"] | length > 0 and all' "$dashboard" >/dev/null
jq -e '.panels[] | select(.id == 10) | .transformations[0].options.byField == "w" and (.targets[1].rawSql | contains("AS w,"))' "$dashboard" >/dev/null
# 結合後に ClickHouse 側の列へ付く refId 接尾辞（" B"）を元の名前へ戻す
jq -e '.panels[] | select(.id == 10) | .transformations[1].options.renameByName | has("推定の合計（%） B") and has("推定幅（%） B")' "$dashboard" >/dev/null
# 推移と使用率で同じ週枠は同じ色
jq -e '[.panels[] | select(.id == 6 or .id == 9) | [.fieldConfig.overrides[] | {n: .matcher.options, c: .properties[0].value.fixedColor}]] | .[0] == .[1] and (.[0] | length) >= 3' "$dashboard" >/dev/null
# 重なった棒で週枠が隠れないよう、推定の推移は線で描く。比較表は全列が見えるよう全幅にする
jq -e '.panels[] | select(.id == 6) | .fieldConfig.defaults.custom.drawStyle == "line"' "$dashboard" >/dev/null
jq -e '.panels[] | select(.id == 10) | .gridPos.w == 24' "$dashboard" >/dev/null
# 記録の欠け表の長い見出しが折り返しで切れないよう幅を確保する
jq -e '.panels[] | select(.id == 7) | [.fieldConfig.overrides[] | select(.matcher.options == "推定に入らないトークン（百万）") | .properties[0].value] == [250]' "$dashboard" >/dev/null
# 票の表示は「票番号 題名」。題名は Prometheus（認証済み Grafana 内）から引き、repo には置かない
jq -e '.templating.list[] | select(.name == "ticket") | .datasource.type == "prometheus" and (.query.query | contains("ai_ops_ticket_title_info")) and .allValue == "'"'"'__all__'"'"'" and (.regex | contains("?<text>") and contains("?<value>")) and .allowCustomValue == true' "$dashboard" >/dev/null
# 「すべて」は選択肢の列挙ではなく絞り込みなし（題名の系列に無い票の利用も落とさない）
jq -e '[.. | objects | .rawSql? // empty | select(contains("${ticket:singlequote}")) | contains("'"'"'__all__'"'"' IN (${ticket:singlequote}) OR")] | length > 0 and all' "$dashboard" >/dev/null
# 票の列を持つ表は題名を値の対応で付け、値（票番号）とリンクは変えない
jq -e '[.panels[] | select(any(.fieldConfig.overrides[]?; .matcher.options == "票")) | (.transformations[0].id == "configFromData" and .transformations[0].options.applyTo.options == "票" and any(.targets[]; .refId == "T" and (.expr | contains("ai_ops_ticket_title_info"))))] | length == 2 and all' "$dashboard" >/dev/null
echo 'ticket usage tests passed'
