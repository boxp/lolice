#!/usr/bin/env bb
;; BOXP-762: lolice DORA collector. 10 分ごとに
;;   1. lolice の公開 git（main と PR head）を blob なしで PVC 上の mirror へ fetch し、
;;   2. Argo CD Application の status.history を read-only で読み（get/list だけの専用 SA）、
;;   3. 台帳（/data/ledger.edn）へ追記して残し（Argo CD は 10 件しか残さない）、
;;   4. lolice.dora で 5 指標を出して /metrics で返す。
;; 書き込むのは自分の PVC だけ。外部へ送るものはない（GitHub からは読むだけ）。
(require '[babashka.fs :as fs]
         '[babashka.process :as p]
         '[cheshire.core :as json]
         '[clojure.edn :as edn]
         '[clojure.string :as str]
         '[org.httpkit.server :as http])

(load-file (str (fs/path (fs/parent *file*) "dora.clj")))
(alias 'dora 'lolice.dora)

(def data-dir (or (System/getenv "DATA_DIR") "/data"))
(def repo-url (or (System/getenv "REPO_URL") "https://github.com/boxp/lolice.git"))
(def interval-s (parse-long (or (System/getenv "INTERVAL_SECONDS") "600")))
(def port (parse-long (or (System/getenv "PORT") "9466")))
(def hotfix-since (some-> (System/getenv "HOTFIX_RECORD_SINCE") java.time.Instant/parse .getEpochSecond))
(def mirror (str (fs/path data-dir "lolice.git")))
(def ledger-file (str (fs/path data-dir "ledger.edn")))
(def windows {"7d" (* 7 dora/day) "28d" (* 28 dora/day)})

(defn log [& xs] (binding [*out* *err*] (println (str (java.time.Instant/now)) (str/join " " xs))))

(defn sh [& args]
  (let [r (apply p/shell {:out :string :err :string :continue true} args)]
    (when-not (zero? (:exit r)) (throw (ex-info (str "command failed: " (first args) " " (second args)) {:err (:err r)})))
    (:out r)))

(defn git [& args] (apply p/shell {:out :string :err :string :continue true} "git" "-C" mirror args))

(defn sync-mirror! []
  (when-not (fs/exists? (fs/path mirror "HEAD"))
    (fs/delete-tree mirror)
    (sh "git" "clone" "--bare" "--filter=blob:none" repo-url mirror))
  (let [r (git "fetch" "--prune" "--filter=blob:none" "origin"
               "+refs/heads/main:refs/heads/main" "+refs/pull/*/head:refs/pull/*/head")]
    (when-not (zero? (:exit r)) (throw (ex-info "git fetch failed" {:err (:err r)})))))

(defn memo
  "成功した結果だけ覚える。nil（未取得 revision・git の失敗）は覚えず、次の fetch 後に問い直す。"
  [f]
  (let [c (atom {})]
    (fn [& a]
      (if (contains? @c a)
        (@c a)
        (let [v (apply f a)]
          (when (some? v) (swap! c assoc a v))
          v)))))

(def tree
  (memo (fn [rev path]
          (let [spec (if (str/blank? path) (str rev "^{tree}") (str rev ":" (str/replace path #"/+$" "")))
                r (git "rev-parse" "--verify" "-q" spec)]
            (cond (zero? (:exit r)) (str/trim (:out r))
                  ;; revision はあるが path が無い（作成前・削除後）は既知の「無し」。仕様 1 の 3a
                  (zero? (:exit (git "cat-file" "-e" (str rev "^{commit}")))) :absent
                  ;; revision 自体が無い・git の失敗は nil（unknown、覚えない）
                  :else nil)))))

(def ancestor?
  (memo (fn [a b] (case (int (:exit (git "merge-base" "--is-ancestor" a b))) 0 true 1 false nil))))

(def pr-earliest
  (memo (fn [n sha]
          (let [ref (str "refs/pull/" n "/head")
                mb (git "merge-base" (str sha "^") ref)]
            (when (zero? (:exit mb))
              (let [r (git "log" "--format=%at" (str (str/trim (:out mb)) ".." ref))]
                (when (zero? (:exit r))
                  (some->> (str/split-lines (:out r)) (remove str/blank?) (map parse-long) seq (apply min)))))))))

(def changes
  (memo (fn [from to paths]
          ;; path 0 個で `git log --` にすると repo 全体が返るので、変更なしとする（仕様 3.1 の 6）
          (if (empty? paths)
            []
            (let [r (apply git "log" "--format=%H%x1f%at%x1f%s%x1f%b%x1e" (str from ".." to) "--"
                           (map #(if (str/blank? %) "." %) paths))]
              (when (zero? (:exit r))
                (vec (for [chunk (str/split (:out r) #"\x1e") :let [chunk (str/triml chunk)]
                           :when (not (str/blank? chunk))
                           :let [[sha at subject body] (str/split chunk #"\x1f" 4)
                                 c {:sha sha :subject subject :body (or body "") :author-time (parse-long at)}
                                 n (some->> (re-find #"\(#(\d+)\)\s*$" subject) second parse-long)]]
                       (assoc c :time (dora/change-time c (if n {n (pr-earliest n sha)} {})))))))))))

(def git-fns {:tree tree :changes changes :ancestor? ancestor?})

(defn epoch [s] (some-> s java.time.Instant/parse .getEpochSecond))

(defn ref-paths
  "Helm の ref source（path なし）が渡す values file: 他の source の helm.valueFiles のうち $ref/ で始まるもの。"
  [spec-sources]
  (into {} (for [s spec-sources :when (:ref s)]
             [(:ref s) (vec (for [o spec-sources f (get-in o [:helm :valueFiles])
                                  :when (str/starts-with? f (str "$" (:ref s) "/"))]
                              (subs f (+ 2 (count (:ref s))))))])))

(defn norm-source [refs s]
  (cond-> {:repo (:repoURL s)}
    (:path s) (assoc :path (:path s))
    (:chart s) (assoc :chart (:chart s))
    (and (not (:path s)) (:ref s)) (assoc :paths (get refs (:ref s) []))))

(defn app-records
  "仕様 1 の 6: 記録の source（path・values file）はその同期記録が持つ source だけから求める。
  今の spec で補うと、後で spec を変えた時に過去の記録の分類が変わるため。source が無い記録は
  :sources nil（分類は unknown）。"
  [app]
  (let [uid (get-in app [:metadata :uid]) nm (get-in app [:metadata :name])]
    (for [h (get-in app [:status :history]) :let [t (epoch (:deployedAt h))] :when t
          :let [srcs (or (seq (:sources h)) (when (seq (get-in h [:source :repoURL])) [(:source h)]))]]
      {:uid uid :app nm :id (:id h) :t t
       :revisions (vec (or (seq (:revisions h)) [(:revision h)]))
       :sources (when srcs (mapv (partial norm-source (ref-paths srcs)) srcs))})))

(defn read-ledger [] (if (fs/exists? ledger-file) (edn/read-string (slurp ledger-file)) {:records {}}))

(defn write-ledger! [l]
  (let [tmp (str ledger-file ".tmp")]
    (spit tmp (pr-str l))
    (fs/move tmp ledger-file {:replace-existing true :atomic-move true})))

(defn merge-records
  "仕様 1 の 7: 台帳は追記だけ。既にある [uid id] の記録は、後から見えた記録で書き換えない。"
  [ledger kvs]
  (update ledger :records (fn [rs] (reduce (fn [rs [k r]] (if (contains? rs k) rs (assoc rs k r))) (or rs {}) kvs))))

(def state (atom {:body "" :last-success nil :apps nil :records 0}))

(defn source-kind [app]
  (let [srcs (or (seq (get-in app [:spec :sources])) [(get-in app [:spec :source])])
        l (count (filter #(dora/lolice-source? {:repo (:repoURL %)}) srcs))]
    (cond (= l (count srcs)) "lolice_git" (zero? l) "external" :else "mixed")))

(defn cycle! []
  (sync-mirror!)
  (let [apps (:items (json/parse-string (sh "kubectl" "get" "applications.argoproj.io" "-n" "argocd" "-o" "json") true))
        ledger (merge-records (read-ledger) (for [a apps r (app-records a)] [[(:uid r) (:id r)] r]))
        _ (write-ledger! ledger)
        by-app (group-by :app (vals (:records ledger)))
        now (quot (System/currentTimeMillis) 1000)
        out (vec (for [a (sort-by #(get-in % [:metadata :name]) apps)
                       :let [nm (get-in a [:metadata :name])
                             kind (source-kind a)
                             ;; 削除して作り直した app の古い uid も同じ名前で残るが、uid の違いで baseline が切れる
                             events (dora/analyze (get by-app nm []) git-fns)]]
                   {:app nm :source-kind kind :origin (dora/origin events)
                    :windows (into {} (for [[w s] windows]
                                        [w (dora/window-metrics events {:now now :window-s s
                                                                        :lead-measurable? (not= kind "external")})]))}))]
    (swap! state assoc :apps out :last-success now :records (count (:records ledger)) :ok true)))

(defn render! []
  (let [{:keys [apps last-success records ok]} @state]
    (swap! state assoc :body (dora/render {:now (quot (System/currentTimeMillis) 1000) :last-success last-success
                                           :ok ok :records records :hotfix-since hotfix-since :apps apps}))))

(defn handler [{:keys [uri]}]
  (case uri
    "/metrics" {:status 200 :headers {"Content-Type" "text/plain; version=0.0.4"} :body (:body @state)}
    "/healthz" {:status 200 :body "ok\n"}
    {:status 404 :body "not found\n"}))

(defn -main []
  (fs/create-dirs data-dir)
  (render!)
  (http/run-server handler {:port port})
  (log "listening on" port)
  (loop []
    (try (cycle!) (log "cycle ok" (:records @state) "records")
         (catch Exception e (swap! state assoc :ok false) (log "cycle failed:" (ex-message e) (pr-str (:err (ex-data e))))))
    (render!)
    (Thread/sleep (* 1000 interval-s))
    (recur)))

(when (= *file* (System/getProperty "babashka.file"))
  (if (= "once" (first *command-line-args*))
    (do (fs/create-dirs data-dir) (cycle!) (render!) (print (:body @state)) (flush))
    (-main)))
