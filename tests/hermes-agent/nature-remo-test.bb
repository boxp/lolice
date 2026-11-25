#!/usr/bin/env bb
;; BOXP-700: nature_remo.clj の判定順テスト。
;; 期待値は Projects/kohako-nature-remo/spec.md（現行仕様）の例から書いている。
;; 相手は模擬 HTTP サーバで、実 API の結果ではない。受けた要求の回数と中身を数える。
(require '[clojure.string :as str] '[clojure.java.io :as io] '[cheshire.core :as json]
         '[babashka.process :as p] '[org.httpkit.server :as srv])

(def script "argoproj/hermes-agent/scripts/nature-remo/nature_remo.clj")
(def token "test-token-SECRET-abc123")
(def tmp (.toFile (java.nio.file.Files/createTempDirectory "nature-remo-test" (make-array java.nio.file.attribute.FileAttribute 0))))
(defn path [& xs] (str (apply io/file tmp xs)))

;; ---- 模擬サーバ ----
(def reqs (atom []))
(def responses (atom {}))
(def appliances
  [{"id" "a1" "nickname" "エアコン" "type" "AC" "device" {"id" "d1" "name" "Remo"}
    "settings" {"temp" "26" "temp_unit" "c" "mode" "cool" "vol" "auto" "dir" "" "button" "" "updated_at" "2026-11-24T10:00:00Z"}
    "aircon" {"range" {"modes" {"cool" {"temp" ["18" "18.5" "32"] "vol" ["1" "2" "3" "auto"] "dir" ["1" "swing"] "dirh" [""]}}}}
    "signals" []}
   {"id" "f1" "nickname" "シーリングファン" "type" "IR" "device" {"id" "d1" "name" "Remo"}
    "signals" [{"id" "s1" "name" "電源" "image" "ico_on"} {"id" "s2" "name" "風量" "image" "ico_fan"}]}])
(def devices [{"id" "d1" "name" "Remo" "newest_events" {"te" {"val" 24.5 "created_at" "2026-11-24T10:00:00Z"}
                                                        "hu" {"val" 40 "created_at" "2026-11-24T10:00:00Z"}}}])
(defn handler [req]
  (let [body (some-> (:body req) slurp)
        r {:method (:request-method req) :uri (:uri req)
           :auth (get-in req [:headers "authorization"]) :body body}]
    (swap! reqs conj r)
    (or (get @responses [(:request-method req) (:uri req)])
        (case [(:request-method req) (:uri req)]
          [:get "/1/appliances"] {:status 200 :headers {"Content-Type" "application/json"} :body (json/generate-string appliances)}
          [:get "/1/devices"] {:status 200 :headers {"Content-Type" "application/json"} :body (json/generate-string devices)}
          [:post "/1/signals/s1/send"] {:status 200 :body "{}"}
          [:post "/1/signals/s2/send"] {:status 200 :body "{}"}
          [:post "/1/appliances/a1/aircon_settings"]
          {:status 200 :body (json/generate-string {"temp" "26" "mode" "cool" "vol" "auto" "dir" "" "button" ""})}
          {:status 404 :body "{}"}))))
(def server (srv/run-server handler {:port 0 :legacy-return-value? false}))
(def port (srv/server-port server))
(def closed-port (with-open [s (java.net.ServerSocket. 0)] (.getLocalPort s)))

;; ---- 許可リスト ----
(def allow-full
  {:cooldown-seconds 60
   :appliances {"f1" {:label "リビングのシーリングファン" :signals {"s1" {:label "電源" :toggle true}}}
                "a1" {:label "リビングのエアコン"
                      :aircon {:modes ["cool" "dry"] :temp [20 28] :power ["on" "off"] :vol false :dir false}}}})
(defn write-allow! [m] (spit (path "allow.edn") (pr-str m)))

(defn run [args & {:keys [tok base state] :or {tok token base (str "http://127.0.0.1:" port) state "state"}}]
  (let [env (cond-> (merge (into {} (System/getenv))
                           {"NATURE_REMO_API_BASE" base
                            "NATURE_REMO_ALLOWLIST" (path "allow.edn")
                            "NATURE_REMO_STATE_DIR" (path state)})
              true (dissoc "NATURE_REMO_TOKEN")
              tok (assoc "NATURE_REMO_TOKEN" tok))
        r (apply p/shell {:out :string :err :string :continue true :env env} "bb" script args)]
    {:exit (:exit r) :out (:out r) :err (:err r) :all (str (:out r) (:err r))}))

(def failures (atom 0))
(defn check [x msg] (if x (println "PASS" msg) (do (swap! failures inc) (println "FAIL" msg))))
(defn posts [] (filter #(= :post (:method %)) @reqs))
(defn reset-reqs! [] (reset! reqs []) (reset! responses {}))
(defn no-token-leak [r msg] (check (not (str/includes? (:all r) token)) (str msg ": トークンを出力しない")))

;; ---- 1. 資格情報 / status ----
(write-allow! allow-full)
(let [r (run ["status"] :tok nil)]
  (check (= 3 (:exit r)) "status: トークン変数なしは終了3")
  (check (str/includes? (:out r) "トークン: 未設定") "status: 未設定と表示"))
(doseq [t ["   " "dummy-value-to-be-updated-manually"]]
  (check (= 3 (:exit (run ["status"] :tok t))) (str "status: " (pr-str t) " は未設定扱い")))
(let [r (run ["status"])]
  (check (= 0 (:exit r)) "status: 設定済みは終了0")
  (check (str/includes? (:out r) "トークン: 設定済み") "status: 設定済みと表示")
  (check (str/includes? (:out r) "許可済み家電: 2 件") "status: 許可済み家電の件数")
  (no-token-leak r "status"))
(check (empty? @reqs) "status は通信しない")

;; ---- 2. list / sensors ----
(reset-reqs!)
(let [r (run ["list"])]
  (check (= 0 (:exit r)) "list: 終了0")
  (check (= [[:get "/1/appliances"]] (map (juxt :method :uri) @reqs)) "list: GET /1/appliances を1回だけ")
  (check (= (str "Bearer " token) (:auth (first @reqs))) "list: Bearer 認証で送る")
  (check (str/includes? (:out r) "[AC] エアコン (id=a1)") "list: エアコンの行")
  (check (str/includes? (:out r) "実機の現在状態ではない") "list: settings は実状態ではないと明示")
  (check (str/includes? (:out r) "[IR] シーリングファン (id=f1)") "list: シーリングファンの行")
  (check (str/includes? (:out r) "- 電源 (signal=s1)") "list: 信号の行")
  (no-token-leak r "list"))
(reset-reqs!)
(let [r (run ["sensors"])]
  (check (= 0 (:exit r)) "sensors: 終了0")
  (check (= [[:get "/1/devices"]] (map (juxt :method :uri) @reqs)) "sensors: GET /1/devices を1回だけ")
  (check (str/includes? (:out r) "室温 24.5℃") "sensors: 室温")
  (check (str/includes? (:out r) "2026-11-24T10:00:00Z") "sensors: 測定時刻"))
(reset-reqs!)
(check (= 3 (:exit (run ["list"] :tok nil))) "list: トークン未設定は終了3")
(check (empty? @reqs) "list: トークン未設定なら通信しない")
(reset-reqs!)
(swap! responses assoc [:get "/1/appliances"] {:status 401 :body "{\"code\":401001}"})
(let [r (run ["list"])]
  (check (= 6 (:exit r)) "list: 401 は終了6")
  (check (str/includes? (:all r) "トークン") "list: 401 はトークン無効を案内")
  (check (= 1 (count @reqs)) "list: 401 でも再試行しない")
  (no-token-leak r "list 401"))
(reset-reqs!)
(let [r (run ["list"] :base (str "http://127.0.0.1:" closed-port))]
  (check (= 7 (:exit r)) "list: 通信失敗は終了7")
  (no-token-leak r "list 通信失敗"))

;; ---- 3. 送信の判定順 ----
;; 引数不正
(reset-reqs!)
(check (= 2 (:exit (run ["send" "--appliance" "f1"]))) "send: --signal なしは終了2")
(check (= 2 (:exit (run ["aircon" "--appliance" "a1"]))) "aircon: 変更項目なしは終了2")
(check (= 2 (:exit (run ["bogus"]))) "未知の命令は終了2")
;; 許可リストが空（初期状態）なら全部拒否・通信なし
(write-allow! {})
(let [r (run ["send" "--appliance" "f1" "--signal" "s1" "--execute"])]
  (check (= 4 (:exit r)) "空の許可リスト: send --execute は終了4")
  (check (str/includes? (:all r) "拒否") "空の許可リスト: 拒否と表示"))
(spit (path "allow.edn") "{:appliances [broken")
(check (= 4 (:exit (run ["send" "--appliance" "f1" "--signal" "s1" "--execute"]))) "壊れた許可リストは空として扱う")
(io/delete-file (path "allow.edn"))
(check (= 4 (:exit (run ["send" "--appliance" "f1" "--signal" "s1" "--execute"]))) "許可リストなしは空として扱う")
(write-allow! allow-full)
(check (= 4 (:exit (run ["send" "--appliance" "zz" "--signal" "s1" "--execute"]))) "許可外の家電は終了4")
(check (= 4 (:exit (run ["send" "--appliance" "f1" "--signal" "s2" "--execute"]))) "許可外の信号は終了4")
(check (= 4 (:exit (run ["aircon" "--appliance" "a1" "--mode" "warm" "--execute"]))) "許可外のモードは終了4")
(check (= 4 (:exit (run ["aircon" "--appliance" "a1" "--temp" "30" "--execute"]))) "範囲外の温度は終了4")
(check (= 4 (:exit (run ["aircon" "--appliance" "a1" "--temp" "abc" "--execute"]))) "数でない温度は終了4")
(check (= 4 (:exit (run ["aircon" "--appliance" "a1" "--vol" "3" "--execute"]))) ":vol false なら風量は終了4")
(check (= 4 (:exit (run ["aircon" "--appliance" "f1" "--temp" "26" "--execute"]))) "aircon 許可のない家電は終了4")
(check (= 4 (:exit (run ["send" "--appliance" "a1" "--signal" "s1" "--execute"]))) "信号許可のない家電への send は終了4")
(check (empty? @reqs) "拒否・引数不正では通信しない")
;; --execute なし = 未送信（確認用）、トークン不要、通信なし
(let [r (run ["send" "--appliance" "f1" "--signal" "s1"] :tok nil)]
  (check (= 0 (:exit r)) "確認用 send: 終了0（トークンなしでも）")
  (check (str/includes? (:out r) "未送信（確認用）") "確認用 send: 未送信と表示")
  (check (str/includes? (:out r) "リビングのシーリングファン") "確認用 send: 家電名")
  (check (str/includes? (:out r) "トグル信号") "確認用 send: トグル信号の注意"))
(let [r (run ["aircon" "--appliance" "a1" "--mode" "cool" "--temp" "26"] :tok nil)]
  (check (= 0 (:exit r)) "確認用 aircon: 終了0")
  (check (str/includes? (:out r) "未送信（確認用）") "確認用 aircon: 未送信と表示"))
(check (empty? @reqs) "確認用では通信しない")
(check (not (.exists (io/file (path "state")))) "確認用では送信記録を作らない")
;; --execute でトークン未設定
(check (= 3 (:exit (run ["send" "--appliance" "f1" "--signal" "s1" "--execute"] :tok nil))) "execute: トークン未設定は終了3")
(check (empty? @reqs) "execute: トークン未設定なら通信しない")
;; 実行: 受付
(let [r (run ["send" "--appliance" "f1" "--signal" "s1" "--execute"])]
  (check (= 0 (:exit r)) "execute send: 2xx は終了0")
  (check (str/includes? (:out r) "送信受付") "execute send: 送信受付と表示")
  (check (str/includes? (:out r) "未確認") "execute send: 実動作は未確認と明示")
  (check (str/includes? (:out r) "トグル信号") "execute send: トグル信号の注意")
  (check (= [[:post "/1/signals/s1/send"]] (map (juxt :method :uri) (posts))) "execute send: POST を1回だけ")
  (check (= (str "Bearer " token) (:auth (first (posts)))) "execute send: Bearer 認証")
  (no-token-leak r "execute send"))
(check (not (str/includes? (str/join (map slurp (filter #(.isFile %) (file-seq (io/file (path "state")))))) token))
       "送信記録にトークンを書かない")
;; 待ち時間内の同じ家電は拒否、POST は合計1回のまま
(let [r (run ["send" "--appliance" "f1" "--signal" "s1" "--execute"])]
  (check (= 5 (:exit r)) "待ち時間内の再送は終了5")
  (check (re-find #"あと\d+秒" (:all r)) "待ち時間内: あとN秒と表示")
  (check (= 1 (count (posts))) "待ち時間内: POST は合計1回のまま"))
;; 待ち時間は家電ごと: a1 は送れる
(reset-reqs!)
(let [r (run ["aircon" "--appliance" "a1" "--mode" "cool" "--temp" "26" "--power" "on" "--execute"])]
  (check (= 0 (:exit r)) "別の家電 a1 は待ち時間の影響を受けない")
  (check (= [[:post "/1/appliances/a1/aircon_settings"]] (map (juxt :method :uri) (posts))) "aircon: POST を1回だけ")
  (let [form (into {} (map #(let [[k v] (str/split % #"=" 2)] [k (java.net.URLDecoder/decode (or v "") "UTF-8")])
                           (str/split (or (:body (first (posts))) "") #"&")))]
    (check (= {"operation_mode" "cool" "temperature" "26" "button" ""} form) "aircon: 指定した項目だけを送る（power on は button 空）"))
  (check (str/includes? (:out r) "実機の状態ではない") "aircon: 記録された設定は実状態ではないと明示"))
;; power off
(write-allow! (assoc allow-full :cooldown-seconds 0))
(reset-reqs!)
(run ["aircon" "--appliance" "a1" "--power" "off" "--execute"])
(check (= "button=power-off" (:body (first (posts)))) "aircon: power off は button=power-off")
;; HTTP 拒否は再送しない
(doseq [[status code] [[500 6] [429 6] [401 6]]]
  (reset-reqs!)
  (swap! responses assoc [:post "/1/signals/s1/send"] {:status status :headers {"X-Rate-Limit-Reset" "1790000000"} :body "{}"})
  (let [r (run ["send" "--appliance" "f1" "--signal" "s1" "--execute"])]
    (check (= code (:exit r)) (str "HTTP " status " は終了" code))
    (check (= 1 (count (posts))) (str "HTTP " status " でも再送しない"))
    (check (str/includes? (:all r) "再送しない") (str "HTTP " status ": 再送しないと表示"))
    (no-token-leak r (str "HTTP " status))))
;; 通信失敗 = 結果不明、記録済みなので直後の再実行は待ち時間で拒否
(write-allow! allow-full)
(reset-reqs!)
(let [r (run ["send" "--appliance" "f1" "--signal" "s1" "--execute"] :base (str "http://127.0.0.1:" closed-port) :state "state2")]
  (check (= 7 (:exit r)) "通信失敗の送信は終了7")
  (check (str/includes? (:all r) "送信結果不明") "通信失敗: 送信結果不明と表示")
  (no-token-leak r "通信失敗 send"))
(let [r (run ["send" "--appliance" "f1" "--signal" "s1" "--execute"] :state "state2")]
  (check (= 5 (:exit r)) "結果不明の直後の再実行は待ち時間で拒否")
  (check (empty? (posts)) "結果不明の直後: POST しない"))
;; 待ち時間が過ぎれば送れる
(write-allow! (assoc allow-full :cooldown-seconds 1))
(reset-reqs!)
(check (= 0 (:exit (run ["send" "--appliance" "f1" "--signal" "s1" "--execute"] :state "state3"))) "待ち時間1秒: 1回目は受付")
(check (= 5 (:exit (run ["send" "--appliance" "f1" "--signal" "s1" "--execute"] :state "state3"))) "待ち時間1秒: 直後は拒否")
(Thread/sleep 1300)
(check (= 0 (:exit (run ["send" "--appliance" "f1" "--signal" "s1" "--execute"] :state "state3"))) "待ち時間1秒: 経過後は受付")
(check (= 2 (count (posts))) "待ち時間1秒: POST は合計2回")
;; 送信ロック: 他の送信中は拒否
(reset-reqs!)
(.mkdirs (io/file (path "state4")))
(with-open [ch (java.nio.channels.FileChannel/open (.toPath (io/file (path "state4" "send.lock")))
                                                  (into-array java.nio.file.OpenOption
                                                              [java.nio.file.StandardOpenOption/CREATE
                                                               java.nio.file.StandardOpenOption/WRITE]))
            _lock (.lock ch)]
  (let [r (run ["send" "--appliance" "f1" "--signal" "s1" "--execute"] :state "state4")]
    (check (= 5 (:exit r)) "他の送信中は終了5")
    (check (empty? (posts)) "他の送信中は POST しない")))

;; ---- 4. エアコンの機器対応範囲方式（:ranges, TD-02）----
;; 期待値は spec.md 4 の例から。
(reset-reqs!)
(let [r (run ["list"])]
  (check (str/includes? (:out r) "cool: 温度 18〜32 / 風量 1 2 3 auto / 風向 1 swing") "list: エアコンのモードごとの対応範囲"))
(def allow-ranges
  {:cooldown-seconds 0
   :appliances {"a1" {:label "エアコン"
                      :aircon {:power ["on" "off"]
                               :ranges {"cool" {:temp ["18" "18.5" "26" "32"] :vol ["1" "2" "auto"] :dir ["1" "swing"]}
                                        "dry" {:temp ["-2" "-1" "0" "1" "2"] :vol [""] :dir ["1" "swing"]}}}}}})
(write-allow! allow-ranges)
(reset-reqs!)
(doseq [[args code msg] [[["--mode" "cool" "--temp" "26"] 0 "範囲内の冷房26℃は確認用 終了0"]
                         [["--mode" "cool" "--temp" "33"] 4 "冷房の範囲外33℃は終了4"]
                         [["--mode" "dry" "--temp" "26"] 4 "除湿(相対温度)に26は終了4"]
                         [["--mode" "warm"] 4 "ranges にないモードは終了4"]
                         [["--temp" "1"] 4 "モードなしで一部モードにしかない温度は終了4"]
                         [["--dir" "1"] 0 "モードなしで全モード共通の風向は確認用 終了0"]
                         [["--mode" "dry" "--vol" "auto"] 4 "除湿に風量(空文字のみ)は終了4"]
                         [["--mode" "cool" "--vol" "3"] 4 "一覧にない風量は終了4"]
                         [["--mode" "cool" "--vol" "auto" "--dir" "swing"] 0 "一覧にある風量・風向は確認用 終了0"]
                         [["--power" "off"] 0 "電源off は確認用 終了0"]
                         [["--temp" "abc" "--mode" "cool"] 4 "数でない温度は終了4"]]]
  (let [r (run (into ["aircon" "--appliance" "a1"] args))]
    (check (= code (:exit r)) (str "ranges: " msg))))
(check (str/includes? (:all (run ["aircon" "--appliance" "a1" "--temp" "1"])) "モード") "ranges: モードなしの拒否はモード指定を案内")
(check (empty? @reqs) "ranges: 確認用・拒否では通信しない")
(reset-reqs!)
(run ["aircon" "--appliance" "a1" "--mode" "cool" "--temp" "26.0" "--execute"])
(check (= "operation_mode=cool&temperature=26" (:body (first (posts)))) "ranges: 温度は数で比べ一覧側の文字で送る")

(srv/server-stop! server)
(println (if (zero? @failures) "ALL PASS" (str @failures " FAILED")))
(System/exit (if (zero? @failures) 0 1))
