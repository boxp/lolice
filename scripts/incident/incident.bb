#!/usr/bin/env bb
;; BOXP-769: lolice の障害記録 CLI（仕様: Obsidian Projects/lolice-incident-recovery/spec.md 3 章、
;; 手順: Incidents/Runbooks/incident-response-dora.md）。
;; 障害中の手入力を減らすため、時刻は既定で今、原因の配備は Argo CD の同期記録から引く。
;; 書くのは vault の Incidents/ だけ。Argo CD は kubectl get で読むだけ。
;;
;;   bb incident.bb open --service S --title T [--key K] [--severity sev3] [--started-at T] [--detected-at T]
;;                       --evidence E [--synthetic] [--force]
;;   bb incident.bb cause ID --kind deployment --app A [--revision R --at T] --evidence E
;;   bb incident.bb cause ID --kind non_deployment|unknown --evidence E
;;   bb incident.bb recover ID --action A [--at T] --evidence E
;;   bb incident.bb confirm ID [--at T] --evidence E
;;   bb incident.bb correct ID FIELD VALUE --reason R
;;   bb incident.bb duplicate ID --of INC-M --reason R
;; 共通: --vault DIR（既定 $CODEX_TASK_BOARD_VAULT、無ければ ~/Documents/obsidian-headless/BOXP）
(require '[babashka.fs :as fs]
         '[babashka.process :as p]
         '[cheshire.core :as json]
         '[clojure.string :as str])

(defn die [& xs] (binding [*out* *err*] (println (str/join " " xs))) (System/exit 1))

(defn parse-args [args]
  (loop [[a & more :as xs] args pos [] opts {}]
    (cond (empty? xs) [pos opts]
          (#{"--synthetic" "--force"} a) (recur more pos (assoc opts (keyword (subs a 2)) true))
          (str/starts-with? a "--") (if (seq more)
                                      (recur (rest more) pos (assoc opts (keyword (subs a 2)) (first more)))
                                      (die "値がない:" a))
          :else (recur more (conj pos a) opts))))

(def recovery-kinds #{"rollback" "revert" "hotfix" "forward_fix" "restart" "manual_op" "self_healed" "external" "unknown"})
(def cause-kinds #{"deployment" "non_deployment" "unknown"})
(def time-fields #{"started" "detected" "cause_deployed_at" "recovery_at" "impact_resolved"})
(def correctable
  (into time-fields #{"detected_evidence" "dedupe_key" "service" "severity" "cause" "cause_app" "cause_revision"
                      "cause_evidence" "recovery" "recovery_evidence" "impact_resolved_evidence" "duplicate_of"}))

(defn fmt-instant [^java.time.Instant i] (str (.truncatedTo i java.time.temporal.ChronoUnit/SECONDS)))

(defn parse-time
  "UTC。ISO-8601 か従来の 'YYYY-MM-DD HH:mm'（UTC）。読めなければ nil。"
  [s]
  (let [s (str/trim (or s ""))]
    (or (try (java.time.Instant/parse s) (catch Exception _ nil))
        (try (.toInstant (java.time.OffsetDateTime/parse s)) (catch Exception _ nil))
        (try (-> (java.time.LocalDateTime/parse s (java.time.format.DateTimeFormatter/ofPattern "yyyy-MM-dd HH:mm"))
                 (.toInstant java.time.ZoneOffset/UTC))
             (catch Exception _ nil)))))

(defn now-str [] (or (System/getenv "INCIDENT_NOW") (fmt-instant (java.time.Instant/now))))

(defn time-arg [v what]
  (if (or (nil? v) (= v "now"))
    (now-str)
    (or (some-> (parse-time v) fmt-instant) (die "読めない時刻:" what v))))

(defn need [opts k what] (let [v (some-> (get opts k) str/trim)] (if (str/blank? v) (die what "が要る（--" (name k) "）") v)))

(defn one-line [s] (str/trim (str/replace (str s) #"[\r\n]+" " ")))
(defn q [s] (json/generate-string (one-line s)))

;; ---- files ----

(defn paths [vault]
  {:tickets (fs/path vault "Incidents/Tickets")
   :synthetic (fs/path vault "Incidents/Synthetic")
   :board (fs/path vault "Incidents/Incident Board.md")})

(defn file-of [vault id]
  (let [{:keys [tickets synthetic]} (paths vault)]
    (cond (re-matches #"INC-\d+" id) (fs/path tickets (str id ".md"))
          (re-matches #"SYN-\d+" id) (fs/path synthetic (str id ".md"))
          :else (die "ID の形が違う:" id))))

(defn split-doc [text]
  (if-let [[_ head rest] (re-matches #"(?s)---\n(.*?)\n---\n?(.*)" text)]
    [(str/split-lines head) rest]
    (die "frontmatter が読めない")))

(defn yaml-scalar
  "frontmatter の値 1 つを読む（仕様 2 章 0、収集側 server.py yaml_scalar と同じ決まり）。
  YAML の空値（'' \"\" null ~ コメントだけ）は空欄。それ以外の解釈しない書き方は元の文字列のまま（空とは見なさない）。"
  [raw]
  (let [v (str/trim raw)]
    (cond
      (str/starts-with? v "\"") (or (try (let [x (json/parse-string (str/replace v #"\s+#.*$" ""))]
                                           (when (string? x) (str/trim x)))
                                         (catch Exception _ nil))
                                       v)
      (str/starts-with? v "'") (if-let [[_ x] (re-matches #"'((?:[^']|'')*)'\s*(?:#.*)?" v)]
                                 (str/trim (str/replace x "''" "'"))
                                 v)
      (str/starts-with? v "#") ""
      :else (let [v (str/trim (first (str/split v #"\s#" 2)))]
              (if (#{"" "~" "null" "Null" "NULL"} v) "" v)))))

(defn fm-map [lines]
  (into {} (for [l lines :let [[_ k v] (re-matches #"([A-Za-z_]+):\s?(.*)" l)] :when k]
             [k (yaml-scalar v)])))

(defn read-incident
  "書き換える記録を読む。incident/v1 以外（INC-1〜5 などの legacy）は書き換えない。"
  [vault id]
  (let [f (file-of vault id)]
    (when-not (fs/exists? f) (die "記録が無い:" id))
    (let [[lines body] (split-doc (slurp (str f))) m (fm-map lines)]
      (when-not (= "incident/v1" (m "schema"))
        (die "incident/v1 ではない記録は書き換えない（legacy）:" id))
      {:file f :lines lines :body body :fm m})))

(defn set-fields [lines kvs]
  (reduce (fn [lines [k v]]
            (let [line (str k ": " v)
                  i (first (keep-indexed (fn [i l] (when (re-matches (re-pattern (str k ":.*")) l) i)) lines))]
              (if i (assoc lines i line) (conj lines line))))
          (vec lines) kvs))

(defn append-under [body heading line]
  (let [h (str "## " heading)]
    (if-let [i (str/index-of body h)]
      (let [after (+ i (count h))
            nxt (or (str/index-of body "\n## " after) (count body))
            section (str/trimr (subs body after nxt))]
        (str (subs body 0 after) section "\n" line "\n" (subs body nxt)))
      (str (str/trimr body) "\n\n" h "\n\n" line "\n"))))

(defn write-incident! [{:keys [file lines body]} kvs & {:keys [timeline notes]}]
  (let [body (cond-> body timeline (append-under "Timeline" (str "- " timeline))
                     notes (append-under "Notes" (str "- " notes)))]
    (spit (str file) (str "---\n" (str/join "\n" (set-fields lines kvs)) "\n---\n" body))))

;; ---- board ----

(def lane-of-status {"reported" "Reported" "triage" "Triage" "mitigating" "Mitigating" "monitoring" "Monitoring"
                     "follow-up" "Follow-up" "resolved" "Resolved"})

(defn move-card! [vault id status card]
  (let [b (str (:board (paths vault)))]
    (when (and (str/starts-with? id "INC-") (fs/exists? b))
      (let [text (slurp b)
            pat (re-pattern (str "(?m)^- \\[ \\] \\[\\[Incidents/Tickets/" id "\\|.*\\n?"))
            old (re-find pat text)
            card (or card (some-> old str/trim (str/replace #"status::\S+" (str "status::" status))))
            text (str/replace text pat "")
            h (str "## " (lane-of-status status) "\n")
            i (str/index-of text h)]
        (when (and card i)
          (let [at (+ i (count h))]
            (spit b (str (subs text 0 at) "\n" card "\n" (subs text at)))))))))

;; ---- argocd ----

(defn argocd-app [app]
  (if-let [f (System/getenv "INCIDENT_ARGOCD_JSON")]
    (json/parse-string (slurp f) true)
    (let [r (p/shell {:out :string :err :string :continue true} "kubectl" "get" "applications.argoproj.io" app "-n" "argocd" "-o" "json")]
      (when-not (zero? (:exit r)) (die "Argo CD の Application を読めない:" app (:err r)))
      (json/parse-string (:out r) true))))

(defn cause-deployment
  "影響開始（無ければ検知）以前で最も新しい同期記録。"
  [app before]
  (let [b (parse-time before)
        hs (->> (get-in (argocd-app app) [:status :history])
                (keep (fn [h] (when-let [t (parse-time (:deployedAt h))] (assoc h :inst t))))
                (filter #(not (.isAfter ^java.time.Instant (:inst %) b)))
                (sort-by :inst))]
    (or (last hs) (die "影響開始より前の同期記録が Argo CD に残っていない（--revision と --at で指定する）:" app))))

;; ---- commands ----

(defn resolved?
  "解消済み（仕様 3 章、収集側の recovered と同じ）: impact_resolved が時刻として読め、根拠が空でない。"
  [m]
  (and (some? (parse-time (m "impact_resolved"))) (not (str/blank? (m "impact_resolved_evidence")))))

(defn refuse-if-duplicate!
  "重複として閉じた記録には recover・confirm を書かない（仕様 3 章）。"
  [inc id]
  (let [of ((:fm inc) "duplicate_of")]
    (when-not (str/blank? of)
      (die id "は" of "の重複として閉じている（duplicate_of）。先に correct" id "duplicate_of \"\" --reason R で外す"))))

(defn ids-in [dir prefix]
  (if (fs/exists? dir)
    (keep #(some->> (re-matches (re-pattern (str prefix "-(\\d+)\\.md")) (fs/file-name %)) second parse-long) (fs/list-dir dir))
    []))

(defn open! [vault opts]
  (let [service (need opts :service "service") title (one-line (need opts :title "題"))
        evidence (need opts :evidence "検知の根拠") key (or (:key opts) service)
        {:keys [tickets synthetic]} (paths vault)
        syn? (:synthetic opts)
        dir (if syn? synthetic tickets) prefix (if syn? "SYN" "INC")
        detected (time-arg (:detected-at opts) "--detected-at")
        started (when (:started-at opts) (time-arg (:started-at opts) "--started-at"))
        severity (or (:severity opts) "sev3")]
    (when-not (:force opts)
      ;; 本番と合成事例は別々に重複を確かめる（合成事例が本番の記録を妨げない）
      (doseq [f (if (fs/exists? dir) (fs/list-dir dir) [])
              :when (re-matches (re-pattern (str prefix "-\\d+\\.md")) (fs/file-name f))
              :let [m (fm-map (first (split-doc (slurp (str f)))))]
              :when (and (= "incident/v1" (m "schema")) (= key (m "dedupe_key"))
                         (str/blank? (m "duplicate_of"))
                         (not (resolved? m)))]
        (die "同じ dedupe_key の未解消の記録がある:" (str/replace (fs/file-name f) #"\.md$" "")
             "（同じ障害ならそちらに追記する。別の障害なら --force）")))
    (let [n (inc (reduce max 0 (ids-in dir prefix))) id (str prefix "-" n)
          fields [["id" id] ["type" "incident"] ["schema" "incident/v1"] ["status" "reported"] ["severity" severity]
                  ["service" service] ["dedupe_key" key] ["commander" "boxp"] ["assignee" "boxp"] ["reporter" "boxp"]
                  ["started" (or started "")] ["detected" detected] ["detected_evidence" (q evidence)]
                  ["cause" "unknown"] ["cause_app" ""] ["cause_revision" ""] ["cause_deployed_at" ""] ["cause_evidence" ""]
                  ["recovery" ""] ["recovery_at" ""] ["recovery_evidence" ""]
                  ["impact_resolved" ""] ["impact_resolved_evidence" ""] ["duplicate_of" ""]
                  ["synthetic" (if syn? "true" "false")] ["resolved" ""] ["repo" "boxp/lolice"]]
          body (str "\n# " id ": " title "\n\n## Summary\n\n" title "\n\n## Impact\n\n- Affected service: " service
                    "\n- User-visible symptom:\n\n## Timeline\n\n- " detected ": 検知（" (one-line evidence) "）\n"
                    (when started (str "- " started ": 影響開始\n"))
                    "\n## Verification\n\n- 利用者影響の解消は `confirm` で根拠とともに記録する（アラートの resolved だけでは確定しない）\n"
                    "\n## Root Cause\n\nTBD\n\n## Follow-up Tasks\n\n## Links\n\n- Board: [[Incidents/Incident Board]]\n"
                    "- Runbook: [[Incidents/Runbooks/incident-response-dora]]\n\n## Notes\n\n")]
      (fs/create-dirs dir)
      (spit (str (fs/path dir (str id ".md")))
            (str "---\n" (str/join "\n" (for [[k v] fields] (str k ": " v))) "\ntags:\n  - incident\n---\n" body))
      (when-not syn?
        (move-card! vault id "reported"
                    (str "- [ ] [[Incidents/Tickets/" id "|" id ": " title "]] #incident status::reported severity::"
                         severity " service::" service)))
      (println id))))

(defn cause! [vault id opts]
  (let [inc (read-incident vault id) kind (need opts :kind "原因の種類")
        evidence (need opts :evidence "原因の根拠")]
    (when-not (cause-kinds kind) (die "原因の種類が違う:" kind (sort cause-kinds)))
    (if (= kind "deployment")
      (let [app (need opts :app "原因の Argo CD app")
            [rev at extra] (if (:at opts)
                             [(need opts :revision "revision") (time-arg (:at opts) "--at") nil]
                             (let [before (let [s ((:fm inc) "started")] (if (str/blank? s) ((:fm inc) "detected") s))
                                   h (cause-deployment app before)]
                               [(:revision h) (fmt-instant (:inst h)) (str "argocd history id=" (:id h))]))
            ev (str evidence (when extra (str "; " extra)))]
        (write-incident! inc [["cause" "deployment"] ["cause_app" app] ["cause_revision" rev]
                              ["cause_deployed_at" at] ["cause_evidence" (q ev)]]
                         :timeline (str (now-str) ": 原因 = 配備 " app "@" rev "（deployedAt " at "、" (one-line ev) "）")))
      (write-incident! inc [["cause" kind] ["cause_evidence" (q evidence)]]
                       :timeline (str (now-str) ": 原因 = " kind "（" (one-line evidence) "）")))))

(defn set-status [inc id vault status kvs & more]
  (apply write-incident! inc (conj (vec kvs) ["status" status]) more)
  (move-card! vault id status nil))

(defn recover! [vault id opts]
  (let [inc (doto (read-incident vault id) (refuse-if-duplicate! id)) action (need opts :action "復旧操作") evidence (need opts :evidence "復旧操作の根拠")
        at (time-arg (:at opts) "--at")]
    (when-not (recovery-kinds action) (die "復旧操作の種類が違う:" action (sort recovery-kinds)))
    (set-status inc id vault "mitigating" [["recovery" action] ["recovery_at" at] ["recovery_evidence" (q evidence)]]
                :timeline (str at ": 復旧操作 " action "（" (one-line evidence) "）。利用者影響の解消は未確認"))))

(defn confirm! [vault id opts]
  (let [inc (doto (read-incident vault id) (refuse-if-duplicate! id)) evidence (need opts :evidence "利用者影響の解消を確かめた根拠")
        at (time-arg (:at opts) "--at")]
    (set-status inc id vault "monitoring" [["impact_resolved" at] ["impact_resolved_evidence" (q evidence)]]
                :timeline (str at ": 利用者影響の解消を確認（" (one-line evidence) "）"))))

(defn fm-of-file
  "重複の確認用に frontmatter だけ読む。読めなければ nil（書き換えには使わない）。"
  [f]
  (when (fs/exists? f)
    (when-let [[_ head] (re-matches #"(?s)﻿?---\n(.*?)\n---\n?.*" (slurp (str f)))]
      (fm-map (str/split-lines head)))))

(defn check-duplicate-target!
  "重複は 1 段だけ（仕様 3 章）: 重複先は存在し、自分ではなく、それ自体が重複ではない。
  さらに、この記録を重複先にしている記録があれば、この記録は重複にできない（連鎖・循環を作らない）。"
  [vault id of]
  (when (= of id) (die "自分自身は重複先にできない"))
  (let [target (file-of vault of)]
    (when-not (fs/exists? target) (die "重複先の記録が無い:" of))
    ;; 外してよい重複先だけを許す（仕様 3 章、収集側と同じ許可リスト）: 同じ母集団（本番↔本番・合成↔合成）、
    ;; incident/v1（legacy は数えない記録なので指すと障害が 1 件も数えられなくなる）、それ自体が重複でない
    (when-not (= (subs id 0 3) (subs of 0 3)) (die "本番と合成事例をまたいで重複にはできない:" id "→" of))
    (let [m (fm-of-file target)]
      (when (nil? m) (die "重複先の記録が読めない:" of))
      (when-not (= "incident/v1" (m "schema")) (die "重複先が incident/v1 の記録ではない（legacy は数えないため指せない）:" of))
      (when-not (= (= "true" (m "synthetic")) (str/starts-with? id "SYN-"))
        (die "重複先の synthetic が この記録と違う:" of))
      (when-not (str/blank? (m "duplicate_of"))
        (die "重複先がそれ自体重複:" of "→" (m "duplicate_of") "（大もとの記録を重複先にする）"))))
  (let [{:keys [tickets synthetic]} (paths vault)
        pointing (for [dir [tickets synthetic] :when (fs/exists? dir)
                       f (fs/list-dir dir)
                       :when (re-matches #"(INC|SYN)-\d+\.md" (fs/file-name f))
                       :let [m (fm-of-file f)]
                       :when (and m (= id (str/trim (str (m "duplicate_of"))))) ]
                   (str/replace (fs/file-name f) #"\.md$" ""))]
    (when (seq pointing)
      (die "この記録を重複先にしている記録がある:" (str/join "," pointing) "（先にそちらの duplicate_of を直す）"))))

(defn status-from-fields
  "重複を外したときの status（仕様 3 章）: 確認と根拠あり → monitoring、復旧操作あり → mitigating、他 → reported。"
  [m]
  (cond (resolved? m) "monitoring"
        (not (str/blank? (m "recovery_at"))) "mitigating"
        :else "reported"))

(defn correct! [vault id [field value] opts]
  (let [inc (read-incident vault id) reason (need opts :reason "訂正の理由")]
    (when-not (correctable field) (die "直せない欄:" field (sort correctable)))
    (when (nil? value) (die "新しい値が要る"))
    (let [value (cond (time-fields field) (if (str/blank? value) "" (time-arg value field))
                      (= field "cause") (if (cause-kinds value) value (die "原因の種類が違う:" value))
                      (= field "recovery") (if (recovery-kinds value) value (die "復旧操作の種類が違う:" value))
                      (= field "duplicate_of") (let [v (str/trim value)]
                                                 (when-not (str/blank? v) (check-duplicate-target! vault id v))
                                                 v)
                      :else (one-line value))
          old (str ((:fm inc) field))
          stored (if (str/ends-with? field "_evidence") (q value) value)
          note (str (now-str) ": 訂正: " field " " (if (str/blank? old) "（空）" old) " → "
                    (if (str/blank? value) "（空）" value) "（" (one-line reason) "）")
          ;; 重複と status・カードを揃える（仕様 3 章）: 付ける → resolved、外す → 欄から決め直す
          status (when (= field "duplicate_of")
                   (cond (and (str/blank? old) (not (str/blank? value))) "resolved"
                         (and (not (str/blank? old)) (str/blank? value)) (status-from-fields (:fm inc))))]
      (if status
        (set-status inc id vault status [[field stored]] :notes note
                    :timeline (str (now-str) ": " (if (str/blank? value)
                                                     (str "重複を解除（" old " の重複ではない）、status を " status " に戻す")
                                                     (str value " の重複として閉じる"))
                                   "（" (one-line reason) "）"))
        (write-incident! inc [[field stored]] :notes note)))))

(defn duplicate! [vault id opts]
  (let [of (need opts :of "重複先") reason (need opts :reason "重複の理由")]
    (let [inc (read-incident vault id)]
      (check-duplicate-target! vault id of)
      (set-status inc id vault "resolved" [["duplicate_of" of]]
                  :timeline (str (now-str) ": " of " の重複として閉じる（" (one-line reason) "）")))))

(let [[cmd & args] *command-line-args*
      [pos opts] (parse-args args)
      vault (or (:vault opts) (System/getenv "CODEX_TASK_BOARD_VAULT")
                (str (fs/path (System/getProperty "user.home") "Documents/obsidian-headless/BOXP")))]
  (case cmd
    "open" (open! vault opts)
    "cause" (cause! vault (first pos) opts)
    "recover" (recover! vault (first pos) opts)
    "confirm" (confirm! vault (first pos) opts)
    "correct" (correct! vault (first pos) (rest pos) opts)
    "duplicate" (duplicate! vault (first pos) opts)
    (die "使い方: bb incident.bb (open|cause|recover|confirm|correct|duplicate) ...")))
