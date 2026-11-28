#!/usr/bin/env bb
;; BOXP-762: Grafana dashboard `lolice-dora` を生成する。
;;   bb argoproj/prometheus-operator/lolice-dora/dashboard.bb > argoproj/prometheus-operator/dashboards/lolice-dora.json
(require '[cheshire.core :as json] '[clojure.string :as str])

(def ds {:type "prometheus" :uid "${datasource}"})
(def sel "app=~\"$app\",window=\"$window\"")

(def status-map
  [{:type "value"
    :options {"measured" {:text "計測済み" :color "green" :index 0}
              "partial" {:text "観測が期間の一部" :color "yellow" :index 1}
              "no_target" {:text "対象0件" :color "blue" :index 2}
              "unmeasured" {:text "未計測（方法なし）" :color "orange" :index 3}
              "unobserved" {:text "未観測" :color "red" :index 4}}}])

(defn q [ref expr & {:as o}]
  (merge {:refId ref :datasource ds :expr expr :instant true :range false :format "table"} o))

(defn status-q [ref metric col]
  (q ref (str "max by (app, " col ") (label_replace(lolice_dora_metric_status{" sel ",metric=\"" metric "\"}, \"" col "\", \"$1\", \"status\", \"(.*)\"))")))

(def text-intro
  (str/join "\n"
   ["**lolice 本番デリバリーの DORA 5 指標（サービス = Argo CD Application）**。仕様: Obsidian `Projects/lolice-dora-metrics/spec.md`（BOXP-762）。AI Ops の票指標・Lean 比較とは別物です。"
    ""
    "- **本番配備** = Argo CD の成功した同期のうち、revision が変わり、その app の source path（Helm の ref は values file）の git tree が変わったもの。PR の merge だけ・同じ revision の再同期・自分の path が変わらない同期は数えません（下の「除外した同期」）。"
    "- **変更リードタイム** = 配備に含まれる commit ごとに、PR の最初の commit（なければ commit）の author 時刻から配備まで。image-updater の commit はアプリ側 repo の build 時間を含みません。"
    "- **変更失敗率 / 復旧時間 / 手直し率** = 7 日以内の revert（`Revert \"… (#N)\"`・`This reverts commit`）、古い commit への rollback、`Hotfix-For: #N` trailer で配備どうしを関連づけたもの。障害記録（INC-*）には配備との関連欄がないため使っていません。hotfix は記録開始（下の時刻）より前は数えられません。"
    "- 空欄は 0 ではありません。状態列が **対象0件**（配備や失敗がない）/ **未計測**（外部 chart だけの app のリードタイム等）/ **観測が期間の一部**（台帳の観測起点が期間の途中）/ **未観測** を示します。Argo CD は 1 app 10 件しか履歴を残さないので、観測起点より前は分かりません。"]))

(def panels
  [{:id 1 :type "text" :title "この画面の読み方" :gridPos {:h 9 :w 16 :x 0 :y 0}
    :options {:mode "markdown" :content text-intro}}
   {:id 2 :type "stat" :title "収集器の最終成功からの経過" :gridPos {:h 3 :w 4 :x 16 :y 0} :datasource ds
    :targets [(q "A" "time() - lolice_dora_collector_last_success_timestamp_seconds" :format "time_series")]
    :fieldConfig {:defaults {:unit "s" :noValue "未取得（値を信じない）"
                             :thresholds {:mode "absolute" :steps [{:color "green" :value nil} {:color "red" :value 1800}]}}
                  :overrides []}
    :options {:reduceOptions {:calcs ["lastNotNull"]} :colorMode "background"}}
   {:id 3 :type "stat" :title "直近の収集" :gridPos {:h 3 :w 4 :x 20 :y 0} :datasource ds
    :targets [(q "A" "lolice_dora_collector_last_run_ok" :format "time_series")]
    :fieldConfig {:defaults {:noValue "収集器なし"
                             :mappings [{:type "value" :options {"1" {:text "成功" :color "green"} "0" {:text "失敗" :color "red"}}}]}
                  :overrides []}
    :options {:reduceOptions {:calcs ["lastNotNull"]} :colorMode "background"}}
   {:id 4 :type "stat" :title "台帳の同期記録数" :gridPos {:h 3 :w 4 :x 16 :y 3} :datasource ds
    :targets [(q "A" "lolice_dora_ledger_records" :format "time_series")]
    :options {:reduceOptions {:calcs ["lastNotNull"]}}}
   {:id 5 :type "stat" :title "hotfix 記録の開始" :gridPos {:h 3 :w 4 :x 20 :y 3} :datasource ds
    :targets [(q "A" "lolice_dora_hotfix_record_since_timestamp_seconds * 1000" :format "time_series")]
    :fieldConfig {:defaults {:unit "dateTimeAsIso"} :overrides []}
    :options {:reduceOptions {:calcs ["lastNotNull"]}}}
   {:id 6 :type "stat" :title "期間内の本番配備（選択サービス合計）" :gridPos {:h 3 :w 8 :x 16 :y 6} :datasource ds
    :targets [(q "A" (str "sum(lolice_dora_deployments{" sel "})") :format "time_series")]
    :options {:reduceOptions {:calcs ["lastNotNull"]}}}
   {:id 10 :type "table" :title "サービス別 DORA 5 指標（期間 $window）" :gridPos {:h 16 :w 24 :x 0 :y 9} :datasource ds
    :targets [(q "A" (str "max by (app) (lolice_dora_deployments{" sel "})"))
              (q "B" (str "max by (app) (lolice_dora_deployments_per_day{" sel "})"))
              (status-q "C" "deployment_frequency" "df_status")
              (q "D" (str "max by (app) (lolice_dora_lead_time_seconds{" sel ",quantile=\"0.5\"})"))
              (q "E" (str "max by (app) (lolice_dora_lead_time_seconds{" sel ",quantile=\"0.9\"})"))
              (status-q "F" "lead_time" "lt_status")
              (q "G" (str "max by (app) (lolice_dora_change_fail_ratio{" sel "})"))
              (q "H" (str "max by (app) (lolice_dora_failed_deployments{" sel "})"))
              (status-q "I" "change_fail_rate" "cfr_status")
              (q "J" (str "max by (app) (lolice_dora_recovery_time_seconds{" sel ",quantile=\"0.5\"})"))
              (status-q "K" "recovery_time" "rt_status")
              (q "L" (str "max by (app) (lolice_dora_rework_ratio{" sel "})"))
              (status-q "M" "rework_rate" "rw_status")
              (q "N" (str "max by (app) (lolice_dora_observed_days{" sel "})"))]
    :transformations [{:id "merge" :options {}}
                      {:id "organize"
                       :options {:excludeByName {"Time" true "__name__" true "window" true "quantile" true
                                                 "container" true "endpoint" true "instance" true "job" true
                                                 "namespace" true "pod" true "service" true}
                                 :indexByName {"app" 0 "Value #N" 1 "Value #A" 2 "Value #B" 3 "df_status" 4
                                               "Value #D" 5 "Value #E" 6 "lt_status" 7 "Value #G" 8 "Value #H" 9
                                               "cfr_status" 10 "Value #J" 11 "rt_status" 12 "Value #L" 13 "rw_status" 14}
                                 :renameByName {"app" "サービス" "Value #N" "観測日数" "Value #A" "配備数" "Value #B" "配備/日"
                                                "df_status" "頻度の状態" "Value #D" "LT 中央値" "Value #E" "LT p90"
                                                "lt_status" "LT の状態" "Value #G" "変更失敗率" "Value #H" "失敗配備"
                                                "cfr_status" "失敗率の状態" "Value #J" "復旧時間 中央値"
                                                "rt_status" "復旧の状態" "Value #L" "手直し率" "rw_status" "手直しの状態"}}}]
    :fieldConfig {:defaults {:custom {:align "auto"}}
                  :overrides (concat
                              (for [c ["頻度の状態" "LT の状態" "失敗率の状態" "復旧の状態" "手直しの状態"]]
                                {:matcher {:id "byName" :options c}
                                 :properties [{:id "mappings" :value status-map}
                                              {:id "custom.cellOptions" :value {:type "color-text"}}]})
                              [{:matcher {:id "byName" :options "観測日数"} :properties [{:id "decimals" :value 1}]}
                               {:matcher {:id "byName" :options "配備/日"} :properties [{:id "decimals" :value 2}]}
                               {:matcher {:id "byName" :options "LT 中央値"} :properties [{:id "unit" :value "s"}]}
                               {:matcher {:id "byName" :options "LT p90"} :properties [{:id "unit" :value "s"}]}
                               {:matcher {:id "byName" :options "復旧時間 中央値"} :properties [{:id "unit" :value "s"}]}
                               {:matcher {:id "byName" :options "変更失敗率"} :properties [{:id "unit" :value "percentunit"} {:id "decimals" :value 1}]}
                               {:matcher {:id "byName" :options "手直し率"} :properties [{:id "unit" :value "percentunit"} {:id "decimals" :value 1}]}])}
    :options {:showHeader true :sortBy [{:displayName "配備数" :desc true}]}}
   {:id 20 :type "table" :title "観測期間と収集範囲（除外した同期・手直しの内訳）" :gridPos {:h 12 :w 24 :x 0 :y 25} :datasource ds
    :targets [(q "A" "max by (app, source_kind) (lolice_dora_app_info{app=~\"$app\"})")
              (q "B" "max by (app) (lolice_dora_observation_start_timestamp_seconds{app=~\"$app\"}) * 1000")
              (q "C" (str "max by (app) (lolice_dora_syncs_excluded{" sel ",reason=\"resync\"})"))
              (q "D" (str "max by (app) (lolice_dora_syncs_excluded{" sel ",reason=\"no_change\"})"))
              (q "E" (str "max by (app) (lolice_dora_syncs_excluded{" sel ",reason=\"unknown\"})"))
              (q "F" (str "max by (app) (lolice_dora_rework_deployments{" sel ",kind=\"revert\"})"))
              (q "G" (str "max by (app) (lolice_dora_rework_deployments{" sel ",kind=\"rollback\"})"))
              (q "H" (str "max by (app) (lolice_dora_rework_deployments{" sel ",kind=\"hotfix\"})"))
              (q "I" (str "max by (app) (lolice_dora_lead_time_changes{" sel "})"))
              (q "J" (str "max by (app) (lolice_dora_recoveries{" sel "})"))]
    :transformations [{:id "merge" :options {}}
                      {:id "organize"
                       :options {:excludeByName {"Time" true "Value #A" true}
                                 :indexByName {"app" 0 "source_kind" 1 "Value #B" 2}
                                 :renameByName {"app" "サービス" "source_kind" "source の種類" "Value #B" "観測起点"
                                                "Value #C" "除外: 再同期" "Value #D" "除外: path 変化なし" "Value #E" "除外: 判定不能"
                                                "Value #F" "手直し: revert" "Value #G" "手直し: rollback" "Value #H" "手直し: hotfix"
                                                "Value #I" "LT の対象 commit 数" "Value #J" "復旧を関連づけた失敗"}}}]
    :fieldConfig {:defaults {}
                  :overrides [{:matcher {:id "byName" :options "観測起点"} :properties [{:id "unit" :value "dateTimeAsIso"}]}
                              {:matcher {:id "byName" :options "source の種類"}
                               :properties [{:id "mappings" :value [{:type "value" :options {"lolice_git" {:text "lolice git"}
                                                                                             "external" {:text "外部 chart のみ（LT 未計測）"}
                                                                                             "mixed" {:text "lolice + 外部 chart"}}}]}]}]}
    :options {:showHeader true}}
   {:id 30 :type "timeseries" :title "期間内の本番配備数の推移（サービス別）" :gridPos {:h 9 :w 24 :x 0 :y 37} :datasource ds
    :targets [{:refId "A" :datasource ds :expr (str "lolice_dora_deployments{" sel "}") :legendFormat "{{app}}"}]
    :fieldConfig {:defaults {:custom {:drawStyle "line" :lineWidth 1 :fillOpacity 0} :decimals 0} :overrides []}
    :options {:legend {:displayMode "table" :placement "right" :calcs ["lastNotNull"]} :tooltip {:mode "multi"}}}])

(def dashboard
  {:uid "lolice-dora"
   :title "lolice 本番デリバリー DORA 指標（BOXP-762）"
   :tags ["dora" "lolice" "boxp-762"]
   :timezone "browser"
   :editable false
   :schemaVersion 39
   :version 1
   :refresh "5m"
   :time {:from "now-7d" :to "now"}
   :templating {:list [{:name "datasource" :label "Data source" :type "datasource" :query "prometheus"
                        :current {:selected true :text "default" :value "default"} :hide 0}
                       {:name "window" :label "期間" :type "custom" :query "7d,28d"
                        :current {:selected true :text "28d" :value "28d"}
                        :options [{:selected false :text "7d" :value "7d"} {:selected true :text "28d" :value "28d"}]}
                       {:name "app" :label "サービス" :type "query" :datasource ds
                        :query {:query "label_values(lolice_dora_app_info, app)" :refId "app"}
                        :definition "label_values(lolice_dora_app_info, app)"
                        :refresh 2 :multi true :includeAll true :allValue ".*"
                        :current {:selected true :text "All" :value "$__all"}}]}
   :panels panels})

(println (json/generate-string dashboard {:pretty true}))
