#!/usr/bin/env bb
;; BOXP-762: lolice DORA 指標の判定（仕様: Obsidian Projects/lolice-dora-metrics/spec.md）。
;; 期待値は仕様の例と規則から書いたもので、実装の出力を写していない。
(require '[clojure.test :refer [deftest is testing run-tests]]
         '[babashka.fs :as fs])

(def root (-> *file* fs/parent fs/parent fs/parent))
(load-file (str (fs/path root "argoproj/prometheus-operator/lolice-dora/dora.clj")))
(alias 'dora 'lolice.dora)

(def H 3600)
(def D 86400)
(def lolice "https://github.com/boxp/lolice")
(def src [{:repo lolice :path "argoproj/x"}])

(defn rec [id t rev & {:keys [uid sources] :or {uid "u1" sources src}}]
  {:uid uid :app "x" :id id :t t :revisions (if (vector? rev) rev [rev]) :sources sources})

(defn fake-git
  "trees: {[rev path] tree}; changes: {[from to] [commit...]}; parents: {rev older-rev} の鎖で祖先を決める。"
  [{:keys [trees changes parents] :or {trees {} changes {} parents {}}}]
  {:tree (fn [rev path] (get trees [rev path]))
   :changes (fn [from to _paths] (get changes [from to] []))
   :ancestor? (fn [a b] (loop [c (get parents b)] (cond (nil? c) false (= c a) true :else (recur (get parents c)))))})

(defn classes [events] (mapv :class events))

(deftest classification-table
  (testing "仕様 1 の例の表"
    (let [git (fake-git {:trees {["A" "argoproj/x"] "t1" ["B" "argoproj/x"] "t1"
                                 ["C" "argoproj/x"] "t2"}})
          ev (dora/analyze [(rec 1 100 "A") (rec 2 200 "A") (rec 3 300 "B") (rec 4 400 "C") (rec 5 500 "X")] git)]
      (is (= [:baseline :resync :no-change :deployment :unknown] (classes ev)))))
  (testing "外部 chart は版が変われば配備、同じ版は resync"
    (let [chart [{:repo "https://charts.longhorn.io/" :chart "longhorn"}]
          ev (dora/analyze [(rec 1 100 "1.2.0" :sources chart) (rec 2 200 "1.3.0" :sources chart)
                            (rec 3 300 "1.3.0" :sources chart)] (fake-git {}))]
      (is (= [:baseline :deployment :resync] (classes ev)))))
  (testing "Helm の ref source は渡す values file だけを比べる（repo 全体を見ない）"
    (let [srcs [{:repo lolice :paths ["argoproj/x/values.yaml"]} {:repo "https://charts.example/" :chart "x"}]
          git (fake-git {:trees {["A" "argoproj/x/values.yaml"] "v1" ["B" "argoproj/x/values.yaml"] "v1"
                                 ["C" "argoproj/x/values.yaml"] "v2"}})
          ev (dora/analyze [(rec 1 100 ["A" "1.0"] :sources srcs) (rec 2 200 ["B" "1.0"] :sources srcs)
                            (rec 3 300 ["C" "1.0"] :sources srcs)] git)]
      (is (= [:baseline :no-change :deployment] (classes ev))))
    (let [srcs [{:repo lolice :paths []} {:repo "https://charts.example/" :chart "x"}]]
      (is (= [:baseline :no-change :deployment]
             (classes (dora/analyze [(rec 1 100 ["A" "1.0"] :sources srcs) (rec 2 200 ["B" "1.0"] :sources srcs)
                                     (rec 3 300 ["C" "2.0"] :sources srcs)] (fake-git {})))))))
  (testing "記録の順は id で決まる（入力の順に依らない）"
    (let [git (fake-git {:trees {["A" "argoproj/x"] "t1" ["B" "argoproj/x"] "t2"}})]
      (is (= [:baseline :deployment] (classes (dora/analyze [(rec 2 200 "B") (rec 1 100 "A")] git)))))))

(deftest gaps-and-recreation
  (let [git (fake-git {:trees {["A" "argoproj/x"] "t1" ["B" "argoproj/x"] "t2" ["C" "argoproj/x"] "t3"}})]
    (testing "id が飛んだら欠けの直後が新しい baseline"
      (is (= [:baseline :deployment :baseline] (classes (dora/analyze [(rec 1 100 "A") (rec 2 200 "B") (rec 5 300 "C")] git)))))
    (testing "uid が変わったら新しい baseline"
      (is (= [:baseline :deployment :baseline]
             (classes (dora/analyze [(rec 1 100 "A") (rec 2 200 "B") (rec 1 300 "C" :uid "u2")] git)))))
    (testing "観測起点は最後の baseline"
      (is (= 300 (dora/origin (dora/analyze [(rec 1 100 "A") (rec 2 200 "B") (rec 5 300 "C")] git)))))))

(def t0 (* 1000 D))

(defn chain-git
  "rev 列 [A B C ...] が順に main に積まれ、それぞれ tree が違う lolice の repo。"
  [revs changes]
  (fake-git {:trees (into {} (map-indexed (fn [i r] [[r "argoproj/x"] (str "t" i)]) revs))
             :parents (into {} (map vector (rest revs) revs))
             :changes changes}))

(deftest revert-example
  (testing "仕様 3.2 の例: d1 を d2 が revert、d3 は正常"
    (let [git (chain-git ["A" "B" "C" "D"]
                         {["A" "B"] [{:sha "b1" :subject "feat: x (#10)" :body "" :time (- (+ t0 (* 10 H)) 600)}]
                          ["B" "C"] [{:sha "c1" :subject "Revert \"feat: x (#10)\" (#11)" :body "Reverts boxp/lolice#10" :time (+ t0 (* 10 H) 900)}]
                          ["C" "D"] [{:sha "d1" :subject "feat: y (#12)" :body "" :time (+ t0 (* 11 H))}]})
          ev (dora/analyze [(rec 1 t0 "A") (rec 2 (+ t0 (* 10 H)) "B") (rec 3 (+ t0 (* 10 H) 1800) "C")
                            (rec 4 (+ t0 (* 12 H)) "D")] git)
          m (dora/window-metrics ev {:now (+ t0 D) :window-s (* 7 D) :lead-measurable? true})]
      (is (= 3 (:deployments m)))
      (is (= 1 (:failed m)))
      (is (= (/ 1.0 3) (:change-fail-ratio m)))
      (is (= {:revert 1 :hotfix 0 :rollback 0} (:rework m)))
      (is (= (/ 1.0 3) (:rework-ratio m)))
      (is (= {:n 1 :p50 1800} (:recovery m)))
      (is (= :partial (:coverage m)) "起点 t0 から 1 日しか観測していない")
      (is (= 1.0 (:observed-days m))))))

(deftest hotfix-and-sha-revert
  (let [git (chain-git ["A" "B" "C" "E"]
                       {["A" "B"] [{:sha "1111111aaaa" :subject "feat: x" :body "" :time t0}]
                        ["B" "C"] [{:sha "2222222bbbb" :subject "fix: x" :body "直す\n\nHotfix-For: 1111111" :time (+ t0 H)}]
                        ["C" "E"] [{:sha "3333333cccc" :subject "Revert \"fix: x\"" :body "This reverts commit 2222222bbbb." :time (+ t0 (* 2 H))}]})
        ev (dora/analyze [(rec 1 (- t0 100) "A") (rec 2 (+ t0 100) "B") (rec 3 (+ t0 H 100) "C") (rec 4 (+ t0 (* 3 H)) "E")] git)
        m (dora/window-metrics ev {:now (+ t0 (* 10 D)) :window-s (* 28 D) :lead-measurable? true})]
    (testing "hotfix trailer と sha の revert を関連づける"
      (is (= {:revert 1 :hotfix 1 :rollback 0} (:rework m)))
      (is (= 2 (:failed m)) "B（hotfix された）と C（revert された）")
      (is (= {:n 2 :p50 (- (+ t0 H 100) (+ t0 100))} (:recovery m)) "nearest-rank 中央値は短い方 3600"))))

(deftest rollback
  (testing "古い commit へ戻した同期は rollback。間の配備が失敗"
    (let [git (chain-git ["A" "B" "C"] {["A" "B"] [{:sha "b" :subject "x" :body "" :time t0}]
                                        ["B" "C"] [{:sha "c" :subject "y" :body "" :time t0}]})
          ev (dora/analyze [(rec 1 t0 "A") (rec 2 (+ t0 H) "B") (rec 3 (+ t0 (* 2 H)) "C") (rec 4 (+ t0 (* 3 H)) "A")] git)
          m (dora/window-metrics ev {:now (+ t0 (* 8 D)) :window-s (* 28 D) :lead-measurable? true})]
      (is (= [:baseline :deployment :deployment :deployment] (classes ev)))
      (is (= {:revert 0 :hotfix 0 :rollback 1} (:rework m)))
      (is (= 2 (:failed m)) "B と C が戻された")))
  (testing "同じ tree に戻る新しい commit（replicas 0⇔1）は rollback ではない"
    (let [git (fake-git {:trees {["A" "argoproj/x"] "t0" ["B" "argoproj/x"] "t1" ["C" "argoproj/x"] "t0"}
                         :parents {"B" "A" "C" "B"}
                         :changes {["A" "B"] [{:sha "b" :subject "replicas 0 (#1)" :body "" :time t0}]
                                   ["B" "C"] [{:sha "c" :subject "replicas 1 (#2)" :body "" :time t0}]}})
          ev (dora/analyze [(rec 1 t0 "A") (rec 2 (+ t0 H) "B") (rec 3 (+ t0 (* 2 H)) "C")] git)
          m (dora/window-metrics ev {:now (+ t0 D) :window-s (* 7 D) :lead-measurable? true})]
      (is (= 0 (:failed m)))
      (is (= {:revert 0 :hotfix 0 :rollback 0} (:rework m))))))

(deftest revert-outside-7-days
  (testing "7 日より前の配備の revert は rework だけ数え、失敗と復旧は出さない"
    (let [git (chain-git ["A" "B" "C"]
                         {["A" "B"] [{:sha "b" :subject "feat (#10)" :body "" :time t0}]
                          ["B" "C"] [{:sha "c" :subject "Revert \"feat (#10)\" (#11)" :body "" :time t0}]})
          ev (dora/analyze [(rec 1 t0 "A") (rec 2 (+ t0 H) "B") (rec 3 (+ t0 (* 9 D)) "C")] git)
          m (dora/window-metrics ev {:now (+ t0 (* 10 D)) :window-s (* 28 D) :lead-measurable? true})]
      (is (= {:revert 1 :hotfix 0 :rollback 0} (:rework m)))
      (is (= 0 (:failed m)))
      (is (= {:n 0} (:recovery m)))
      (is (= :no-target (get-in m [:status :recovery-time]))))))

(deftest lead-time
  (testing "変更ごとの deployedAt − 変更の時刻。中央値・90 パーセンタイルは nearest-rank、負は 0"
    (let [deploy (+ t0 (* 10 H))
          git (chain-git ["A" "B"] {["A" "B"] (vec (for [h [1 2 3 4 11]] {:sha (str "s" h) :subject "c" :body "" :time (- deploy (* h H))}))})
          ev (dora/analyze [(rec 1 t0 "A") (rec 2 deploy "B")] git)
          m (dora/window-metrics ev {:now (+ t0 (* 5 D)) :window-s (* 28 D) :lead-measurable? true})]
      (is (= {:n 5 :p50 (* 3 H) :p90 (* 11 H)} (:lead m)))))
  (testing "PR の変更時刻は PR の commit のうち最も早い author 時刻。なければ commit の時刻"
    (is (= 50 (dora/change-time {:subject "feat (#7)" :author-time 100} {7 50})))
    (is (= 100 (dora/change-time {:subject "feat (#7)" :author-time 100} {})))
    (is (= 100 (dora/change-time {:subject "build: automatic update of x" :author-time 100} {7 50}))))
  (testing "負のリードタイムは 0"
    (let [git (chain-git ["A" "B"] {["A" "B"] [{:sha "s" :subject "c" :body "" :time (+ t0 (* 2 H))}]})
          m (dora/window-metrics (dora/analyze [(rec 1 t0 "A") (rec 2 (+ t0 H) "B")] git)
                                 {:now (+ t0 D) :window-s (* 7 D) :lead-measurable? true})]
      (is (= {:n 1 :p50 0 :p90 0} (:lead m))))))

(deftest statuses
  (let [git (chain-git ["A" "B"] {["A" "B"] [{:sha "s" :subject "c" :body "" :time t0}]})]
    (testing "観測起点が期間より前で配備 0 件なら頻度は本当の 0、他は no_target"
      (let [m (dora/window-metrics (dora/analyze [(rec 1 t0 "A")] git)
                                   {:now (+ t0 (* 30 D)) :window-s (* 7 D) :lead-measurable? true})]
        (is (= :full (:coverage m)))
        (is (= 0 (:deployments m)))
        (is (= 0.0 (:per-day m)))
        (is (= {:deployment-frequency :measured :lead-time :no-target :change-fail-rate :no-target
                :recovery-time :no-target :rework-rate :no-target} (:status m)))
        (is (nil? (:change-fail-ratio m)) "対象 0 件の割合は 0 ではなく出さない")))
    (testing "記録がなければ unobserved で値を出さない"
      (let [m (dora/window-metrics [] {:now t0 :window-s (* 7 D) :lead-measurable? true})]
        (is (= :unobserved (:coverage m)))
        (is (nil? (:deployments m)))
        (is (every? #{:unobserved} (vals (:status m))))))
    (testing "期間の途中から観測した配備ありの app は partial"
      (let [m (dora/window-metrics (dora/analyze [(rec 1 t0 "A") (rec 2 (+ t0 H) "B")] git)
                                   {:now (+ t0 (* 2 D)) :window-s (* 7 D) :lead-measurable? true})]
        (is (= :partial (get-in m [:status :deployment-frequency])))
        (is (= :partial (get-in m [:status :lead-time])))
        (is (= 0.5 (:per-day m)))))
    (testing "lolice の source がない app のリードタイムは unmeasured"
      (let [m (dora/window-metrics (dora/analyze [(rec 1 t0 "A") (rec 2 (+ t0 H) "B")] git)
                                   {:now (+ t0 (* 5 D)) :window-s (* 28 D) :lead-measurable? false})]
        (is (= :unmeasured (get-in m [:status :lead-time])))
        (is (nil? (:lead m)))))
    (testing "除外した同期を理由別に数える（観測範囲内）"
      (let [git2 (fake-git {:trees {["A" "argoproj/x"] "t1" ["B" "argoproj/x"] "t1"}})
            m (dora/window-metrics (dora/analyze [(rec 1 t0 "A") (rec 2 (+ t0 H) "A") (rec 3 (+ t0 (* 2 H)) "B") (rec 4 (+ t0 (* 3 H)) "Z")] git2)
                                   {:now (+ t0 D) :window-s (* 7 D) :lead-measurable? true})]
        (is (= {:resync 1 :no-change 1 :unknown 1} (:excluded m)))
        (is (= 0 (:deployments m)))))))

(deftest exposition
  (testing "対象 0 件の割合の行は出さず、状態の行は出す"
    (let [txt (dora/render {:now 1000 :last-success 1000 :ok true :records 1 :hotfix-since 900
                            :apps [{:app "x" :source-kind "lolice_git" :origin 100
                                    :windows {"7d" (dora/window-metrics [] {:now 1000 :window-s (* 7 D) :lead-measurable? true})}}]})]
      (is (re-find #"(?m)^lolice_dora_metric_status\{app=\"x\",window=\"7d\",metric=\"change_fail_rate\",status=\"unobserved\"\} 1$" txt))
      (is (not (re-find #"lolice_dora_change_fail_ratio\{" txt)))
      (is (re-find #"(?m)^lolice_dora_collector_last_run_ok 1$" txt)))))

(let [{:keys [fail error]} (run-tests)]
  (System/exit (if (zero? (+ fail error)) 0 1)))
