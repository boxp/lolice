#!/usr/bin/env bb
;; BOXP-554 lab: a fake kohako-home on loopback for the real Task Board dispatcher. It stores what the
;; dispatcher ingests and answers the status query the unread reminder uses (everything stays unread).
(require '[org.httpkit.server :as srv] '[cheshire.core :as json] '[clojure.string :as str])
(def seen (atom #{}))
(def log-path (or (System/getenv "FAKE_LOG") "/lab/logs/home.jsonl"))
(defn log! [m] (spit log-path (str (json/generate-string (assoc m :at (str (java.time.Instant/now)))) "\n") :append true))
(defn handler [req]
  (cond
    (not= (get-in req [:headers "authorization"]) "Bearer home-token") {:status 403 :body "{}"}
    (= :get (:request-method req))
    (let [id (java.net.URLDecoder/decode (or (second (re-find #"ids=([^&]+)" (str (:query-string req)))) "") "UTF-8")]
      (log! {:kind "status" :id id})
      {:status 200 :headers {"content-type" "application/json"}
       :body (json/generate-string {:items {id {:exists (contains? @seen id) :read false :handled false :replied false}}})})
    :else
    (let [body (json/parse-string (slurp (:body req)) true) dup? (contains? @seen (:eventId body))]
      (swap! seen conj (:eventId body))
      (log! {:kind (if dup? "duplicate" "stored") :payload body})
      {:status 200 :headers {"content-type" "application/json"}
       :body (json/generate-string (cond-> {:status (if dup? "duplicate" "stored")}
                                     (not dup?) (assoc :push {:subscriptions 2 :sent 2 :errors 0 :removed 0})))})))
(srv/run-server handler {:ip "127.0.0.1" :port 18190})
(println "home listening")
@(promise)
