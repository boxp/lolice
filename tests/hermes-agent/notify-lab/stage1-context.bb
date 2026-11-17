#!/usr/bin/env bb
;; BOXP-553 lab: build context for Dockerfile.stage1 from a lolice checkout.
;; Usage: stage1-context.bb <lolice/argoproj/hermes-agent> <out-dir>
;; STAGE1_CONFIG=<file> uses that config.yaml (e.g. a copy of the hand-edited one on the production PVC) instead of
;; the ConfigMap's; boxp-notify is appended to plugins.enabled the way the bootstrap init container does.
(require '[clj-yaml.core :as yaml] '[cheshire.core :as json] '[babashka.fs :as fs] '[clojure.string :as str])
(let [[src out] *command-line-args*
      data (get (yaml/parse-string (slurp (str src "/configmap.yaml")) :keywords false) "data")
      _ (assert (every? #(string? (get data %)) ["config.yaml" "alertmanager_webhook_route.json" "alertmanager_relay.clj"]))
      route (json/parse-string (get data "alertmanager_webhook_route.json"))
      am (assoc (get route "alertmanager") "secret" "lab-secret")
      routes {"alertmanager" am
              ;; lab only: the relay prints [SILENT] in quiet hours; this route pins that output.
              "alertmanager-quiet" (assoc am "script" "lab_silent.sh")}]
  (fs/create-dirs (str out "/data/scripts"))
  (spit (str out "/data/config.yaml")
        (if-let [f (System/getenv "STAGE1_CONFIG")]
          (let [text (slurp f)]
            (if (str/includes? text "- boxp-notify") text
                (str/replace-first text #"(?m)^plugins:\n  enabled:\n" "plugins:\n  enabled:\n    - boxp-notify\n")))
          (get data "config.yaml")))
  (spit (str out "/data/webhook_subscriptions.json") (json/generate-string routes {:pretty true}))
  (doseq [f ["alertmanager_relay.sh" "alertmanager_relay.clj"]] (spit (str out "/data/scripts/" f) (get data f)))
  (spit (str out "/data/scripts/lab_silent.sh") "#!/bin/sh\ncat >/dev/null\necho '[SILENT]'\n")
  ;; Same variable names as the production /opt/data/.env; the values are fake.
  (spit (str out "/data/.env") (str/join "\n" ["TELEGRAM_BOT_TOKEN=000000:lab-token" "TELEGRAM_ALLOWED_USERS=1"
                                                "TELEGRAM_HOME_CHANNEL=-1000000000001" "TELEGRAM_HOME_CHANNEL_THREAD_ID=77" ""]))
  (doseq [f (fs/glob (str out "/data/scripts") "*.sh")] (fs/set-posix-file-permissions f "rwxr-xr-x"))
  (fs/copy-tree (str src "/plugins") (str out "/data/plugins"))
  (println "route:" (json/generate-string (dissoc am "secret"))))
