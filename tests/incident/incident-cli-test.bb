#!/usr/bin/env bb
;; BOXP-769: 障害記録 CLI（仕様: Obsidian Projects/lolice-incident-recovery/spec.md 3 章）。
;; 期待値は仕様の決まりから書いたもの。一時 vault だけを使い、本物の vault・Argo CD には触れない。
(require '[clojure.test :refer [deftest is testing run-tests]]
         '[clojure.string :as str]
         '[babashka.fs :as fs]
         '[babashka.process :as p]
         '[cheshire.core :as json])

(def root (-> *file* fs/parent fs/parent fs/parent))
(def cli (str (fs/path root "scripts/incident/incident.bb")))

(def board-text
  "---\n\nkanban-plugin: board\n\n---\n\n## Incident Board\n\n- [ ] ルール\n\n\n## Reported\n\n\n\n## Triage\n\n\n\n## Mitigating\n\n\n\n## Monitoring\n\n\n\n## Follow-up\n\n\n\n## Resolved\n\n- [ ] [[Incidents/Tickets/INC-1|INC-1: 既存]] #incident status::resolved severity::sev2 service::k8s\n\n\n%% kanban:settings\n```\n{}\n```\n%%\n")

(def legacy-text "---\nid: INC-1\ntype: incident\nstatus: resolved\nstarted: 2026-08-19 02:10\n---\n\n# INC-1: 既存\n")

(defn vault []
  (let [v (fs/create-temp-dir)]
    (fs/create-dirs (fs/path v "Incidents/Tickets"))
    (spit (str (fs/path v "Incidents/Incident Board.md")) board-text)
    (spit (str (fs/path v "Incidents/Tickets/INC-1.md")) legacy-text)
    (str v)))

(defn run [v now & args]
  (apply p/shell {:out :string :err :string :continue true
                  :extra-env {"INCIDENT_NOW" now "INCIDENT_ARGOCD_JSON" (str (fs/path v "argocd.json"))}}
         "bb" cli (concat args ["--vault" v])))

(defn fm [v id]
  (let [dir (if (str/starts-with? id "SYN-") "Incidents/Synthetic" "Incidents/Tickets")
        text (slurp (str (fs/path v dir (str id ".md"))))
        [_ head] (str/split text #"(?m)^---\s*$" 3)]
    (into {} (for [l (str/split-lines head) :let [[_ k val] (re-matches #"([a-z_]+):\s?(.*)" l)] :when k]
               [k (if (str/starts-with? val "\"") (json/parse-string val) val)]))))

(defn body [v id] (slurp (str (fs/path v "Incidents/Tickets" (str id ".md")))))
(defn board [v] (slurp (str (fs/path v "Incidents/Incident Board.md"))))
(defn lane-of [v id]
  (let [lanes (str/split (board v) #"(?m)^## ")]
    (some (fn [l] (when (str/includes? l (str "[[Incidents/Tickets/" id "|")) (first (str/split-lines l)))) lanes)))

(deftest open-defaults-and-board
  (let [v (vault) r (run v "2026-11-28T10:05:00Z" "open" "--service" "svc-a" "--title" "架空の障害"
                     "--started-at" "2026-11-28T10:00:00Z" "--evidence" "Alertmanager FooDown")]
    (is (zero? (:exit r)) (:err r))
    (is (= "INC-2" (str/trim (:out r))) "既存の最大番号の次")
    (let [f (fm v "INC-2")]
      (is (= "incident/v1" (f "schema")))
      (is (= "2026-11-28T10:05:00Z" (f "detected")) "検知の既定は今")
      (is (= "2026-11-28T10:00:00Z" (f "started")))
      (is (= "Alertmanager FooDown" (f "detected_evidence")))
      (is (= "svc-a" (f "dedupe_key")) "dedupe_key の既定は service")
      (is (= "reported" (f "status")))
      (is (= "" (f "impact_resolved"))))
    (is (= "Reported" (lane-of v "INC-2")))
    (is (str/includes? (body v "INC-2") "2026-11-28T10:05:00Z: 検知"))
    (is (= legacy-text (slurp (str (fs/path v "Incidents/Tickets/INC-1.md")))) "既存記録は書き換えない")))

(deftest open-refuses-same-key-while-unrecovered
  (let [v (vault)]
    (run v "2026-11-28T10:05:00Z" "open" "--service" "svc-a" "--title" "一件目" "--evidence" "alert")
    (let [r (run v "2026-11-28T10:06:00Z" "open" "--service" "svc-a" "--title" "二件目" "--evidence" "alert")]
      (is (not (zero? (:exit r))))
      (is (str/includes? (:err r) "INC-2"))
      (is (not (fs/exists? (fs/path v "Incidents/Tickets/INC-3.md")))))
    (testing "別の key なら作れる、--force でも作れる"
      (is (zero? (:exit (run v "2026-11-28T10:06:00Z" "open" "--service" "svc-b" "--title" "別" "--evidence" "alert"))))
      (is (zero? (:exit (run v "2026-11-28T10:07:00Z" "open" "--service" "svc-a" "--title" "強制" "--evidence" "alert" "--force")))))
    (testing "確認つきで解消した後は同じ key で作れる"
      (run v "2026-11-28T11:00:00Z" "confirm" "INC-2" "--evidence" "外形監視 OK")
      (run v "2026-11-28T11:00:00Z" "duplicate" "INC-4" "--of" "INC-2" "--reason" "同じ障害")
      (is (zero? (:exit (run v "2026-11-28T12:00:00Z" "open" "--service" "svc-a" "--title" "再発" "--evidence" "alert")))))))

(deftest evidence-is-required
  (let [v (vault)]
    (is (not (zero? (:exit (run v "2026-11-28T10:05:00Z" "open" "--service" "svc-a" "--title" "t")))))
    (run v "2026-11-28T10:05:00Z" "open" "--service" "svc-a" "--title" "t" "--evidence" "alert")
    (is (not (zero? (:exit (run v "2026-11-28T10:30:00Z" "confirm" "INC-2")))) "確認の根拠なしは拒否")
    (is (= "" ((fm v "INC-2") "impact_resolved")))
    (is (not (zero? (:exit (run v "2026-11-28T10:30:00Z" "cause" "INC-2" "--kind" "non_deployment")))))
    (is (not (zero? (:exit (run v "2026-11-28T10:30:00Z" "recover" "INC-2" "--action" "nope" "--evidence" "x")))) "列挙外")))

(deftest cause-from-argocd-history
  (let [v (vault)]
    (spit (str (fs/path v "argocd.json"))
          (json/generate-string {:status {:history [{:id 7 :revision "aaa111" :deployedAt "2026-11-28T09:00:00Z"}
                                                    {:id 8 :revision "bbb222" :deployedAt "2026-11-28T09:50:00Z"}
                                                    {:id 9 :revision "ccc333" :deployedAt "2026-11-28T10:20:00Z"}]}}))
    (run v "2026-11-28T10:05:00Z" "open" "--service" "svc-a" "--title" "t" "--started-at" "2026-11-28T10:00:00Z" "--evidence" "alert")
    (let [r (run v "2026-11-28T10:10:00Z" "cause" "INC-2" "--kind" "deployment" "--app" "app-x" "--evidence" "差分を確認")]
      (is (zero? (:exit r)) (:err r))
      (let [f (fm v "INC-2")]
        (is (= "deployment" (f "cause")))
        (is (= "app-x" (f "cause_app")))
        (is (= "bbb222" (f "cause_revision")) "影響開始以前で最も新しい同期記録（10:20 の記録は後なので選ばない）")
        (is (= "2026-11-28T09:50:00Z" (f "cause_deployed_at")))
        (is (str/includes? (f "cause_evidence") "差分を確認"))
        (is (str/includes? (f "cause_evidence") "argocd history id=8"))))))

(deftest recover-is-not-the-end-confirm-is
  (let [v (vault)]
    (run v "2026-11-28T10:05:00Z" "open" "--service" "svc-a" "--title" "t" "--evidence" "alert")
    (run v "2026-11-28T10:20:00Z" "recover" "INC-2" "--action" "restart" "--evidence" "Pod 再起動")
    (let [f (fm v "INC-2")]
      (is (= "restart" (f "recovery")))
      (is (= "2026-11-28T10:20:00Z" (f "recovery_at")))
      (is (= "" (f "impact_resolved")) "復旧操作は終点ではない")
      (is (= "mitigating" (f "status"))))
    (is (= "Mitigating" (lane-of v "INC-2")))
    (run v "2026-11-28T10:30:00Z" "confirm" "INC-2" "--evidence" "外形監視 200 を 3 回")
    (let [f (fm v "INC-2")]
      (is (= "2026-11-28T10:30:00Z" (f "impact_resolved")))
      (is (= "外形監視 200 を 3 回" (f "impact_resolved_evidence")))
      (is (= "monitoring" (f "status"))))
    (is (= "Monitoring" (lane-of v "INC-2")))))

(deftest correct-keeps-old-value-in-notes
  (let [v (vault)]
    (run v "2026-11-28T10:05:00Z" "open" "--service" "svc-a" "--title" "t" "--started-at" "2026-11-28T10:00:00Z" "--evidence" "alert")
    (let [r (run v "2026-11-28T12:00:00Z" "correct" "INC-2" "started" "2026-11-28T09:40:00Z" "--reason" "ログで 09:40 から 5xx")]
      (is (zero? (:exit r)) (:err r)))
    (is (= "2026-11-28T09:40:00Z" ((fm v "INC-2") "started")))
    (is (str/includes? (body v "INC-2") "訂正: started 2026-11-28T10:00:00Z → 2026-11-28T09:40:00Z（ログで 09:40 から 5xx）"))
    (is (not (zero? (:exit (run v "2026-11-28T12:00:00Z" "correct" "INC-2" "started" "きのう" "--reason" "x")))) "読めない時刻は拒否")
    (is (not (zero? (:exit (run v "2026-11-28T12:00:00Z" "correct" "INC-2" "schema" "x" "--reason" "x")))) "直せない欄")
    (is (not (zero? (:exit (run v "2026-11-28T12:00:00Z" "correct" "INC-2" "started" "2026-11-28T09:00:00Z")))) "理由なしは拒否")))

(deftest duplicate-closes
  (let [v (vault)]
    (run v "2026-11-28T10:05:00Z" "open" "--service" "svc-a" "--title" "t" "--evidence" "alert")
    (run v "2026-11-28T10:06:00Z" "open" "--service" "svc-a" "--title" "t2" "--evidence" "alert" "--force")
    (is (not (zero? (:exit (run v "2026-11-28T10:07:00Z" "duplicate" "INC-3" "--of" "INC-9" "--reason" "r")))) "先が無い")
    (is (not (zero? (:exit (run v "2026-11-28T10:07:00Z" "duplicate" "INC-3" "--of" "INC-3" "--reason" "r")))) "自分自身")
    (is (zero? (:exit (run v "2026-11-28T10:07:00Z" "duplicate" "INC-3" "--of" "INC-2" "--reason" "同じ通知"))))
    (let [f (fm v "INC-3")] (is (= "INC-2" (f "duplicate_of"))) (is (= "resolved" (f "status"))))
    (is (= "Resolved" (lane-of v "INC-3")))))

(deftest synthetic-goes-to-its-own-dir-without-board
  (let [v (vault) r (run v "2026-11-28T10:05:00Z" "open" "--service" "svc-a" "--title" "合成" "--evidence" "合成" "--synthetic")]
    (is (zero? (:exit r)) (:err r))
    (is (= "SYN-1" (str/trim (:out r))))
    (is (= "true" ((fm v "SYN-1") "synthetic")))
    (is (not (str/includes? (board v) "SYN-1")))
    (is (zero? (:exit (run v "2026-11-28T10:30:00Z" "confirm" "SYN-1" "--evidence" "合成の確認"))))))

(let [{:keys [fail error]} (run-tests)] (System/exit (if (zero? (+ fail error)) 0 1)))
