#!/usr/bin/env bb
;; BOXP-700: コハコから Nature Remo 公式 Cloud API を直接呼ぶ CLI。
;; 現行仕様: Obsidian vault Projects/kohako-nature-remo/spec.md
;;
;; 終了コード: 0 成功 / 2 使い方 / 3 トークン未設定 / 4 許可外 / 5 送信中・待ち時間内
;;             6 Remo API が拒否（再送しない） / 7 通信失敗・結果不明（再送しない）
;; トークンの値はどこにも出力しない。
(ns nature-remo
  (:require [babashka.http-client :as http]
            [cheshire.core :as json]
            [clojure.edn :as edn]
            [clojure.java.io :as io]
            [clojure.string :as str])
  (:import [java.net URLEncoder]
           [java.nio.channels FileChannel]
           [java.nio.file OpenOption StandardOpenOption]))

(def api-base (or (System/getenv "NATURE_REMO_API_BASE") "https://api.nature.global"))
(def allowlist-path (or (System/getenv "NATURE_REMO_ALLOWLIST") "/etc/nature-remo/allowlist.edn"))
(def state-dir (or (System/getenv "NATURE_REMO_STATE_DIR") "/opt/data/nature-remo"))
(def placeholder "dummy-value-to-be-updated-manually")
(def timeout-ms 10000)

(defn token []
  (let [t (some-> (System/getenv "NATURE_REMO_TOKEN") str/trim)]
    (when-not (or (str/blank? t) (= t placeholder)) t)))

(defn die [code & lines]
  (binding [*out* *err*] (doseq [l lines] (println l)))
  (System/exit code))

(defn load-allowlist []
  (try
    (let [m (edn/read-string (slurp allowlist-path))]
      (if (and (map? m) (or (nil? (:appliances m)) (map? (:appliances m)))) m {}))
    (catch Exception _ {})))

;; ---- HTTP（1 回だけ、自動再試行なし）----
(defn request! [method path & [form]]
  (let [tok (or (token) (die 3 "トークン: 未設定（NATURE_REMO_TOKEN）。本人が SSM の /lolice/hermes-agent/nature-remo-token に保存するまで実機には接続できない。"))
        resp (try
               (http/request (cond-> {:method method :uri (str api-base path) :throw false
                                      :timeout timeout-ms
                                      :client (http/client {:connect-timeout timeout-ms})
                                      :headers {"Authorization" (str "Bearer " tok) "Accept" "application/json"}}
                               form (assoc :body form
                                           :headers {"Authorization" (str "Bearer " tok)
                                                     "Accept" "application/json"
                                                     "Content-Type" "application/x-www-form-urlencoded"})))
               (catch Exception e
                 {:network-error (.getName (class e))}))]
    resp))

(defn rate-note [resp]
  (let [h (:headers resp)]
    (when-let [reset (get h "x-rate-limit-reset")]
      (str "（回数制限のリセット: epoch " reset "）"))))

(defn http-failure [resp what]
  (if (:network-error resp)
    (die 7 (str what "送信結果不明: 通信に失敗した（" (:network-error resp) "）。届いたか分からない。自動再送しない。")
         "実機の状態を本人が見てから、必要なら本人の指示で再実行する。")
    (let [s (:status resp)]
      (die 6 (case (long s)
               401 (str what "Remo API が拒否 HTTP 401: トークンが無効か失効している。再送しない。本人がトークンを確認する。")
               429 (str what "回数制限で拒否 HTTP 429（30 回 / 5 分）。再送しない。" (rate-note resp))
               (str what "Remo API が拒否 HTTP " s "。再送しない。"))))))

(defn get-json [path]
  (let [resp (request! :get path)]
    (if (and (:status resp) (<= 200 (:status resp) 299))
      (json/parse-string (:body resp))
      (http-failure resp ""))))

;; ---- 読み取り ----
(defn allowed-note [allow id]
  (when-let [a (get-in allow [:appliances id])] (str "  ← 許可済み: " (:label a))))

(defn cmd-list [_]
  (let [allow (load-allowlist)]
    (doseq [a (get-json "/1/appliances")]
      (println (str "[" (get a "type") "] " (get a "nickname") " (id=" (get a "id") ")"
                    " device=" (get-in a ["device" "name"]) (allowed-note allow (get a "id"))))
      (when-let [s (get a "settings")]
        (println (str "  Remoが最後に送った設定: " (get s "temp") (when (seq (str (get s "temp"))) "℃")
                      " mode=" (get s "mode") " vol=" (get s "vol") " dir=" (get s "dir")
                      " button=" (pr-str (get s "button")) " 更新=" (get s "updated_at")
                      "（実機の現在状態ではない）")))
      (when-let [modes (some-> (get-in a ["aircon" "range" "modes"]) keys seq)]
        (println (str "  対応モード: " (str/join " " (sort modes)))))
      (doseq [sig (get a "signals")]
        (println (str "  - " (get sig "name") " (signal=" (get sig "id") ")"))))))

(def sensor-names {"te" ["室温" "℃"] "hu" ["湿度" "%"] "il" ["照度" ""] "mo" ["人感" ""]})

(defn cmd-sensors [_]
  (doseq [d (get-json "/1/devices")]
    (println (str (get d "name") " (id=" (get d "id") "):"))
    (doseq [[k [label unit]] sensor-names
            :let [ev (get-in d ["newest_events" k])] :when ev]
      (println (str "  " label " " (get ev "val") unit " (" (get ev "created_at") " 測定)")))))

(defn cmd-status [_]
  (let [allow (load-allowlist)]
    (println (str "トークン: " (if (token) "設定済み" "未設定")))
    (println (str "許可済み家電: " (count (:appliances allow)) " 件"))
    (doseq [[id a] (:appliances allow)]
      (println (str "  " (:label a) " (id=" id ")")))
    (when-not (token) (System/exit 3))))

;; ---- 送信 ----
(defn parse-opts [args]
  (loop [m {} [a & more :as xs] args]
    (cond
      (empty? xs) m
      (= a "--execute") (recur (assoc m :execute true) more)
      (and (str/starts-with? a "--") (seq more)) (recur (assoc m (keyword (subs a 2)) (first more)) (rest more))
      :else (die 2 (str "使い方: 不明な引数 " a)))))

(defn lock-and-cooldown! [appliance-id label f]
  (.mkdirs (io/file state-dir))
  (with-open [ch (FileChannel/open (.toPath (io/file state-dir "send.lock"))
                                   (into-array OpenOption [StandardOpenOption/CREATE StandardOpenOption/WRITE]))]
    (let [lock (try (.tryLock ch) (catch Exception _ nil))]
      (when-not lock (die 5 "拒否: 別の送信が進行中。重ねて送らない。"))
      ;; ロックはチャネルを閉じると（プロセス終了時も）解放される
      (let [allow (load-allowlist)
              cooldown (long (or (:cooldown-seconds allow) 60))
              f-state (io/file state-dir (str "last-send-" (str/replace appliance-id #"[^A-Za-z0-9_-]" "_") ".edn"))
              last-at (try (:at (edn/read-string (slurp f-state))) (catch Exception _ nil))
              now (quot (System/currentTimeMillis) 1000)]
          (when (and last-at (< (- now last-at) cooldown))
            (die 5 (str "拒否: " label " は前回の送信から待ち時間内（連打・重複防止）。あと" (- cooldown (- now last-at)) "秒。")))
          ;; 結果不明でも次の待ち時間を効かせるため、POST の前に記録する
          (spit f-state (pr-str {:at now}))
          (f)))))

(def toggle-note "トグル信号: 今の状態によって逆になる。送る前に本人が今の状態を確認すること。")
(def accepted-note "送信受付: Remo API が受け付けた。家電が実際に動いたかは未確認（本人の目視や sensors の推移で確認する）。")

(defn cmd-send [args]
  (let [{:keys [appliance signal execute]} (parse-opts args)
        _ (when (or (str/blank? appliance) (str/blank? signal))
            (die 2 "使い方: send --appliance <家電id> --signal <信号id> [--execute]"))
        allow (load-allowlist)
        a (get-in allow [:appliances appliance])
        s (get-in a [:signals signal])]
    (when-not a (die 4 (str "拒否: 家電 " appliance " は許可リストにない（送信しない）。")))
    (when-not s (die 4 (str "拒否: 信号 " signal " は " (:label a) " の許可リストにない（送信しない）。")))
    (if-not execute
      (do (println (str "未送信（確認用）: " (:label a) " に「" (:label s) "」を送る予定。"))
          (when (:toggle s) (println toggle-note))
          (println "本人の確認後に --execute を付けて 1 回だけ実行する。"))
      (do
        (when-not (token) (die 3 "トークン: 未設定。実機には送れない。"))
        (lock-and-cooldown! appliance (:label a)
          (fn []
            (let [resp (request! :post (str "/1/signals/" (URLEncoder/encode signal "UTF-8") "/send"))]
              (if (and (:status resp) (<= 200 (:status resp) 299))
                (do (println (str accepted-note " 対象: " (:label a) "「" (:label s) "」"))
                    (when (:toggle s) (println toggle-note)))
                (http-failure resp (str (:label a) "「" (:label s) "」: "))))))))))

(def mode-names {"cool" "冷房" "warm" "暖房" "dry" "除湿" "blow" "送風" "auto" "自動"})

(defn form-encode [m]
  (str/join "&" (for [[k v] m] (str k "=" (URLEncoder/encode (str v) "UTF-8")))))

(defn cmd-aircon [args]
  (let [{:keys [appliance mode temp vol dir power execute]} (parse-opts args)
        _ (when (or (str/blank? appliance) (every? nil? [mode temp vol dir power]))
            (die 2 "使い方: aircon --appliance <家電id> [--mode M] [--temp T] [--vol V] [--dir D] [--power on|off] [--execute]"))
        allow (load-allowlist)
        a (get-in allow [:appliances appliance])
        ac (:aircon a)
        reject #(die 4 (str "拒否: " % "（送信しない）。"))]
    (when-not ac (reject (str "家電 " appliance " はエアコン操作の許可リストにない")))
    (when (and mode (not (some #{mode} (:modes ac)))) (reject (str "モード " mode " は許可外")))
    (when temp
      (let [[lo hi] (:temp ac)
            t (try (Double/parseDouble temp) (catch Exception _ nil))]
        (when-not (and t lo hi (<= lo t hi)) (reject (str "温度 " temp " は許可範囲 " lo "〜" hi " の外")))))
    (when (and power (not (some #{power} (:power ac)))) (reject (str "電源 " power " は許可外")))
    (when (and vol (not (:vol ac))) (reject "風量の変更は許可外"))
    (when (and dir (not (:dir ac))) (reject "風向の変更は許可外"))
    (let [form (cond-> (array-map)
                 mode (assoc "operation_mode" mode)
                 temp (assoc "temperature" temp)
                 vol (assoc "air_volume" vol)
                 dir (assoc "air_direction" dir)
                 power (assoc "button" (if (= power "off") "power-off" "")))
          plan (str (:label a) " を"
                    (str/join "" (remove nil? [(when power (str " 電源" power))
                                               (when mode (str " " (get mode-names mode mode)))
                                               (when temp (str " " temp "℃"))
                                               (when vol (str " 風量" vol))
                                               (when dir (str " 風向" dir))]))
                    " にする")]
      (if-not execute
        (do (println (str "未送信（確認用）: " plan "予定。"))
            (println "本人の確認後に --execute を付けて 1 回だけ実行する。"))
        (do
          (when-not (token) (die 3 "トークン: 未設定。実機には送れない。"))
          (lock-and-cooldown! appliance (:label a)
            (fn []
              (let [resp (request! :post (str "/1/appliances/" (URLEncoder/encode appliance "UTF-8") "/aircon_settings")
                                   (form-encode form))]
                (if (and (:status resp) (<= 200 (:status resp) 299))
                  (let [s (try (json/parse-string (:body resp)) (catch Exception _ nil))]
                    (println (str accepted-note " 対象: " plan))
                    (when (map? s)
                      (println (str "Remo が記録した設定: " (get s "temp") "℃ mode=" (get s "mode")
                                    " vol=" (get s "vol") " button=" (pr-str (get s "button"))
                                    "（実機の状態ではない）"))))
                  (http-failure resp (str plan ": ")))))))))))

(def usage
  "使い方: nature_remo.sh <status|list|sensors|send|aircon> ...
  status                     トークンの有無と許可済み家電（通信なし）
  list                       家電と信号の一覧（GET 1 回）
  sensors                    Remo 本体の室温・湿度など（GET 1 回）
  send --appliance ID --signal ID [--execute]
  aircon --appliance ID [--mode M] [--temp T] [--vol V] [--dir D] [--power on|off] [--execute]
--execute なしは確認用の表示だけで送信しない。")

(let [[cmd & args] *command-line-args*]
  (case cmd
    "status" (cmd-status args)
    "list" (cmd-list args)
    "sensors" (cmd-sensors args)
    "send" (cmd-send args)
    "aircon" (cmd-aircon args)
    (die 2 usage)))
