#!/usr/bin/env bb
;; BOXP-554 lab: build context for Dockerfile.stage2 from a lolice checkout. The image gets what the Pod
;; gets: the ConfigMap as /bootstrap, the plugin ConfigMaps as /bootstrap-plugins/<name>, and the bootstrap
;; init container's own script (run unchanged before the gateway starts).
;; Usage: stage2-context.bb <lolice/argoproj/hermes-agent> <arch checkout> <out-dir>
;; STAGE2_CONFIG=<file> seeds /opt/data/config.yaml with that file (a copy of the hand-edited one on the
;; production PVC) instead of the ConfigMap's; only its model block is pointed at the loopback stub.
(require '[clj-yaml.core :as yaml] '[babashka.fs :as fs] '[clojure.string :as str])
(let [[src arch out] *command-line-args*
      data (get (yaml/parse-string (slurp (str src "/configmap.yaml")) :keywords false) "data")
      deployment (yaml/parse-string (slurp (str src "/deployment.yaml")) :keywords false)
      init (first (get-in deployment ["spec" "template" "spec" "initContainers"]))
      script (last (get init "command"))
      lab-model "model:\n  provider: custom\n  default: lab\n  model: lab\n  base_url: http://127.0.0.1:18080/v1\n  api_key: lab\n  supports_vision: false\n  context_length: 65536\n"
      config (let [text (if-let [f (System/getenv "STAGE2_CONFIG")] (slurp f) (get data "config.yaml"))
                   replaced (str/replace-first text #"(?m)^model:[ \t]*\n(?:[ \t#].*\n|\n)*" (str/re-quote-replacement lab-model))]
               (assert (not= text replaced) "config.yaml has no top-level model block")
               replaced)]
  (assert (= "bootstrap-config" (get init "name")))
  (doseq [d ["bootstrap" "bootstrap-plugins" "lab" "data"]] (fs/create-dirs (str out "/" d)))
  (doseq [[k v] data] (spit (str out "/bootstrap/" k) v))
  (fs/copy-tree (str src "/plugins") (str out "/bootstrap-plugins"))
  (spit (str out "/lab/init.sh") script)
  (spit (str out "/data/config.yaml") config)
  ;; Same variable names as the production /opt/data/.env; the values are fake.
  (spit (str out "/data/.env") (str/join "\n" ["TELEGRAM_BOT_TOKEN=000000:lab-token" "TELEGRAM_ALLOWED_USERS=1"
                                                "TELEGRAM_HOME_CHANNEL=1000000001" ""]))
  (fs/copy (str arch "/docker/codex-workspace/task-board/task_board_events.bb") (str out "/lab/task_board_events.bb"))
  (println "init script lines:" (count (str/split-lines script)) "configmap keys:" (count data)))
