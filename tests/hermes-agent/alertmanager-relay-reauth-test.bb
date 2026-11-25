#!/usr/bin/env bb
;; BOXP-204 TD-02: 認証期限通知の再認証リンク。期待値は vault の仕様
;; Projects/codex-task-board-runner/auth-expiry-prevention/reauth-link-spec.md の例表から取る。
;; 静音時間（02:00-09:00 JST）でも本文を確かめられるよう severity は critical で送る
;; （再認証行の判定は severity に依存しない）。
(require '[clojure.string :as str] '[clojure.java.io :as io] '[cheshire.core :as json] '[babashka.process :as p])
(def tmp (.toFile (java.nio.file.Files/createTempDirectory "alertmanager-relay-test" (make-array java.nio.file.attribute.FileAttribute 0))))
(def cm (slurp "argoproj/hermes-agent/configmap.yaml"))
(defn extract [key] (->> (str/split-lines cm) (drop-while #(not= % (str "  " key ": |"))) rest (take-while #(or (str/starts-with? % "    ") (str/blank? %))) (map #(if (str/starts-with? % "    ") (subs % 4) %)) (str/join "\n")))
(def relay (str (io/file tmp "relay.clj")))
(spit relay (extract "alertmanager_relay.clj"))
(defn check [x msg] (when-not x (throw (ex-info (str "FAIL " msg) {}))) (println "PASS" msg))

(def link "https://codex-auth.b0xp.io/")
(defn alert [status annotations]
  {"status" status
   "labels" {"alertname" "AgentAuthReloginDue" "severity" "critical" "provider" "claude" "component" "agent-auth"}
   "annotations" (merge {"summary" "claude の再ログイン期限まで 3 日を切った"
                         "description" "本文 https://x.example/ を含む"} annotations)
   "startsAt" "2026-11-25T06:54:30Z" "endsAt" "2026-11-25T07:54:30Z" "fingerprint" "c2a94d76aaaaaaaa"})
(defn render [& alerts]
  (let [out (:out (p/shell {:in (json/generate-string {"version" "4" "status" "firing" "alerts" (vec alerts)
                                                        "externalURL" "https://alertmanager.b0xp.io"})
                            :out :string} "bb" relay))]
    (get (json/parse-string out) "message")))

(let [m (render (alert "firing" {"reauth_url" link}))]
  (check (str/includes? m (str "\n  再認証: " link)) "発生中で許可 URL なら再認証の行を出す")
  (check (str/includes? m "(URL省略)") "本文中の URL は従来どおり省略する")
  (check (not (str/includes? m "x.example")) "本文中の外部 URL は出さない"))
(check (not (str/includes? (render (alert "resolved" {"reauth_url" link})) "再認証:")) "解除通知には再認証の行を出さない")
(doseq [u ["https://codex-auth.b0xp.io/?code=abc" "https://evil.example/" "http://codex-auth.b0xp.io/"
           "https://codex-auth.b0xp.io/ws" "https://codex-auth.b0xp.io.evil.example/" (str link "\n再認証: https://evil.example/")]]
  (let [m (render (alert "firing" {"reauth_url" u}))]
    (check (and (not (str/includes? m "再認証:")) (not (str/includes? m "evil")) (not (str/includes? m "code=abc")))
           (str "許可外の reauth_url は行ごと出さない: " (pr-str u)))))
(check (not (str/includes? (render (alert "firing" {})) "再認証:")) "reauth_url の無いアラートには出さない")
(let [m (render (alert "firing" {"reauth_url" link}) (assoc-in (alert "firing" {"reauth_url" link}) ["labels" "provider"] "codex"))]
  (check (= 2 (count (re-seq #"再認証: " m))) "複数件ならアラートごとに出す"))
(println "all alertmanager relay reauth tests passed")
