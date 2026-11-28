(ns lolice.dora
  "BOXP-762: lolice 本番配備の DORA 5 指標（仕様: Obsidian Projects/lolice-dora-metrics/spec.md）。
  入出力を持たない純粋な判定だけを置く。git の問い合わせは引数の関数で受け取る:
    {:tree      (fn [rev path] tree-sha-or-nil)
     :changes   (fn [from to paths] [{:sha :subject :body :time} ...])  ; :time は仕様 3.1 の変更時刻
     :ancestor? (fn [a b] bool)}                                      ; a が b の祖先か"
  (:require [clojure.string :as str]))

(def day 86400)
(def rework-window (* 7 day))

(defn lolice-source? [{:keys [repo]}]
  (boolean (and repo (re-find #"github\.com[/:]boxp/lolice(\.git)?/?$" repo))))

(defn source-paths
  "lolice source の比べる path。収集器が :paths（Helm の ref source なら $ref/ の valueFiles）を入れる。
  無ければ :path、それも無ければ repo 全体。"
  [src]
  (or (:paths src) [(or (:path src) "")]))

(defn- source-state [git rev src]
  (if (lolice-source? src)
    (mapv #(or ((:tree git) rev %) ::unknown) (source-paths src))
    rev))

(defn- state [git {:keys [revisions sources]}]
  (mapv (partial source-state git) revisions sources))

(defn- lolice-rev [{:keys [revisions sources]}]
  (some (fn [[r s]] (when (lolice-source? s) r)) (map vector revisions sources)))

(defn- changes [git p e]
  (->> (map vector (:revisions p) (:revisions e) (:sources e))
       (filter (fn [[_ _ s]] (lolice-source? s)))
       (mapcat (fn [[from to s]] (when (seq (source-paths s)) ((:changes git) from to (source-paths s)))))
       (reduce (fn [acc c] (if (some #(= (:sha %) (:sha c)) acc) acc (conj acc c))) [])))

(defn- classify-segment [git recs]
  (loop [[e & more] (sort-by :id recs) p nil out []]
    (if-not e
      out
      (let [cls (cond
                  (or (nil? p) (not= (:id e) (inc (:id p)))) :baseline
                  (= (:revisions e) (:revisions p)) :resync
                  :else (let [se (state git e) sp (state git p)]
                          (cond (some #{::unknown} (flatten (concat se sp))) :unknown
                                (= se sp) :no-change
                                :else :deployment)))
            ev (cond-> (assoc e :class cls :prev p)
                 (= cls :deployment) (assoc :changes (changes git p e)))]
        (recur more e (conj out ev))))))

(def ^:private pr-suffix #"\(#(\d+)\)\s*$")

(defn- pr-of [subject] (some-> (re-find pr-suffix (or subject "")) second parse-long))

(defn- targets
  "仕様 3.2 の 2・3: commit から revert / hotfix の対象（{:sha s} か {:pr n}）を拾う。"
  [{:keys [subject body]}]
  (let [subject (or subject "") body (or body "")
        reverts (concat
                 (when-let [[_ inner] (re-find #"^Revert \"(.*)\"" subject)]
                   (when-let [n (pr-of inner)] [{:pr n}]))
                 (for [[_ s] (re-seq #"This reverts commit ([0-9a-f]{7,40})" body)] {:sha s})
                 (for [[_ n] (re-seq #"Reverts [\w.-]+/[\w.-]+#(\d+)" body)] {:pr (parse-long n)}))
        hotfixes (concat
                  (for [[_ n] (re-seq #"(?m)^Hotfix-For:\s*#(\d+)\s*$" body)] {:pr (parse-long n)})
                  (for [[_ s] (re-seq #"(?m)^Hotfix-For:\s*([0-9a-f]{7,40})\s*$" body)] {:sha s}))]
    {:revert (distinct reverts) :hotfix (distinct hotfixes)}))

(defn- contains-target? [d {:keys [sha pr]}]
  (some (fn [c] (if sha (str/starts-with? (or (:sha c) "") sha) (= pr (pr-of (:subject c)))))
        (:changes d)))

(defn- link
  "deployment ごとに手当て（rework）を判定し、手当てされた配備に :failed? と :recovery-s を付ける。"
  [git events]
  (let [v (vec events)
        idx-of-deps (filterv #(= :deployment (:class (v %))) (range (count v)))]
    (reduce
     (fn [v i]
       (let [e (v i)
             ;; 仕様 2: 直前の baseline（観測起点）より前の配備は相手にしない。
             ;; baseline は履歴の欠落・Application 再作成の境目で、その前後は続いていると言えない。
             since (or (last (filter #(= :baseline (:class (v %))) (range i))) -1)
             earlier (filter #(and (< since % i)
                                   (= :deployment (:class (v %)))
                                   (>= (:t (v %)) (- (:t e) rework-window)))
                             idx-of-deps)
             {:keys [revert hotfix]} (->> (:changes e) (map targets)
                                          (apply merge-with concat {:revert [] :hotfix []}))
             find-f (fn [ts] (filter (fn [j] (some #(contains-target? (v j) %) ts)) earlier))
             erev (lolice-rev e) prev-rev (some-> (:prev e) lolice-rev)
             rollback? (boolean (and erev prev-rev (not= erev prev-rev) ((:ancestor? git) erev prev-rev)))
             fs (distinct (concat (find-f revert) (find-f hotfix)
                                  (when rollback?
                                    (remove (fn [j] (let [r (lolice-rev (v j))]
                                                      (or (nil? r) (= r erev) ((:ancestor? git) r erev))))
                                            earlier))))
             kind (cond (seq revert) :revert (seq hotfix) :hotfix rollback? :rollback)]
         (if-not kind
           v
           (reduce (fn [v j]
                     (update v j (fn [f] (cond-> (assoc f :failed? true)
                                           (nil? (:recovery-s f)) (assoc :recovery-s (- (:t e) (:t f)))))))
                   (assoc-in v [i :rework] kind)
                   fs))))
     v idx-of-deps)))

(defn analyze
  "1 app の台帳記録（uid ごとに id が続く）を分類し、手当てを関連づけた event 列を返す。"
  [records git]
  (->> (group-by :uid records)
       vals
       (sort-by #(apply min (map :t %)))
       (mapcat (partial classify-segment git))
       (link git)
       (mapv #(dissoc % :prev))))

(defn origin
  "観測起点 = 最後の baseline の deployedAt（仕様 2）。"
  [events]
  (:t (last (filter #(= :baseline (:class %)) events))))

(defn nearest-rank [q xs]
  (let [s (vec (sort xs)) n (count s)]
    (when (pos? n) (s (dec (max 1 (long (Math/ceil (* q n)))))))))

(defn change-time
  "仕様 3.1: `(#N)` の commit は PR の最初の author 時刻、ほかは commit の author 時刻。"
  [{:keys [subject author-time]} pr-earliest]
  (or (some->> (pr-of subject) (get pr-earliest)) author-time))

(def metric-keys [:deployment-frequency :lead-time :change-fail-rate :recovery-time :rework-rate])

(defn window-metrics [events {:keys [now window-s lead-measurable?]}]
  (let [o (origin events)]
    (if (nil? o)
      {:coverage :unobserved :status (zipmap metric-keys (repeat :unobserved))}
      (let [ws (- now window-s)
            start (max o ws)
            coverage (if (<= o ws) :full :partial)
            base (if (= coverage :full) :measured :partial)
            observed-days (/ (double (- now start)) day)
            in-window (filter #(and (> (:t %) start) (<= (:t %) now)) events)
            deps (filter #(= :deployment (:class %)) in-window)
            n (count deps)
            lts (when lead-measurable?
                  (for [d deps c (:changes d) :when (:time c)] (max 0 (- (:t d) (:time c)))))
            lead (when (seq lts) {:n (count lts) :p50 (nearest-rank 0.5 lts) :p90 (nearest-rank 0.9 lts)})
            failed (count (filter :failed? deps))
            rework (merge {:revert 0 :hotfix 0 :rollback 0} (frequencies (keep :rework deps)))
            recs (keep :recovery-s (filter :failed? deps))
            recovery (if (seq recs) {:n (count recs) :p50 (nearest-rank 0.5 recs)} {:n 0})
            ratio (fn [x] (when (pos? n) (/ (double x) n)))]
        {:coverage coverage
         :observed-days observed-days
         :deployments n
         :per-day (when (pos? observed-days) (/ (double n) observed-days))
         :excluded (merge {:resync 0 :no-change 0 :unknown 0}
                          (frequencies (keep #(#{:resync :no-change :unknown} (:class %)) in-window)))
         :lead lead
         :failed failed
         :change-fail-ratio (ratio failed)
         :rework rework
         :rework-ratio (ratio (reduce + (vals rework)))
         :recovery recovery
         :status {:deployment-frequency base
                  :lead-time (cond (not lead-measurable?) :unmeasured
                                   (zero? n) :no-target
                                   (nil? lead) :unmeasured
                                   :else base)
                  :change-fail-rate (if (zero? n) :no-target base)
                  :recovery-time (if (zero? (:n recovery)) :no-target base)
                  :rework-rate (if (zero? n) :no-target base)}}))))

;; ---- Prometheus exposition ----

(defn- snake [k] (str/replace (name k) "-" "_"))

(defn- esc [v] (-> (str v) (str/replace "\\" "\\\\") (str/replace "\n" "\\n") (str/replace "\"" "\\\"")))

(def ^:private help
  {"collector_last_success_timestamp_seconds" "Unix time of the last collector cycle that finished without error."
   "collector_last_run_ok" "1 when the last collector cycle finished without error."
   "ledger_records" "Argo CD sync records kept in the collector ledger."
   "hotfix_record_since_timestamp_seconds" "Hotfix-For trailers are only recorded from this time on."
   "app_info" "Argo CD Application tracked as a service (source_kind: lolice_git, external, mixed)."
   "observation_start_timestamp_seconds" "Observation origin of the service (latest baseline sync record)."
   "observed_days" "Days of the window that were actually observed."
   "deployments" "Production deployments (changed successful syncs) in the window."
   "deployments_per_day" "Production deployments per observed day."
   "syncs_excluded" "Sync records not counted as deployments, by reason."
   "lead_time_seconds" "Change lead time (commit to production deployment), nearest-rank quantile."
   "lead_time_changes" "Changes behind the lead time quantiles."
   "failed_deployments" "Deployments that needed a rollback, revert or hotfix within 7 days."
   "change_fail_ratio" "Failed deployments / deployments (absent when there is no deployment)."
   "rework_deployments" "Unplanned remediation deployments (rollback, revert, hotfix)."
   "rework_ratio" "Rework deployments / deployments (absent when there is no deployment)."
   "recovery_time_seconds" "Failed deployment recovery time, nearest-rank median."
   "recoveries" "Failed deployments with a linked remediation deployment."
   "metric_status" "Status of each DORA metric (measured, partial, no_target, unmeasured, unobserved)."})

(defn render [{:keys [now last-success ok records hotfix-since apps]}]
  (let [rows (transient [])
        add! (fn [n labels v] (when (and (number? v) (not (Double/isNaN (double v))))
                                (conj! rows [n labels v])))]
    (add! "collector_last_success_timestamp_seconds" nil last-success)
    (add! "collector_last_run_ok" nil (if ok 1 0))
    (add! "ledger_records" nil records)
    (add! "hotfix_record_since_timestamp_seconds" nil hotfix-since)
    (doseq [{:keys [app source-kind origin windows]} apps]
      (add! "app_info" [["app" app] ["source_kind" source-kind]] 1)
      (add! "observation_start_timestamp_seconds" [["app" app]] origin)
      (doseq [[w m] (sort-by key windows)
              :let [l [["app" app] ["window" w]]]]
        (doseq [k metric-keys]
          (add! "metric_status" (conj l ["metric" (snake k)] ["status" (snake (get-in m [:status k]))]) 1))
        (add! "observed_days" l (:observed-days m))
        (add! "deployments" l (:deployments m))
        (add! "deployments_per_day" l (:per-day m))
        (doseq [[r c] (sort-by key (:excluded m))] (add! "syncs_excluded" (conj l ["reason" (snake r)]) c))
        (when-let [{:keys [n p50 p90]} (:lead m)]
          (add! "lead_time_seconds" (conj l ["quantile" "0.5"]) p50)
          (add! "lead_time_seconds" (conj l ["quantile" "0.9"]) p90)
          (add! "lead_time_changes" l n))
        (add! "failed_deployments" l (:failed m))
        (add! "change_fail_ratio" l (:change-fail-ratio m))
        (doseq [[r c] (sort-by key (:rework m))] (add! "rework_deployments" (conj l ["kind" (snake r)]) c))
        (add! "rework_ratio" l (:rework-ratio m))
        (when-let [{:keys [n p50]} (:recovery m)]
          (add! "recoveries" l n)
          (add! "recovery_time_seconds" (conj l ["quantile" "0.5"]) p50))))
    (let [rows (persistent! rows)
          order (distinct (map first rows))
          by (group-by first rows)]
      (str (str/join
            (for [n order :let [full (str "lolice_dora_" n)]]
              (str "# HELP " full " " (help n) "\n# TYPE " full " gauge\n"
                   (str/join (for [[_ labels v] (by n)]
                               (str full (when (seq labels)
                                           (str "{" (str/join "," (for [[k x] labels] (str k "=\"" (esc x) "\""))) "}"))
                                    " " (if (integer? v) v (double v)) "\n"))))))))))
