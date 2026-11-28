#!/usr/bin/env bb
;; BOXP-762 / BOXP-769（障害記録の行）: Grafana dashboard `lolice-dora` を生成する。
;;   bb argoproj/prometheus-operator/lolice-dora/dashboard.bb > argoproj/prometheus-operator/dashboards/lolice-dora.json
(require '[cheshire.core :as json] '[clojure.string :as str])

(def ds {:type "prometheus" :uid "${datasource}"})
(def sel "app=~\"$app\",window=\"$window\"")

(def status-map
  [{:type "value"
    :options {"measured" {:text "計測済み" :color "green" :index 0}
              "partial" {:text "観測が期間の一部" :color "yellow" :index 1}
              "no_target" {:text "対象0件" :color "blue" :index 2}
              "incomplete" {:text "変更一覧の取得失敗" :color "purple" :index 3}
              "unmeasured" {:text "未計測（方法なし）" :color "orange" :index 4}
              "unobserved" {:text "未観測" :color "red" :index 5}}}])

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
    "- **変更失敗率 / 復旧時間 / 手直し率** = 7 日以内の revert（`Revert \"… (#N)\"`・`This reverts commit`）、古い commit への rollback、`Hotfix-For: #N` trailer で配備どうしを関連づけたもの。障害記録（INC-*）からの復旧時間は下の「障害記録と復旧時間」に別に出します。hotfix は記録開始（下の時刻）より前は数えられません。"
    "- 空欄は 0 ではありません。状態列が **対象0件**（配備や失敗がない）/ **変更一覧の取得失敗**（git から変更が読めなかった配備が期間内にあり、次の収集で再取得）/ **未計測**（外部 chart だけの app のリードタイム等）/ **観測が期間の一部**（台帳の観測起点が期間の途中）/ **未観測** を示します。Argo CD は 1 app 10 件しか履歴を残さないので、観測起点より前は分かりません。"]))

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

(def isel "synthetic=~\"$synthetic\"")

(def text-incidents
  (str/join "\n"
   ["**障害記録と復旧時間（BOXP-769）**。源は Obsidian `Incidents/Tickets/INC-*.md`（合成事例は `Incidents/Synthetic/SYN-*.md`）の `schema: incident/v1` の記録で、上の表（配備どうしの revert・rollback・hotfix の関連づけ）とは別の源です。足さずに並べて見ます。仕様: `Projects/lolice-incident-recovery/spec.md`、手順: `Incidents/Runbooks/incident-response-dora.md`。"
    ""
    "- **失敗配備からの復旧時間（DORA）** = 原因の配備の deployedAt → 利用者影響の解消を**確かめた**時刻。原因 = 配備を根拠つきで確かめた記録だけ。再配備を伴わない復旧（再起動・手作業）も含みます。"
    "- **障害の復旧時間（一般）** = 影響開始 → 解消の確認。原因を問いません（DORA 外）。"
    "- アラートの resolved・rollback の実行は終点にしません（「復旧操作」列）。確認の根拠がない記録は **未確認**、解消の記録がない記録は **未復旧** で、どちらも値を出しません。v1 より前の記録（INC-1〜5）は集計外です。空欄は 0 ではありません。"]))

(def incident-state-map
  [{:type "value"
    :options {"recovered" {:text "復旧確認済み" :color "green"} "unrecovered" {:text "未復旧" :color "red"}
              "unconfirmed" {:text "解消の根拠なし" :color "orange"}
              "deployment" {:text "配備（確認済み）" :color "purple"} "deployment_unconfirmed" {:text "配備（未確認）" :color "orange"}
              "non_deployment" {:text "配備以外" :color "blue"} "unknown" {:text "不明" :color "yellow"}
              "counted" {:text "集計対象" :color "green"} "duplicate" {:text "重複" :color "text"} "invalid" {:text "記録の誤り" :color "red"}}}])

(defn window-stat [id x title expr unit novalue]
  {:id id :type "stat" :title title :gridPos {:h 4 :w 4 :x x :y 55} :datasource ds
   :targets [(q "A" expr :format "time_series")]
   :fieldConfig {:defaults (cond-> {:noValue novalue} unit (assoc :unit unit :mappings [{:type "value" :options {"-1" {:text "対象0件"}}}])) :overrides []}
   :options {:reduceOptions {:calcs ["lastNotNull"]}}})

;; 件数の「0」は収集が新しく本番の置き場を読めたときだけ出す。収集が止まった・読めないときは値なし（= 不明）にして、障害なしと区別する。
(def collected " and on () (max(lolice_incident_dir_present) == 1) and on () (time() - max(lolice_incident_metrics_generated_timestamp_seconds) < 900)")
(defn guarded-count [expr] (str "(count(" expr ") or vector(0))" collected))
(def unknown-count "不明（収集停止・置き場や記録を読めない）")
;; 中央値も同じ: 収集が健全で対象が無いときだけ -1 を返し「対象0件」と表示する。値なし = 不明（収集停止・読めない記録あり）。
(defn guarded-median [expr] (str "(quantile(0.5, " expr ")" collected ") or (vector(-1)" collected ")"))

(defn resolved-within [days] (str " and on (incident) (lolice_incident_timestamp_seconds{point=\"impact_resolved\"} > time() - " days " * 86400)"))

(def incident-panels
  [{:id 40 :type "text" :title "障害記録の読み方" :gridPos {:h 9 :w 16 :x 0 :y 46}
    :options {:mode "markdown" :content text-incidents}}
   {:id 41 :type "stat" :title "障害記録の収集からの経過" :gridPos {:h 3 :w 4 :x 16 :y 46} :datasource ds
    :targets [(q "A" "time() - max(lolice_incident_metrics_generated_timestamp_seconds)" :format "time_series")]
    :fieldConfig {:defaults {:unit "s" :noValue "未取得（値を信じない）"
                             :thresholds {:mode "absolute" :steps [{:color "green" :value nil} {:color "red" :value 900}]}}
                  :overrides []}
    :options {:reduceOptions {:calcs ["lastNotNull"]} :colorMode "background"}}
   {:id 42 :type "stat" :title "障害記録の置き場" :gridPos {:h 3 :w 4 :x 20 :y 46} :datasource ds
    :targets [(q "A" "max(lolice_incident_dir_present)" :format "time_series")]
    :fieldConfig {:defaults {:noValue "収集なし"
                             :mappings [{:type "value" :options {"1" {:text "読めた" :color "green"} "0" {:text "読めない" :color "red"}}}]}
                  :overrides []}
    :options {:reduceOptions {:calcs ["lastNotNull"]} :colorMode "background"}}
   {:id 43 :type "stat" :title "未復旧・解消の根拠なし" :gridPos {:h 3 :w 4 :x 16 :y 49} :datasource ds
    :targets [(q "A" (guarded-count (str "lolice_incident_info{state=\"counted\",recovery_state=~\"unrecovered|unconfirmed\"," isel "}")) :format "time_series")]
    :fieldConfig {:defaults {:noValue unknown-count :thresholds {:mode "absolute" :steps [{:color "green" :value nil} {:color "red" :value 1}]}} :overrides []}
    :options {:reduceOptions {:calcs ["lastNotNull"]} :colorMode "background"}}
   {:id 44 :type "stat" :title "記録の問題（誤り・重複の疑い・原因未確認）" :gridPos {:h 3 :w 4 :x 20 :y 49} :datasource ds
    :targets [(q "A" (guarded-count (str "count by (incident) (lolice_incident_record_problem{" isel "})")) :format "time_series")]
    :fieldConfig {:defaults {:noValue unknown-count :thresholds {:mode "absolute" :steps [{:color "green" :value nil} {:color "orange" :value 1}]}} :overrides []}
    :options {:reduceOptions {:calcs ["lastNotNull"]} :colorMode "background"}}
   {:id 45 :type "stat" :title "記録の内訳（v1 より前 = 集計外）" :gridPos {:h 3 :w 8 :x 16 :y 52} :datasource ds
    :targets [(q "A" "max by (state) (lolice_incident_records)" :format "time_series" :legendFormat "{{state}}")]
    :options {:reduceOptions {:calcs ["lastNotNull"]} :textMode "value_and_name"}}
   (window-stat 46 0 "DORA 復旧時間 中央値（解消確認が 7 日以内）"
                (guarded-median (str "lolice_incident_failed_deployment_recovery_seconds{" isel "}" (resolved-within 7))) "s" unknown-count)
   (window-stat 47 4 "DORA 復旧時間 中央値（28 日以内）"
                (guarded-median (str "lolice_incident_failed_deployment_recovery_seconds{" isel "}" (resolved-within 28))) "s" unknown-count)
   (window-stat 48 8 "配備起因の復旧確認済み（28 日）"
                (guarded-count (str "lolice_incident_failed_deployment_recovery_seconds{" isel "}" (resolved-within 28))) nil unknown-count)
   (window-stat 49 12 "一般の復旧時間 中央値（28 日）"
                (guarded-median (str "lolice_incident_restore_seconds{" isel "}" (resolved-within 28))) "s" unknown-count)
   (window-stat 50 16 "復旧確認済み（28 日、原因を問わず）"
                ;; 影響開始を省いた記録（open の既定）も数えるため、restore_seconds ではなく復旧状態から数える。
                (guarded-count (str "lolice_incident_info{state=\"counted\",recovery_state=\"recovered\"," isel "}" (resolved-within 28))) nil unknown-count)
   (window-stat 51 20 "原因 不明・配備（未確認）"
                (guarded-count (str "lolice_incident_info{state=\"counted\",cause_state=~\"unknown|deployment_unconfirmed\"," isel "}")) nil unknown-count)
   {:id 52 :type "table" :title "障害記録（v1、重複・誤りも表示）" :gridPos {:h 12 :w 24 :x 0 :y 59} :datasource ds
    :targets [(q "A" (str "max by (incident, service, state, recovery_state, cause_state, recovery, redeploy) (lolice_incident_info{state!=\"legacy\"," isel "})"))
              (q "B" (str "max by (incident) (lolice_incident_timestamp_seconds{point=\"started\"," isel "}) * 1000"))
              (q "C" (str "max by (incident) (lolice_incident_timestamp_seconds{point=\"detected\"," isel "}) * 1000"))
              (q "D" (str "max by (incident) (lolice_incident_timestamp_seconds{point=\"cause_deployed\"," isel "}) * 1000"))
              (q "E" (str "max by (incident) (lolice_incident_timestamp_seconds{point=\"recovery_action\"," isel "}) * 1000"))
              (q "F" (str "max by (incident) (lolice_incident_timestamp_seconds{point=\"impact_resolved\"," isel "}) * 1000"))
              (q "G" (str "max by (incident) (lolice_incident_restore_seconds{" isel "})"))
              (q "H" (str "max by (incident, app) (lolice_incident_failed_deployment_recovery_seconds{" isel "})"))
              (q "I" (str "count by (incident) (lolice_incident_record_problem{" isel "})"))]
    :transformations [{:id "merge" :options {}}
                      {:id "organize"
                       :options {:excludeByName {"Time" true "Value #A" true}
                                 :indexByName {"incident" 0 "service" 1 "state" 2 "recovery_state" 3 "cause_state" 4 "app" 5
                                               "recovery" 6 "redeploy" 7 "Value #B" 8 "Value #C" 9 "Value #D" 10 "Value #E" 11
                                               "Value #F" 12 "Value #G" 13 "Value #H" 14 "Value #I" 15}
                                 :renameByName {"incident" "記録" "service" "サービス" "state" "集計" "recovery_state" "復旧"
                                                "cause_state" "原因" "app" "原因の app" "recovery" "復旧操作" "redeploy" "再配備"
                                                "Value #B" "影響開始" "Value #C" "検知" "Value #D" "原因の配備" "Value #E" "復旧操作の時刻"
                                                "Value #F" "解消の確認" "Value #G" "一般の復旧時間" "Value #H" "DORA 復旧時間"
                                                "Value #I" "記録の問題"}}}]
    :fieldConfig {:defaults {}
                  :overrides (concat
                              (for [c ["集計" "復旧" "原因"]]
                                {:matcher {:id "byName" :options c}
                                 :properties [{:id "mappings" :value incident-state-map}
                                              {:id "custom.cellOptions" :value {:type "color-text"}}]})
                              (for [c ["影響開始" "検知" "原因の配備" "復旧操作の時刻" "解消の確認"]]
                                {:matcher {:id "byName" :options c} :properties [{:id "unit" :value "dateTimeAsIso"}]})
                              (for [c ["一般の復旧時間" "DORA 復旧時間"]]
                                {:matcher {:id "byName" :options c} :properties [{:id "unit" :value "s"}]})
                              [{:matcher {:id "byName" :options "再配備"}
                                :properties [{:id "mappings" :value [{:type "value" :options {"true" {:text "あり"} "false" {:text "なし"} "unknown" {:text "不明"}}}]}]}])}
    :options {:showHeader true :sortBy [{:displayName "検知" :desc true}]}}])

(def dashboard
  {:uid "lolice-dora"
   :title "lolice 本番デリバリー DORA 指標（BOXP-762）"
   :tags ["dora" "lolice" "boxp-762" "boxp-769"]
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
                        :current {:selected true :text "All" :value "$__all"}}
                       {:name "synthetic" :label "障害記録" :type "custom" :query "false,true|false"
                        :current {:selected true :text "本番のみ" :value "false"}
                        :options [{:selected true :text "本番のみ" :value "false"}
                                  {:selected false :text "合成事例を含む" :value "true|false"}]}]}
   :panels (vec (concat panels incident-panels))})

(println (json/generate-string dashboard {:pretty true}))
