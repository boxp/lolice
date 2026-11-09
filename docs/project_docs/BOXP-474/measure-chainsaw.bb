#!/usr/bin/env bb
;; Manifest Validation の chainsaw job を実 run から計測して JSON に出す (BOXP-474)。
;; 使い方: measure-chainsaw.bb OUT.json RUN_ID[:ATTEMPT] [RUN_ID[:ATTEMPT] ...]
;; 取得は既存の gh 認証のみを使う。日時と step/case の差は GitHub の値からコードで計算する。
(require '[babashka.process :refer [shell]]
         '[cheshire.core :as json]
         '[clojure.string :as str])

(def repo "boxp/lolice")

(defn gh-text [& args]
  (:out (apply shell {:out :string :err :string :continue true}
               "gh" "api" "--allow-escape-sequences" args)))

(defn gh-json [path] (json/parse-string (gh-text path) true))

(defn sec-between [from to]
  (when (and from to)
    (.getSeconds (java.time.Duration/between (java.time.Instant/parse from)
                                             (java.time.Instant/parse to)))))

(defn step-row [s]
  {:name (:name s) :conclusion (:conclusion s)
   :seconds (sec-between (:started_at s) (:completed_at s))})

(defn job-row [j]
  {:id (:id j) :name (:name j) :conclusion (:conclusion j)
   :created_at (:created_at j) :started_at (:started_at j) :completed_at (:completed_at j)
   :queue_seconds (sec-between (:created_at j) (:started_at j))
   :run_seconds (sec-between (:started_at j) (:completed_at j))
   :steps (mapv step-row (:steps j))})

(def ansi #"\u001b\[[0-9;]*m")
(def ts-prefix #"^\d{4}-\d{2}-\d{2}T[0-9:.]+Z ")

(defn log-lines [job-id]
  (->> (str/split-lines (gh-text (str "repos/" repo "/actions/jobs/" job-id "/logs")))
       (map #(str/replace % "\b" ""))
       (map #(str/replace % ansi ""))
       (map #(str/replace % ts-prefix ""))))

(defn parse-log [lines]
  (let [cases (keep (fn [l]
                      (when-let [[_ r n s] (re-find #"--- (PASS|FAIL): chainsaw/(\S+) \(([0-9.]+)s\)" l)]
                        {:name n :result r :seconds (parse-double s)}))
                    lines)
        steps (keep (fn [l]
                      (when-let [[_ t c st ph] (re-find #"sink\.go:\d+: \| (\d\d:\d\d:\d\d) \| (\S+)\s+\| (\S+)\s+\| TRY\s+\| (BEGIN|END)" l)]
                        {:time t :case c :step st :phase ph}))
                    lines)
        hms (fn [t] (let [[h m s] (map parse-long (str/split t #":"))] (+ (* 3600 h) (* 60 m) s)))
        step-rows (->> (group-by (juxt :case :step) steps)
                       (keep (fn [[[c st] xs]]
                               (let [b (first (filter #(= "BEGIN" (:phase %)) xs))
                                     e (last (filter #(= "END" (:phase %)) xs))]
                                 (when (and b e)
                                   {:case c :step st
                                    :seconds (mod (- (hms (:time e)) (hms (:time b))) 86400)}))))
                       (sort-by (comp - :seconds))
                       vec)
        comp-line (some #(second (re-find #"^Detected components: (.*)" %)) lines)
        started (vec (keep #(second (re-find #"Started chainsaw tests for (\S+)" %)) lines))
        fetch-fail (some #(re-find #"(504|502|503)[^\n]{0,60}(Gateway|Bad|Service)|curl: \(22\)" %) lines)]
    {:detected_components comp-line
     :started_components started
     :cases (vec cases)
     :chainsaw_steps_by_duration step-rows
     :fetch_error_seen (boolean fetch-fail)}))

(defn classify [chainsaw parsed]
  (let [conclusion (:conclusion chainsaw)
        failed-step (->> (:steps chainsaw) (filter #(= "failure" (:conclusion %))) first :name)]
    (cond
      (= "success" conclusion) "normal-success"
      (= "cancelled" conclusion) "cancelled"
      (and (= "failure" conclusion)
           (= "Run chainsaw tests" failed-step)) "test-body-failure"
      (and (= "failure" conclusion) (:fetch_error_seen parsed)) "external-fetch-failure"
      (= "failure" conclusion) (str "setup-failure:" failed-step)
      :else (or conclusion "unknown"))))

(defn measure [spec]
  (let [[id attempt] (str/split spec #":")
        run (gh-json (str "repos/" repo "/actions/runs/" id))
        attempt (or attempt (str (:run_attempt run)))
        jobs (:jobs (gh-json (str "repos/" repo "/actions/runs/" id
                                  "/attempts/" attempt "/jobs?per_page=100")))
        rows (mapv job-row jobs)
        chainsaw (first (filter #(= "chainsaw" (:name %)) rows))
        detect (first (filter #(= "detect" (:name %)) rows))
        parsed (when (and chainsaw (not= "skipped" (:conclusion chainsaw)))
                 (parse-log (log-lines (:id chainsaw))))
        detect-parsed (when detect (parse-log (log-lines (:id detect))))]
    {:run_id (:id run) :attempt (parse-long attempt) :url (:html_url run)
     :head_sha (:head_sha run) :event (:event run)
     :run_conclusion (:conclusion run)
     :detected_components (:detected_components detect-parsed)
     :classification (if chainsaw (classify chainsaw (or parsed {})) "chainsaw-absent")
     :runner_seconds_all_jobs (reduce + 0 (filter #(and (some? %) (not (neg? %))) (map :run_seconds rows)))
     :jobs rows
     :chainsaw_log parsed}))

(let [[out & specs] *command-line-args*
      data (mapv (fn [s] (binding [*out* *err*] (println "measure" s)) (measure s)) specs)]
  (spit out (json/generate-string {:measured_at (str (java.time.Instant/now)) :runs data}
                                  {:pretty true}))
  (println "wrote" out (count data) "runs"))
