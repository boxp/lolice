#!/usr/bin/env bb
;; BOXP-623: 監視入口ダッシュボード (uid alert-entry) を生成する。
;; 判定の正本は PrometheusRule / Alertmanager のまま。このダッシュボードは読むだけで、
;; アラートを評価も通知もしない (Grafana が止まっても通知経路は変わらない)。
(require '[cheshire.core :as json])

(def out "argoproj/prometheus-operator/dashboards/alert-entry.json")
(def prom {:type "prometheus" :uid "${datasource}"})
(def loki {:type "loki" :uid "${loki}"})
;; 通知の入口として一覧に出さないもの: Watchdog は判定経路の生存確認、InfoInhibitor は抑制用の内部アラート
(def internal "Watchdog|InfoInhibitor")
(def sel (str "alertname!~\"" internal "\", alertname=~\"${alertname}\", namespace=~\"${namespace}\", severity=~\"${severity}\""))

(def ids (atom 0))
(defn nid [] (swap! ids inc))

(defn target [expr & {:keys [legend instant fmt ref] :or {ref "A"}}]
  (cond-> {:refId ref :datasource prom :expr expr}
    legend (assoc :legendFormat legend)
    instant (assoc :instant true :range false)
    fmt (assoc :format fmt)))

(defn steps [& xs] {:mode "absolute" :steps (mapv (fn [[c v]] {:color c :value v}) (partition 2 xs))})

(defn stat [title desc expr thresholds & {:keys [mappings unit]}]
  {:id (nid) :type "stat" :title title :description desc :datasource prom
   :targets [(target expr :instant true)]
   :options {:reduceOptions {:calcs ["lastNotNull"] :fields "" :values false}
             :colorMode "background" :graphMode "none" :textMode "value_and_name" :justifyMode "center"}
   :fieldConfig {:defaults (cond-> {:thresholds thresholds :noValue "欠測（Prometheus に届かない）"
                                    :mappings (or mappings [])}
                             unit (assoc :unit unit))
                 :overrides []}})

(defn row [title] {:id (nid) :type "row" :title title :collapsed false :panels []})

(defn text [title md] {:id (nid) :type "text" :title title :options {:mode "markdown" :content md}})

(def guide
  "**この画面は監視の入口です。** 判定と通知の正本は PrometheusRule と Alertmanager（メール / Hermes）のままで、Grafana はそれを読むだけです。Grafana が止まっても通知は届きます。

**通知を受けたら**: 上の `alertname` / `namespace` を通知の値にして絞り込み、表の行のリンクで「発生 30 分前から」の関連ダッシュボード・ログへ進みます。URL で渡すときは `/d/alert-entry/?var-alertname=<alertname>&var-namespace=<namespace>`。

**状態の読み分け**: 正常 = 判定経路（Watchdog）が生きていて一覧に出ない / Pending = 条件成立中で `for` 待ち（まだ通知しない）/ Firing = 通知対象 / 抑制中 = Alertmanager の silence か inhibit で通知を止めている（判定は Firing のまま）/ 欠測 = 値が来ない（Prometheus 停止・取得失敗）/ 評価失敗 = ルールの式が評価できない / 対象消失 = 以前あった監視対象が消えた。欠測や対象消失は「正常」ではありません。")

(def links-md
  "| 見たいもの | 入口 |
|---|---|
| アラートの抑制・silence・通知グループ | [Grafana Alerting（Alertmanager: alertmanager）](/alerting/groups?alertmanager=alertmanager) / [Alertmanager UI](https://alertmanager.b0xp.io/) |
| ルールの定義と式 | [Prometheus Alerts](https://prometheus-web.b0xp.io/alerts) |
| 通知の送信実績 | [Alertmanager / Overview](/d/alertmanager-overview/) |
| クラスタ全体の負荷 | [Kubernetes / Compute Resources / Cluster](/d/efa86fd1d0c121a26444b636a3f509a8/) / [Node Exporter / Nodes](/d/7d57716318ee0dddbac5a7f451fb7753/) |
| AI Ops（Task Board / Hermes の成果・利用枠） | [AI Ops 運用品質](/d/ai-ops-quality/) / [票ごとの枠負担](/d/ai-ops-ticket-usage/) / [Gemini 前払い残高](/d/gemini-prepaid/) |
| Local LLM / GPU | [Local LLM / GPU Overview](/d/local-llm-gpu-overview/) |
| ログ全般 | [Explore（Loki）](/explore) |
| 棚卸し結果と運用手順 | Obsidian `BOXP/Tickets/BOXP-623.md` |")

(def alerts-table
  ;; A = 発生時刻 (ALERTS_FOR_STATE の値は activeAt の UNIX 秒)、B = 調査開始 (その 30 分前)。
  ;; alertstate は ALERTS から group_left で持ってくる。
  (let [base (str "ALERTS_FOR_STATE{" sel "} * ignoring(alertstate) group_left(alertstate) ALERTS{" sel "}")
        ns-link (str "/d/85a562078cdf77779eaa1add43ccec1e/?var-namespace=${__data.fields.namespace}&from=${__data.fields.investigate_from}&to=now")]
    {:id (nid) :type "table" :title "Firing / Pending のアラート（抑制中も含む。行のリンクで掘り下げ）"
     :description "Prometheus の判定結果（ALERTS）。抑制（silence / inhibit）されていても判定は Firing のまま表示されます。通知が止まっているかは上の「抑制中」と Alertmanager で確認します。"
     :datasource prom
     :targets [(target (str "1000 * (" base ")") :instant true :fmt "table" :ref "A")
               (target (str "1000 * ((" base ") - 1800)") :instant true :fmt "table" :ref "B")]
     :transformations [{:id "merge" :options {}}
                       {:id "organize"
                        :options {:excludeByName {"Time" true "__name__" true}
                                  :renameByName {"Value #A" "active_at" "Value #B" "investigate_from"}
                                  :indexByName {"alertstate" 0 "severity" 1 "alertname" 2 "namespace" 3 "active_at" 4}}}]
     :options {:showHeader true :sortBy [{:displayName "active_at" :desc true}] :cellHeight "sm"}
     :fieldConfig
     {:defaults {:custom {:filterable true :align "auto"}}
      :overrides
      [{:matcher {:id "byName" :options "active_at"}
        :properties [{:id "unit" :value "dateTimeAsIso"} {:id "displayName" :value "発生時刻"}]}
       {:matcher {:id "byName" :options "investigate_from"}
        :properties [{:id "custom.hidden" :value true}]}
       {:matcher {:id "byName" :options "alertstate"}
        :properties [{:id "displayName" :value "状態"}
                     {:id "custom.cellOptions" :value {:type "color-background"}}
                     {:id "mappings" :value [{:type "value" :options {"firing" {:text "Firing" :color "red"}
                                                                     "pending" {:text "Pending" :color "yellow"}}}]}]}
       {:matcher {:id "byName" :options "alertname"}
        :properties [{:id "links"
                      :value [{:title "この入口で絞る（発生 30 分前から）"
                               :url "/d/alert-entry/?var-alertname=${__data.fields.alertname}&var-namespace=${__data.fields.namespace}&from=${__data.fields.investigate_from}&to=now"}
                              {:title "namespace の負荷（発生 30 分前から）" :url ns-link}
                              {:title "Alertmanager で抑制・silence を確認"
                               :url "https://alertmanager.b0xp.io/#/alerts?silenced=true&inhibited=true&filter=%7Balertname%3D%22${__data.fields.alertname}%22%7D"
                               :targetBlank true}
                              {:title "ルール定義（Prometheus）"
                               :url "https://prometheus-web.b0xp.io/alerts?search=${__data.fields.alertname}"
                               :targetBlank true}]}]}]}}))

(def timeline
  {:id (nid) :type "state-timeline" :title "いつ鳴っていたか（Pending / Firing の時間帯。最上段「判定経路」が途切れた時間は欠測）"
   :description "最上段の「判定経路（Watchdog）」は Prometheus がルールを評価し、その結果が保存されていた時間に緑で出ます。緑の帯がある時間だけ、下の行の空白を「その時間は鳴っていない（正常）」と読めます。緑が途切れた時間（Prometheus 停止・取得失敗など）は全行が欠測で、空白でも正常とは言えません。"
   :datasource prom
   :targets [(target "max(3 * ALERTS{alertname=\"Watchdog\", alertstate=\"firing\"})" :legend "判定経路（Watchdog）" :ref "A")
             (target (str "max by (alertname, severity) (2 * ALERTS{alertstate=\"firing\", " sel "})"
                          " or max by (alertname, severity) (ALERTS{alertstate=\"pending\", " sel "})")
                     :legend "{{alertname}} ({{severity}})" :ref "B")]
   :options {:showValue "never" :mergeValues true :rowHeight 0.8 :legend {:showLegend false}}
   ;; 欠測区間を前後の値で埋めない (2 分以上サンプルが無い区間は空白にする)
   :fieldConfig {:defaults {:color {:mode "thresholds"}
                            :custom {:insertNulls 120000 :spanNulls false}
                            :thresholds (steps "yellow" nil "red" 2 "green" 3)
                            :mappings [{:type "value" :options {"1" {:text "Pending" :color "yellow"}
                                                               "2" {:text "Firing" :color "red"}
                                                               "3" {:text "判定稼働" :color "green"}}}]}
                 :overrides []}})

(def logs
  {:id (nid) :type "logs" :title "選んだ namespace のエラーらしきログ（Loki）"
   :description "namespace を絞ってから見ます（All のままだと全 namespace の先頭 200 行だけ）。完全な検索は Explore で。"
   :datasource loki
   :targets [{:refId "A" :datasource loki :maxLines 200
              :expr "{namespace=~\"${namespace}\", namespace!=\"\"} |~ \"(?i)(error|fail|fatal|panic|oomkill|timeout)\""}]
   :options {:showTime true :wrapLogMessage true :sortOrder "Descending" :enableLogDetails true}})

(def disappeared
  {:id (nid) :type "table" :title "監視対象の消失・取得失敗"
   :description "消失 = 過去 7 日にはあった job が今は無い（監視の穴になる。7 日を過ぎると表から消えるが、復旧したわけではない）。取得失敗 = 対象は見えているが up=0。"
   :datasource prom
   :targets [(target "label_replace((count by (job) (last_over_time(up[7d]))) unless (count by (job) (up)), \"problem\", \"消失（過去 7 日にはあった）\", \"\", \"\")" :instant true :fmt "table" :ref "A")
             (target "label_replace(count by (job) (up == 0), \"problem\", \"取得失敗（up=0）\", \"\", \"\")" :instant true :fmt "table" :ref "B")]
   :transformations [{:id "merge" :options {}}
                     {:id "organize" :options {:excludeByName {"Time" true}
                                               :renameByName {"Value #A" "消失した対象数（過去 7 日比）" "Value #B" "取得失敗の対象数"}}}]
   :options {:showHeader true :cellHeight "sm"}
   :fieldConfig {:defaults {:noValue "なし"} :overrides []}})

(def notifications
  {:id (nid) :type "timeseries" :title "通知の送信と失敗（Alertmanager、1 時間ごと）"
   :datasource prom
   :targets [(target "sum by (integration) (increase(alertmanager_notifications_total[1h])) > 0" :legend "送信 {{integration}}" :ref "A")
             (target "sum by (integration) (increase(alertmanager_notifications_failed_total[1h])) > 0" :legend "失敗 {{integration}}" :ref "B")]
   :fieldConfig {:defaults {:custom {:drawStyle "bars" :fillOpacity 60} :unit "short"}
                 :overrides [{:matcher {:id "byRegexp" :options "失敗.*"} :properties [{:id "color" :value {:mode "fixed" :fixedColor "red"}}]}]}})

;; 「0 件」は判定経路 (Watchdog) が生きているときだけ補う。Watchdog が見えない = 欠測で、0 と表示しない
(def zero-if-evaluating " or on() (0 * max(ALERTS{alertname=\"Watchdog\", alertstate=\"firing\"}))")
;; Alertmanager 由来の値は Alertmanager が scrape できているときだけ出す (欠測を 0 と見せない)
(def if-am-up " and on() (max(up{job=\"alertmanager-main\"}) == 1)")
(def firing-count (fn [sev] (str "count(ALERTS{alertstate=\"firing\", severity=\"" sev "\", alertname!~\"" internal "\"})" zero-if-evaluating)))

(def panels-spec
  ;; [panel w h]
  [[(text "使い方と状態の読み分け" guide) 24 6]
   [(row "1. いま何が鳴っているか（全体。下の表と時間帯は上の変数で絞れる）") 24 1]
   [(stat "Firing: critical" "メールと Hermes に通知される" (firing-count "critical") (steps "green" nil "red" 1)) 4 4]
   [(stat "Firing: warning" "メールに通知される" (firing-count "warning") (steps "green" nil "orange" 1)) 4 4]
   [(stat "Firing: info" "通知しない（画面で見るだけ）" (firing-count "info") (steps "green" nil "blue" 1)) 4 4]
   [(stat "Pending" "条件成立中で for の待ち。まだ通知しない" (str "count(ALERTS{alertstate=\"pending\", alertname!~\"" internal "\"})" zero-if-evaluating) (steps "green" nil "yellow" 1)) 4 4]
   [(stat "抑制中（silence / inhibit）" "Alertmanager が通知を止めているアラート数。判定は Firing のまま" (str "sum(alertmanager_alerts{state=\"suppressed\"})" if-am-up) (steps "green" nil "purple" 1)) 4 4]
   [(stat "有効な silence" "手動で止めている通知。期限切れを忘れないこと" (str "sum(alertmanager_silences{state=\"active\"})" if-am-up) (steps "green" nil "purple" 1)) 4 4]
   [alerts-table 24 10]
   [timeline 24 9]
   [logs 24 9]
   [(row "2. 監視そのものの健全性（欠測・評価失敗・対象消失・通知失敗）") 24 1]
   [(stat "判定経路（Watchdog）" "常に Firing するはずの Watchdog。1 = Prometheus がルールを評価し続けている"
          "count(ALERTS{alertname=\"Watchdog\", alertstate=\"firing\"}) or vector(0)" (steps "red" nil "green" 1)
          :mappings [{:type "value" :options {"0" {:text "停止"} "1" {:text "生存"}}}]) 4 4]
   [(stat "ルール評価失敗（15 分）" "式が評価できなかった回数（Prometheus）。0 以外は判定が止まっている可能性"
          "sum(increase(prometheus_rule_evaluation_failures_total[15m]))" (steps "green" nil "red" 1)) 4 4]
   [(stat "評価の取りこぼし（15 分）" "評価が間に合わず飛ばした回数"
          "sum(increase(prometheus_rule_group_iterations_missed_total[15m]))" (steps "green" nil "orange" 1)) 4 4]
   [(stat "取得失敗の対象（up=0）" "scrape できない監視対象の数。その対象のアラートは欠測になる"
          "count(up == 0) or on() (0 * count(up))" (steps "green" nil "orange" 1)) 4 4]
   [(stat "消えた job（過去 7 日比）" "過去 7 日のどこかで scrape されていた job のうち、今は無いものの数。監視の穴。消えて 7 日を過ぎると数えられなくなるので、0 に戻っても復旧とは限らない（復旧は下の表と Prometheus Targets で確かめる）"
          "count((count by (job) (last_over_time(up[7d]))) unless (count by (job) (up))) or on() (0 * count(up))" (steps "green" nil "orange" 1)) 4 4]
   [(stat "Prometheus → Alertmanager" "Prometheus が見つけている Alertmanager の数。0 なら判定しても通知されない"
          "min(prometheus_notifications_alertmanagers_discovered)" (steps "red" nil "green" 1)) 4 4]
   [(stat "通知の送信失敗（24h）" "Alertmanager からメール / Hermes への送信失敗。Alertmanager を scrape できないときは 0 ではなく欠測と出る"
          (str "sum(increase(alertmanager_notifications_failed_total[24h]))" if-am-up) (steps "green" nil "red" 1)) 4 4]
   [(stat "通知の破棄（24h）" "Prometheus が Alertmanager へ送れず捨てたアラート"
          "sum(increase(prometheus_notifications_dropped_total[24h]))" (steps "green" nil "red" 1)) 4 4]
   [(stat "設定の再読込" "Prometheus と Alertmanager の最新設定が読めているか（1 = 成功）"
          (str "min(prometheus_config_last_reload_successful) * min(alertmanager_config_last_reload_successful)" if-am-up) (steps "red" nil "green" 1)
          :mappings [{:type "value" :options {"0" {:text "失敗"} "1" {:text "成功"}}}]) 4 4]
   [(stat "Grafana 管理ルール評価失敗（1h）" "Grafana 側に残るルール（Discord 通知の palworld など）の評価失敗。正本ではない補助の判定"
          "sum(increase(grafana_alerting_rule_evaluation_failures_total[1h]))" (steps "green" nil "orange" 1)) 4 4]
   [(stat "Grafana 管理ルール: 発火中" "Grafana 側ルールの Alerting 状態の数（Discord に通知）"
          "sum(grafana_alerting_alerts{state=\"alerting\"})" (steps "green" nil "red" 1)) 4 4]
   [(stat "Grafana 管理ルール: NoData / Error" "Grafana 側ルールの欠測・評価エラーの数"
          "sum(grafana_alerting_alerts{state=~\"nodata|error\"})" (steps "green" nil "orange" 1)) 4 4]
   [disappeared 12 8]
   [notifications 12 8]
   [(row "3. 個別の入口") 24 1]
   [(text "関連ダッシュボードと画面" links-md) 24 9]])

(defn layout [specs]
  (loop [[[p w h] & more] specs x 0 y 0 rowh 0 acc []]
    (if-not p
      acc
      (let [[x y rowh] (if (> (+ x w) 24) [0 (+ y rowh) 0] [x y rowh])]
        (recur more (+ x w) y (max rowh h) (conj acc (assoc p :gridPos {:x x :y y :w w :h h})))))))

(defn var-query [name label q & {:keys [all]}]
  {:name name :label label :type "query" :datasource prom
   :query {:query q :refId (str "var-" name) :qryType 1} :definition q
   :refresh 2 :includeAll true :multi true :allValue (or all ".*")
   :current {:selected true :text ["All"] :value ["$__all"]} :sort 1})

(def dashboard
  {:uid "alert-entry"
   :title "監視の入口：アラートと監視の健全性"
   :description "BOXP-623。判定の正本は PrometheusRule / Alertmanager。ここは入口と健全性確認のための読み取り専用画面。"
   :tags ["monitoring" "alerting" "entry"]
   :timezone "Asia/Tokyo"
   :editable false
   :graphTooltip 1
   :refresh "1m"
   :schemaVersion 39
   :time {:from "now-24h" :to "now"}
   :links [{:type "dashboards" :tags ["ai-ops"] :asDropdown true :title "AI Ops" :keepTime true :includeVars false}
           {:type "dashboards" :tags ["local-llm"] :asDropdown true :title "Local LLM" :keepTime true :includeVars false}
           {:type "link" :title "Alertmanager" :url "https://alertmanager.b0xp.io/" :targetBlank true}
           {:type "link" :title "Prometheus Alerts" :url "https://prometheus-web.b0xp.io/alerts" :targetBlank true}]
   :annotations {:list [{:name "Firing（critical / warning）" :enable true :iconColor "red"
                         :datasource prom
                         :expr (str "ALERTS{alertstate=\"firing\", severity=~\"critical|warning\", alertname!~\"" internal "\"}")
                         :step "60s" :titleFormat "{{alertname}}" :textFormat "{{severity}} {{namespace}}"
                         :useValueForTime false}]}
   :templating
   {:list [{:name "datasource" :label "Prometheus" :type "datasource" :query "prometheus"
            :current {:text "prometheus" :value "prometheus"} :hide 2}
           {:name "loki" :label "Loki" :type "datasource" :query "loki" :current {:text "Loki" :value "Loki"} :hide 2}
           (var-query "alertname" "alertname" "label_values(ALERTS, alertname)")
           (var-query "namespace" "namespace" "label_values(kube_namespace_status_phase, namespace)")
           {:name "severity" :label "severity" :type "custom" :query "critical,warning,info"
            :includeAll true :multi true :allValue ".*"
            :current {:selected true :text ["All"] :value ["$__all"]}
            :options []}]}
   :panels (layout panels-spec)})

(spit out (str (json/generate-string dashboard {:pretty true}) "\n"))
(println "wrote" out)
