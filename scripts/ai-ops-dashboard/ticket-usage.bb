#!/usr/bin/env bb
;; 票ごとの枠負担 dashboard (uid ai-ops-ticket-usage) を生成する。
;; 目的: 票ごとに「どのモデルが・どの経路で・どの週枠をどれだけ減らしたか（推定）」を読み、担当モデルを決める。
;; 上段は画面上部の選択（票・モデル・経路・提供元）に連動する推定、下段は選択に連動しないアカウント全体の実測。
(require '[cheshire.core :as json] '[clojure.string :as str] '[clojure.edn :as edn] '[babashka.process :as proc])
(import '[java.time Instant ZoneId] '[java.time.format DateTimeFormatter])

(def coeff (edn/read-string (slurp "scripts/ai-ops-dashboard/quota-coefficients.edn")))
(def ws (:windows coeff))
(def path "argoproj/prometheus-operator/dashboards/ai-ops-ticket-usage.json")
(def ch {:type "grafana-clickhouse-datasource" :uid "langfuse-clickhouse"})
(def prom {:type "prometheus" :uid "${datasource}"})
(def mixed {:type "datasource" :uid "-- Mixed --"})

;; ---- SQL の部品 ----
(def ticket-expr "if(ticket = '', '(票なし)', ticket)")
;; 票の選択肢は題名つきの Prometheus 系列から作るため、「すべて」は選択肢の列挙ではなく絞り込みなしにする
;; （題名の系列に無い票の利用を落とさない）。
;; Grafana は独自の allValue を書式（:singlequote）を通さずそのまま埋め込むため、SQL 文字列として引用符つきで持つ。
(def all-ticket "__all__")
(def all-ticket-value (str "'" all-ticket "'"))
(def where-selected (str "WHERE $__timeFilter(start_time) AND model IN (${model:singlequote}) AND route IN (${route:singlequote}) AND provider IN (${provider:singlequote}) AND ('" all-ticket "' IN (${ticket:singlequote}) OR " ticket-expr " IN (${ticket:singlequote}))"))
(def where-all "WHERE $__timeFilter(start_time)")
(def models (sort (distinct (mapcat (comp keys :coef) ws))))
(def no-coef (str "model NOT IN (" (str/join ", " (map #(str "'" % "'") models)) ")"))
(defn num [n] (str (bigdec n)))
(defn coef-of [mode [l h]] (case mode :low l :high h :mid (/ (+ (bigdec l) (bigdec h)) 2M)))
(defn per-row [w mode] (str "multiIf(" (str/join ", " (mapcat (fn [[m c]] [(str "model = '" m "'") (num (coef-of mode c))]) (sort-by key (:coef w)))) ", 0) * total_tokens / 1e6"))
(defn est [w mode] (str "sum(" (per-row w mode) ")"))
(defn range-text [w] (str "concat(toString(toDecimal64(round(" (est w :low) ", 2), 2)), '〜', toString(toDecimal64(round(" (est w :high) ", 2), 2)))"))
(defn q [s] (str "`" s "`"))
(def col-mtok (q "トークン（百万）"))
(def col-nocoef (q "推定に入らないトークン（百万）"))
(defn col-est [w] (q (str (:label w) " 推定（%）")))
(defn col-range [w] (q (str (:label w) " 推定幅（%）")))
(def burden-cols
  (str "uniqExact(trace_id) AS " (q "実行回数") ", "
       "sum(total_tokens) / 1e6 AS " col-mtok ", "
       (str/join ", " (for [w ws] (str (est w :mid) " AS " (col-est w) ", " (range-text w) " AS " (col-range w)))) ", "
       "sumIf(total_tokens, " no-coef ") / 1e6 AS " col-nocoef))
(def first-window (first ws))
(def bin "toStartOfInterval(start_time, toIntervalSecond($__interval_s))")

;; ---- panel の部品 ----
(defn ch-target [type sql] {:refId "A" :datasource ch :editorType "sql" :queryType (if (= type "timeseries") "timeseries" "table") :format (if (= type "timeseries") 0 1) :rawSql sql :pluginVersion "4.23.4"})
(def plain-color {:color {:mode "fixed" :fixedColor "text"} :thresholds {:mode "absolute" :steps [{:color "text" :value nil}]}})
(defn by-regex [re props] {:matcher {:id "byRegexp" :options re} :properties (vec (for [[k v] props] {:id k :value v}))})
(defn by-name [n props] {:matcher {:id "byName" :options n} :properties (vec (for [[k v] props] {:id k :value v}))})
(def table-overrides
  [(by-regex ".*（%）$" {"decimals" 2 "unit" "none"})
   (by-regex ".*（百万）$" {"decimals" 1 "unit" "none"})
   (by-regex ".*（万）$" {"decimals" 1 "unit" "none"})
   (by-regex ".*推定幅（%）$" {"custom.align" "center"})])
(def ticket-links
  (by-name "票" {"links" [{:title "この票だけ表示" :url "/d/ai-ops-ticket-usage/?${__url_time_range}&${datasource:queryparam}&var-ticket=${__value.raw}"}
                          {:title "Langfuse で記録を開く" :url "https://langfuse.b0xp.io/project/ai-ops/sessions/${__value.raw}"}]
                 "custom.width" 130}))
;; 題名は票番号とは別の列にする。値の対応で票の列に題名を付けると、行の高さとセルの中身（inspect）が元の票番号で決まり、
;; 長い題名が切れたり全文を確かめられなかったりするため。題名の列は本当の文字列なので、折り返しで全文が出る。
;; 題名の系列に無い票（(票なし) など）は、利用の「記録なし」と取り違えないよう「（題名なし）」と出す。
(def title-column (by-name "題名" {"custom.width" 420 "custom.wrapText" true "custom.inspect" true "noValue" "（題名なし）"}))
;; ---- 票の題名（認証済みの Grafana の中だけで、Prometheus の ai_ops_ticket_title_info から引く） ----
;; 題名は repo に置かない。すべての票ファイルと run のある票について、票ファイルの見出しから現在の題名を出す。
;; 題名が取れない票は title="" になり、表示は票番号だけのまま（推測で埋めない）。
(def titles-job "{job=\"codex-workspace-ai-ops-metrics\"}")
(def titled (str "max by (ticket,title) (ai_ops_ticket_title_info" titles-job ")"))
;; 票の選択欄の表示名「BOXP-N 題名」。題名が空なら票番号だけ。
(def ticket-display
  (str "label_join(" titled ", \"display\", \" \", \"ticket\", \"title\")"))
(def title-target {:refId "T" :datasource prom :expr titled :instant true :range false :format "table" :legendFormat ""})
;; 題名の系列（ticket, title）を票の列で表へ結合する。票ごとに 1 行の題名を、表の各行（同じ票が複数行あってもよい）へ付ける。
;; 結合は外部結合にし、題名の系列だけにある票（期間内に記録の無い票）の行は「実行回数」が空なので取り除く。
;; これで題名の系列に無い票（(票なし) など）の行も落とさず、題名の欄が空のまま票番号だけ残る（推測で埋めない）。
(def ticket-title-join
  [{:id "renameByRegex" :options {:regex "^ticket$" :renamePattern "票"}}
   {:id "renameByRegex" :options {:regex "^title$" :renamePattern "題名"}}
   {:id "joinByField" :options {:byField "票" :mode "outerTabular"}}
   ;; 結合で付く refId の接尾辞（" A" " T"）を外し、列名の書式・並べ替えの設定が効くようにする
   {:id "renameByRegex" :options {:regex "^(.*) [AT]$" :renamePattern "$1"}}
   {:id "filterByValue" :options {:type "exclude" :match "any" :filters [{:fieldName "実行回数" :config {:id "isNull" :options {}}}]}}
   {:id "organize" :options {:excludeByName {"Time" true "Value" true} :indexByName {"票" 0 "題名" 1}}}])
(defn table [id title description sql & {:keys [overrides sort titles]}]
  {:id id :title title :description description :type "table" :datasource (if titles mixed ch)
   :targets (if titles [(ch-target "table" sql) title-target] [(ch-target "table" sql)])
   :transformations (if titles ticket-title-join [])
   :fieldConfig {:defaults (merge plain-color {:noValue "記録なし" :unit "none" :custom {:align "auto" :wrapHeaderText true :cellOptions {:type "auto"}}})
                 :overrides (vec (concat table-overrides overrides))}
   ;; 題名を折り返す表は、日本語 2 行の題名が行に収まるよう最小の行の高さを上げる
   :options {:showHeader true :cellHeight (if titles "md" "sm") :footer {:show false} :sortBy (if sort [{:displayName sort :desc true}] [])}})
(defn row [id title] {:id id :type "row" :title title :collapsed false :panels []})
(defn text [id title content] {:id id :type "text" :title title :options {:mode "markdown" :content content}})

;; ---- Prometheus（アカウント全体の実測） ----
(def job "{job=\"codex-workspace-ai-ops-metrics\"}")
(defn named [expr] (reduce (fn [e w] (str "label_replace(" e ", \"w\", \"" (:label w) "\", \"name\", \"" (get-in w [:prom :name]) "\")")) expr ws))
(defn metric [name offset] (str "max by (provider,name,limit,window) (" name job (when offset " offset 1m") ")"))
(def u (metric "ai_subscription_window_used_percent" false))
(def u1 (metric "ai_subscription_window_used_percent" true))
(def r (metric "ai_subscription_window_resets_at_seconds" false))
(def r1 (metric "ai_subscription_window_resets_at_seconds" true))
;; 1 分ごとの used% 増分。reset をまたぐ 1 分は除外する（BOXP-472 と同じ式）。
(def delta-base (str "((" u " - " u1 ") unless on(provider,name,limit,window) (abs(" r " - " r1 ") > 3600))"))
(def used-now (str "max by (provider,name,limit,window) (ai_subscription_window_used_percent" job " unless on(provider,limit,name,window,pod) (ai_subscription_window_resets_at_seconds" job " <= time()))"))

;; ---- 説明文 ----
(def jst (.withZone (DateTimeFormatter/ofPattern "yyyy-MM-dd HH:mm") (ZoneId/of "Asia/Tokyo")))
(defn jst-text [s] (.format jst (Instant/parse s)))
(defn mid-coef [k model] (coef-of :mid (get-in (first (filter #(= k (:key %)) ws)) [:coef model])))
(def fable-ratio (Math/round (/ (double (mid-coef "shared" "claude-fable-5-2")) (double (mid-coef "shared" "claude-opus-5-5")))))
(def window-lines (str/join "\n" (for [w ws] (str "- **" (:label w) "**: " (:note w)))))
(def guide (str
"票（チケット）ごとに、どのモデルがどれだけ動き、Claude と Codex の週枠をどれだけ減らしたか（推定）を見て、**票の担当モデルを決める**ための画面です。

**読む順番**
1. 「票ごとの枠負担」で、週枠を多く減らした票を探します（列の見出しを押すと並べ替え）。
2. 票の隣の「題名」列で内容を確かめます（題名は Task Board の票ファイルの現在の見出し。取れない票は空欄のまま票番号だけ）。票番号を押して「この票だけ表示」を選ぶと、上段がその票だけになります。「モデル × 経路ごとの枠負担」で、親の作業・委譲した子・レビューのどこが重いかを見ます。
3. 「モデル別の枠負担」で、モデルごとの重さを比べます。同じトークン数なら Fable は Opus の約 " fable-ratio " 倍 Claude 共有週枠を減らし、さらに Fable 専用週枠も減らします。軽いモデルで済む作業が重いモデルに割り当てられていれば、担当変更の候補です。
4. 下段「アカウント全体の実測」で、推定の合計が実際の週枠の減り方とどれだけ合っているかを確かめます。

**値の種類**
- **実測**: 実際に記録された値です。トークン数は Langfuse の記録、週枠の使用率は Claude / Codex が返す値（1% 単位、アカウント全体）です。
- **推定**: トークン数 × モデルごとの係数で計算した値です。Claude / Codex は票ごとの消費を返さないため、票ごとの枠消費はすべて推定です。「推定（%）」は中央値、「推定幅（%）」は係数の下限〜上限で計算した範囲です。
- 単位「%」は**週枠に対する割合**（例: 2.5 は週枠の 2.5% を使った）、「トークン（百万）」は 100 万トークン単位です。

**週枠の種類**（どれも 7 日ごとにリセット）
" window-lines "

**選択の効き方**
- 上の「票」「モデル」「経路」「提供元」は**上段（推定と記録）だけ**に効きます。下段「アカウント全体の実測」は常にアカウント全体の値で、選んだ票の実測ではありません。
- 「集計間隔」は推移グラフの 1 点がまとめる時間の長さです。
- 「(票なし)」は Hermes（Telegram・家の Web・Webhook・定期実行）など票番号のない利用で、どの票にも割り当てません。"))
(def coef-table (str/join "\n" (for [w ws [m [l h]] (sort-by key (:coef w))] (str "| " (:label w) " | " m " | " (num l) "〜" (num h) " | " (:r2 w) " |"))))
(def basis (str
"**係数**（100 万トークンで週枠の何 % が減るか）

| 週枠 | モデル | 100 万トークンあたりの消費（%） | 当てはまりの良さ（R²、1 に近いほど良い） |
|---|---|---|---|
" coef-table "

- 求めた期間: " (jst-text (:valid-from coeff)) " 〜 " (jst-text (:valid-to coeff)) "（日本時間）、" (:estimated-at coeff) " 算出。出典: " (:source coeff) "。
- 数え方: キャッシュ読み込みを含む全トークン数に掛けています。期間外の値はこの係数をそのまま当てはめたもので、モデルの更新や使い方の変化でずれるため毎週見直します。
- 表にないモデル（Haiku・Sonnet など）は推定に入れず「推定に入らないトークン」に出します。その分だけ推定は小さくなります。

**推定が実測より小さくなる理由**
- Langfuse に届かない記録があります。Claude の親セッションは手元の記録より約 26% 少ないことが分かっています（BOXP-567）。Task Board の記録は run の終了後に送られるため、実行中の run はまだ入っていません。
- Pod の外（手元の端末など）で使った分は記録されません。
- 実測の増加はリセットをまたぐ 1 分を除いて数えるため、リセットを含む期間では実測のほうが小さく出ることもあります。

**記録の範囲**: Langfuse の記録は 14 日で消えます。5 時間枠と同時実行の上限は観測していません。会話の本文は表示しません。"))

;; ---- panels ----
;; 推移と使用率で同じ週枠を同じ色にする
(def window-colors (vec (for [[w c] (map vector ws ["green" "yellow" "blue" "purple"])] (by-name (:label w) {"color" {:mode "fixed" :fixedColor c}}))))
(def comparison-sql
  (str/join " UNION ALL "
            (for [w ws :let [mid (per-row w :mid)]]
              ;; 結合キーは Prometheus の label 名 w に合わせ、結合後に「週枠」へ改名する（organize は単一フレームでしか効かないため）
              (str "SELECT '" (:label w) "' AS w, " (est w :mid) " AS " (q "推定の合計（%）") ", " (range-text w) " AS " (q "推定幅（%）") ", "
                   "sumIf(" mid ", ticket != '') AS " (q "うち票に割り当てた分（%）") ", sumIf(" mid ", ticket = '') AS " (q "うち票なしの分（%）")
                   " FROM ai_ops_usage_v1 " where-all))))
;; 結合すると ClickHouse 側の列の表示名に refId が付く（例: 推定の合計（%） B）ため、結合後に元の名前へ戻す
(def comparison-cols ["推定の合計（%）" "推定幅（%）" "うち票に割り当てた分（%）" "うち票なしの分（%）"])
(def comparison
  {:id 10 :type "table" :title "推定の合計と実測の比較（アカウント全体）"
   :description "選んだ期間について、全員分の推定の合計と、実際に週枠が増えた量を並べます。上の票やモデルの選択は効きません。「推定で説明できない分」が大きいほど、記録の欠けや係数のずれが大きいことを示します。"
   :datasource mixed
   :targets [{:refId "A" :datasource prom :expr (str "sum by (w) (" (named (str "sum_over_time(" delta-base "[$__range:1m])")) ")") :instant true :range false :format "table"}
             (assoc (ch-target "table" comparison-sql) :refId "B")]
   :transformations [{:id "joinByField" :options {:byField "w" :mode "outer"}}
                     {:id "organize" :options {:excludeByName {"Time" true} :renameByName (into {"w" "週枠" "Value" "実測の増加（%）"} (for [n comparison-cols] [(str n " B") n]))}}
                     {:id "calculateField" :options {:mode "binary" :alias "推定で説明できない分（%）" :binary {:left "実測の増加（%）" :operator "-" :right "推定の合計（%）"} :replaceFields false}}
                     {:id "organize" :options {:indexByName {"週枠" 0 "実測の増加（%）" 1 "推定の合計（%）" 2 "推定幅（%）" 3 "推定で説明できない分（%）" 4 "うち票に割り当てた分（%）" 5 "うち票なしの分（%）" 6}}}]
   :fieldConfig {:defaults (merge plain-color {:noValue "記録なし" :unit "none" :custom {:align "auto" :wrapHeaderText true :cellOptions {:type "auto"}}})
                 :overrides table-overrides}
   :options {:showHeader true :cellHeight "sm" :footer {:show false}}})

(def ps
  [(text 1 "この画面の読み方" guide)
   (row 2 "票ごとの枠負担（推定）— 上の選択に連動")
   (table 3 "票ごとの枠負担"
          "選んだ期間に各票が使ったトークン数（実測）と、それで減った週枠の推定です。票番号を押すと、その票だけの表示に切り替えられます。"
          (str "SELECT " ticket-expr " AS " (q "票") ", arrayStringConcat(arraySort(groupUniqArray(model)), ', ') AS " (q "使ったモデル") ", " burden-cols
               " FROM ai_ops_usage_v1 " where-selected " GROUP BY " (q "票") " ORDER BY " (col-est first-window) " DESC, " col-mtok " DESC LIMIT 200")
          :overrides [ticket-links title-column (by-name "使ったモデル" {"custom.width" 220})] :titles true :sort (str (:label first-window) " 推定（%）"))
   (table 4 "モデル × 経路ごとの枠負担"
          "票の中で、どのモデルがどの経路（親の作業・委譲した子・レビュー・Hermes など）で週枠を減らしたかの内訳です。票を 1 つ選ぶと、その票の担当モデルを見直す材料になります。"
          (str "SELECT " ticket-expr " AS " (q "票") ", model AS " (q "モデル") ", route AS " (q "経路") ", " burden-cols
               " FROM ai_ops_usage_v1 " where-selected " GROUP BY " (q "票") ", " (q "モデル") ", " (q "経路") " ORDER BY " (col-est first-window) " DESC, " col-mtok " DESC LIMIT 200")
          :overrides [ticket-links title-column (by-name "経路" {"custom.width" 220})] :titles true :sort (str (:label first-window) " 推定（%）"))
   (table 5 "モデル別の枠負担"
          "モデルごとの合計です。「1 回あたりのトークン（万）」はそのモデルの実行 1 回の平均の大きさです（1 万トークン単位）。"
          (str "SELECT model AS " (q "モデル") ", uniqExactIf(ticket, ticket != '') AS " (q "票の数") ", " burden-cols
               ", sum(total_tokens) / 1e4 / nullIf(uniqExact(trace_id), 0) AS " (q "1 回あたりのトークン（万）")
               " FROM ai_ops_usage_v1 " where-selected " GROUP BY " (q "モデル") " ORDER BY " (col-est first-window) " DESC, " col-mtok " DESC"))
   {:id 6 :type "timeseries" :title "推定した週枠消費の推移"
    :description "集計間隔ごとに、選んだ票・モデルが減らした週枠の推定（中央値）です。週枠ごとに別の線で、積み上げていません（1 点が集計間隔 1 つ分）。"
    :datasource ch :interval "$bin" :maxDataPoints 2016
    :targets [(ch-target "timeseries" (str "SELECT " bin " AS time, " (str/join ", " (for [w ws] (str (est w :mid) " AS " (q (:label w))))) " FROM ai_ops_usage_v1 " where-selected " GROUP BY time ORDER BY time"))]
    :fieldConfig {:defaults {:noValue "記録なし" :unit "percent" :decimals 2 :custom {:drawStyle "line" :lineWidth 2 :fillOpacity 0 :showPoints "always" :pointSize 6 :spanNulls false :stacking {:mode "none" :group "A"}}} :overrides window-colors}
    :options {:legend {:displayMode "list" :placement "bottom" :calcs []} :tooltip {:mode "multi"}}}
   (table 7 "記録の欠けと推定に入らない量"
          "推定が小さくなる原因の量です。数が大きいほど推定は実際より小さく出ます。"
          (str "SELECT count() AS " (q "LLM 呼び出し数") ", countIf(has_usage = 0) AS " (q "トークン数のない呼び出し") ", countIf(route = '(未分類)') AS " (q "経路不明の呼び出し")
               ", sumIf(total_tokens, " no-coef ") / 1e6 AS " col-nocoef ", arrayStringConcat(arraySort(groupUniqArrayIf(model, " no-coef ")), ', ') AS " (q "係数のないモデル")
               ", sum(other_tokens) / 1e6 AS " (q "種類不明のトークン（百万）")
               ", (SELECT min(start_time) FROM ai_ops_usage_v1 WHERE start_time >= now() - INTERVAL 30 DAY) AS " (q "最も古い記録")
               " FROM ai_ops_usage_v1 " where-selected)
          :overrides [(by-name "係数のないモデル" {"custom.width" 240}) (by-name "推定に入らないトークン（百万）" {"custom.width" 250})])
   (row 8 "アカウント全体の実測 — 上の選択には連動しません")
   {:id 9 :type "timeseries" :title "週枠の使用率（実測・アカウント全体）"
    :description "Claude / Codex が返す週枠の使用率です（1% 単位）。リセットで 0 付近に戻ります。票ごとの値ではありません。"
    :datasource prom
    :targets [{:refId "A" :datasource prom :expr (named used-now) :legendFormat "{{w}}" :range true :instant false}]
    :fieldConfig {:defaults {:noValue "記録なし" :unit "percent" :decimals 0 :min 0 :max 100 :custom {:spanNulls false :lineWidth 2}} :overrides window-colors}
    :options {:legend {:displayMode "list" :placement "bottom" :calcs []} :tooltip {:mode "multi"}}}
   comparison
   (text 11 "推定の根拠と限界" basis)])
(def sizes [[24 19] [24 1] [24 11] [24 11] [12 9] [12 9] [24 5] [24 1] [24 9] [24 5] [24 19]])
(defn pack [sizes] (loop [ss sizes x 0 y 0 h 0 out []] (if-let [[w hh] (first ss)] (let [wrap (> (+ x w) 24) x (if wrap 0 x) y (if wrap (+ y h) y) h (if wrap 0 h)] (recur (rest ss) (+ x w) y (max h hh) (conj out {:x x :y y :w w :h hh}))) out)))
(def all-value {:text "すべて" :value ["$__all"]})
(def vars
  (concat [{:name "datasource" :label "Prometheus" :type "datasource" :query "prometheus" :hide 2 :current {:text "default" :value "default"}}
           ;; 選択肢は「BOXP-N 題名」で表示し、値は票番号。すべての票ファイル・run のある票と「(票なし)」。
           ;; それでも一覧に無い票（票ファイルを消した票など）は、票番号の直接入力（allowCustomValue）か表の「この票だけ表示」で選べる。
           {:name "ticket" :label "票" :type "query" :datasource prom
            :query {:qryType 3 :refId "PrometheusVariableQueryEditor-VariableQuery"
                    :query (str "query_result(" ticket-display " or label_replace(label_replace(vector(1), \"display\", \"(票なし) Hermes など票番号のない利用\", \"\", \"\"), \"ticket\", \"(票なし)\", \"\", \"\"))")}
            :regex "/display=\"(?<text>[^\"]*)\".*ticket=\"(?<value>[^\"]*)\"/" :sort 8
            :multi true :includeAll true :allValue all-ticket-value :allowCustomValue true :refresh 2 :current all-value}]
          (for [[n label] [["model" "モデル"] ["route" "経路"] ["provider" "提供元"]]]
            {:name n :label label :type "query" :datasource ch :query (str "SELECT DISTINCT " n " FROM ai_ops_usage_v1 WHERE start_time >= now() - INTERVAL 14 DAY ORDER BY " n) :refresh 2 :includeAll true :multi true :current all-value})
          [{:name "bin" :label "集計間隔" :type "custom" :query "1時間 : 1h,6時間 : 6h,1日 : 1d" :current {:text "6時間" :value "6h"}
            :options [{:text "1時間" :value "1h" :selected false} {:text "6時間" :value "6h" :selected true} {:text "1日" :value "1d" :selected false}]}]))
(def dashboard {:title "AI Ops 票ごとの枠負担（推定）と週枠の実測" :uid "ai-ops-ticket-usage" :timezone "Asia/Tokyo" :time {:from "now-7d" :to "now"} :refresh "" :tags ["ai-ops"] :schemaVersion 39
                :links [{:title "AI Ops 運用品質" :url "/d/ai-ops-quality" :type "link" :keepTime true}]
                :templating {:list (vec vars)} :panels (mapv #(assoc %1 :gridPos %2) ps (pack sizes))})
(spit path (str (json/generate-string dashboard) "\n"))
(spit path (:out (proc/shell {:out :string} "jq" "-S" "." path)))
