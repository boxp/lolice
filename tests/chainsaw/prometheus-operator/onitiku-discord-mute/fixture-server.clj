;; BOXP-699 テスト用の偽 Prometheus と webhook 受信口 (babashka)。
;;
;;   POST/GET /api/v1/query_range, /api/v1/query : palserver の available replicas を返す。
;;                                                 値は /mode/down なら 0 (発火)、/mode/ok なら 1 (解消)
;;   POST /hook/<name>                           : Grafana の webhook 通知を記録する
;;   GET  /log                                   : 記録した通知 (name / status / alertname) を JSON で返す
;;   POST /reset                                 : 記録を消す
(require '[org.httpkit.server :as http]
         '[cheshire.core :as json]
         '[clojure.string :as str])

(def mode (atom "ok"))
(def received (atom []))

(defn- json-response [body]
  {:status 200 :headers {"Content-Type" "application/json"} :body (json/generate-string body)})

(defn- prom-value []
  (if (= @mode "down") "0" "1"))

(defn- prom-response [range?]
  (let [now (quot (System/currentTimeMillis) 1000)
        metric {"__name__" "kube_deployment_status_replicas_available" "deployment" "palserver"}]
    (json-response
     {"status" "success"
      "data" (if range?
               {"resultType" "matrix"
                "result" [{"metric" metric "values" [[now (prom-value)]]}]}
               {"resultType" "vector"
                "result" [{"metric" metric "value" [now (prom-value)]}]})})))

(defn- record-hook [name req]
  (let [body (json/parse-string (slurp (:body req)))]
    (swap! received conj {"name" name
                          "status" (get body "status")
                          "alertnames" (mapv #(get-in % ["labels" "alertname"]) (get body "alerts"))})
    (json-response {"ok" true})))

(defn handler [{:keys [uri request-method] :as req}]
  (cond
    (= uri "/api/v1/query_range") (prom-response true)
    (= uri "/api/v1/query") (prom-response false)
    (str/starts-with? uri "/mode/") (do (reset! mode (subs uri 6)) (json-response {"mode" @mode}))
    (and (= request-method :post) (str/starts-with? uri "/hook/")) (record-hook (subs uri 6) req)
    (= uri "/log") (json-response @received)
    (= uri "/reset") (do (reset! received []) (json-response {"ok" true}))
    (= uri "/healthz") (json-response {"mode" @mode})
    :else {:status 404 :body "not found"}))

(http/run-server handler {:port 8080})
(println "fixture-server listening on 8080")
@(promise)
