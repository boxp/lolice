#!/usr/bin/env bb
;; BOXP-546 lab stub: a scripted OpenAI-compatible LLM and a fake Telegram Bot API on 127.0.0.1.
;; The scenario is read from the prompt (`scenario=<name>`), so every run is deterministic.
(require '[org.httpkit.server :as srv] '[cheshire.core :as json] '[clojure.string :as str])

(def log-dir (or (System/getenv "LAB_LOG_DIR") "/lab/logs"))
(def control-file (str log-dir "/control.json"))
(.mkdirs (java.io.File. log-dir))
(defn log! [name m]
  (spit (str log-dir "/" name ".jsonl") (str (json/generate-string (assoc m :at (str (java.time.Instant/now)))) "\n") :append true))
(defn control [] (try (json/parse-string (slurp control-file) true) (catch Exception _ {})))

(defn text-of [content]
  (cond (string? content) content
        (sequential? content) (str/join "\n" (keep #(when (map? %) (:text %)) content))
        :else ""))

(defn chunk [delta finish]
  (str "data: " (json/generate-string {:id "lab" :object "chat.completion.chunk" :created 0 :model "lab"
                                       :choices [{:index 0 :delta delta :finish_reason finish}]}) "\n\n"))

(defn respond-stream [req parts tool-calls delay-ms]
  (srv/as-channel req
    {:on-open
     (fn [ch]
       (future
         (srv/send! ch {:status 200 :headers {"Content-Type" "text/event-stream"} :body (chunk {:role "assistant" :content ""} nil)} false)
         (doseq [p parts] (Thread/sleep delay-ms) (srv/send! ch (chunk {:content p} nil) false))
         (when (seq tool-calls)
           (srv/send! ch (chunk {:tool_calls (map-indexed (fn [i t] (assoc t :index i)) tool-calls)} nil) false))
         (srv/send! ch (chunk {} (if (seq tool-calls) "tool_calls" "stop")) false)
         (srv/send! ch (str "data: " (json/generate-string {:id "lab" :object "chat.completion.chunk" :created 0 :model "lab" :choices []
                                                              :usage {:prompt_tokens 10 :completion_tokens 5 :total_tokens 15}}) "\n\n") false)
         (srv/send! ch "data: [DONE]\n\n" true)))}))

(defn respond [req stream? text tool-calls & [{:keys [delay-ms parts]}]]
  (if stream?
    (respond-stream req (or parts (if (str/blank? text) [] [text])) tool-calls (or delay-ms 0))
    {:status 200 :headers {"Content-Type" "application/json"}
     :body (json/generate-string
            {:id "lab" :object "chat.completion" :created 0 :model "lab"
             :choices [{:index 0 :finish_reason (if (seq tool-calls) "tool_calls" "stop")
                        :message (cond-> {:role "assistant" :content text} (seq tool-calls) (assoc :tool_calls tool-calls))}]
             :usage {:prompt_tokens 10 :completion_tokens 5 :total_tokens 15}})}))

(defn tool-call [command]
  {:id (str "call_" (subs (str (java.util.UUID/randomUUID)) 0 8)) :type "function"
   :function {:name "terminal" :arguments (json/generate-string {:command command})}})

;; BOXP-556: the agent's own cron tool call. With tool search active the tool is deferred and the model
;; reaches it through the `tool_call` bridge; the deliver rewrite hook must see the underlying call.
(defn cron-call [name deliver]
  {:id (str "call_" (subs (str (java.util.UUID/randomUUID)) 0 8)) :type "function"
   :function {:name "tool_call"
              :arguments (json/generate-string
                          {:calls [{:name "cronjob_manage"
                                    :arguments (cond-> {:action "create" :name name :schedule "every 12h"
                                                        :prompt "cron lab scenario=cron-report"}
                                                 deliver (assoc :deliver deliver))}]})}})

(defn note-command [event ticket result]
  (str "printf '%s\\n' '- 2026-11-14 00:00:00 UTC [kohako-event]: [kohako-event] handled event=" event
       " run=lab result=" result " summary=lab' >> /lab/vault/Tickets/" ticket ".md"))

(defn chat [req]
  (let [body (json/parse-string (slurp (:body req)) true)
        messages (:messages body)
        stream? (boolean (:stream body))
        users (filter #(= "user" (:role %)) messages)
        prompt (text-of (:content (last users)))
        event (or (second (re-find #"event_id=(\S+)" prompt)) "none")
        ;; BOXP-554: the dispatcher builds morning digests itself, so they carry no scenario.
        scenario (or (second (re-find #"scenario=([a-z0-9-]+)" prompt))
                     (when (str/includes? event "-digest-") "receipt-notify") "default")
        ticket (or (second (re-find #"ticket=(\S+)" prompt)) "BOXP-9001")
        after-tool? (= "tool" (:role (last messages)))
        report (str (if (str/includes? event "-digest-") "【Task Board 朝の回収】" "【Task Board 通知】")
                    ticket " lab **本文** scenario=" scenario " event=" event)]
    (log! "llm" {:scenario scenario :event event :stream stream? :messages (count messages) :after-tool after-tool?
                 :tools (mapv #(get-in % [:function :name]) (:tools body))
                 :last-role (:role (last messages))
                 :last-tool (when after-tool? (subs (text-of (:content (last messages))) 0 (min 300 (count (text-of (:content (last messages)))))))})
    (case scenario
      "notify" (respond req stream? report nil)
      "silent" (respond req stream? "[SILENT]" nil)
      "empty" (respond req stream? "" nil)
      "receipt-silent" (if after-tool? (respond req stream? "[SILENT]" nil)
                           (respond req stream? "" [(tool-call (note-command event ticket "silent"))]))
      "receipt-notify" (if after-tool? (respond req stream? report nil)
                           (respond req stream? "途中経過: Notes に記録します。" [(tool-call (note-command event ticket "notified"))]))
      ;; BOXP-554: night handling (marker deferred, then silence) and a reply that arrives late.
      "receipt-deferred" (if after-tool? (respond req stream? "[SILENT]" nil)
                             (respond req stream? "" [(tool-call (note-command event ticket "deferred"))]))
      "slow-notify" (if after-tool?
                      (do (Thread/sleep (* 1000 (or (:slow_seconds (control)) 25))) (respond req stream? report nil))
                      (respond req stream? "" [(tool-call (note-command event ticket "notified"))]))
      "cron-create" (if after-tool? (respond req stream? "作成しました。" nil)
                        (respond req stream? "" [(cron-call "lab-tool-origin" nil)]))
      "cron-create-telegram" (if after-tool? (respond req stream? "作成しました。" nil)
                                 (respond req stream? "" [(cron-call "lab-tool-telegram" "telegram:1000000001")]))
      "cron-create-local" (if after-tool? (respond req stream? "作成しました。" nil)
                              (respond req stream? "" [(cron-call "lab-tool-local" "local")]))
      "cron-report" (respond req stream? "ご主人さま、**lab の定期報告**です。\n- `項目` は正常です。" nil)
      "error" {:status 400 :headers {"Content-Type" "application/json"}
               :body (json/generate-string {:error {:message "lab injected failure" :type "invalid_request_error" :code "lab_failure"}})}
      "error500" {:status 500 :headers {"Content-Type" "application/json"}
                  :body (json/generate-string {:error {:message "lab injected server failure" :type "server_error"}})}
      "slow" (do (Thread/sleep (* 1000 (or (:slow_seconds (control)) 25))) (respond req stream? report nil))
      "stream" (respond req stream? report nil {:delay-ms 400 :parts (mapv #(str "断片" % " ") (range 8))})
      (respond req stream? (str "lab default reply scenario=" scenario) nil))))

(def telegram-count (atom 0))
(defn telegram [req]
  (let [body (json/parse-string (slurp (:body req)) true)
        fail? (:telegram_fail (control))]
    (log! "telegram" {:ok (not fail?) :chat_id (:chat_id body) :thread (:message_thread_id body) :length (count (str (:text body))) :text (:text body)})
    (if fail?
      {:status 502 :headers {"Content-Type" "application/json"} :body "{\"ok\":false,\"description\":\"lab injected\"}"}
      {:status 200 :headers {"Content-Type" "application/json"}
       :body (json/generate-string {:ok true :result {:message_id (swap! telegram-count inc)}})})))

(defn handler [req]
  (let [uri (:uri req)]
    (cond
      (and (= :post (:request-method req)) (str/ends-with? uri "/chat/completions")) (chat req)
      (and (= :post (:request-method req)) (re-find #"^/bot[^/]+/sendMessage$" uri)) (telegram req)
      (str/ends-with? uri "/models") {:status 200 :headers {"Content-Type" "application/json"}
                                      :body (json/generate-string {:object "list" :data [{:id "lab" :object "model"}]})}
      :else (do (log! "other" {:method (:request-method req) :uri uri})
                {:status 404 :headers {"Content-Type" "application/json"} :body "{\"error\":\"lab: not found\"}"}))))

(srv/run-server handler {:ip "127.0.0.1" :port 18080 :thread 16})
(println "lab stub listening on 127.0.0.1:18080")
@(promise)
