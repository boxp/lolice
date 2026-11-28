#!/usr/bin/env bb
;; BOXP-762: 収集器の git 問い合わせ（実 git の一時 repo 相手。ネットワークは使わない）。
;; 期待値は仕様 1 の 3a・4（path が無いのは既知の「無し」、revision が無い時だけ unknown）から書いた。
(require '[clojure.test :refer [deftest is testing run-tests]]
         '[babashka.fs :as fs]
         '[babashka.process :as p]
         '[clojure.string :as str])

(def root (-> *file* fs/parent fs/parent fs/parent))
(load-file (str (fs/path root "argoproj/prometheus-operator/lolice-dora/collector.bb")))

(def work (str (fs/create-temp-dir {:prefix "lolice-dora-git-"})))
(def repo (str (fs/path work "repo")))

(defn g [& args]
  (-> (apply p/shell {:out :string :err :string :dir repo
                      :extra-env {"GIT_AUTHOR_NAME" "t" "GIT_AUTHOR_EMAIL" "t@example.invalid"
                                  "GIT_COMMITTER_NAME" "t" "GIT_COMMITTER_EMAIL" "t@example.invalid"}}
             "git" args)
      :out str/trim))

(defn commit! [msg]
  (g "add" "-A")
  (g "commit" "-q" "--allow-empty" "-m" msg)
  (g "rev-parse" "HEAD"))

;; A: path 無し → B: argoproj/x を作る → C: argoproj/x を消す
(fs/create-dirs repo)
(g "init" "-q" "-b" "main")
(spit (str (fs/path repo "README.md")) "r\n")
(def A (commit! "init"))
(fs/create-dirs (fs/path repo "argoproj/x"))
(spit (str (fs/path repo "argoproj/x/a.yaml")) "a: 1\n")
(def B (commit! "feat: add x"))
(fs/delete-tree (fs/path repo "argoproj/x"))
(def C (commit! "chore: remove x"))

(def src [{:repo "https://github.com/boxp/lolice" :path "argoproj/x"}])
(defn rec [id t rev] {:uid "u1" :app "x" :id id :t t :revisions [rev] :sources src})

(deftest tree-of-missing-path
  (with-redefs [mirror (str (fs/path repo ".git"))]
    (testing "revision はあるが path が無い → 既知の「無し」（nil ではない）"
      (is (= :absent (tree A "argoproj/x")))
      (is (= :absent (tree C "argoproj/x"))))
    (testing "path がある → tree の sha"
      (is (re-matches #"[0-9a-f]{40}" (str (tree B "argoproj/x")))))
    (testing "revision 自体が無い → nil（unknown）"
      (is (nil? (tree "0123456789abcdef0123456789abcdef01234567" "argoproj/x"))))
    (testing "仕様 1 の例: path 作成・削除の同期は deployment"
      (is (= [:baseline :deployment :deployment]
             (mapv :class (dora/analyze [(rec 1 100 A) (rec 2 200 B) (rec 3 300 C)] git-fns)))))))


;; 仕様 1 の 6・7: 記録を分類する材料はその記録自身（同期時の source）だけから取り、台帳の記録は書き換えない。
(def lolice "https://github.com/boxp/lolice")
(defn app [spec-sources history]
  {:metadata {:uid "u1" :name "a"} :spec {:sources spec-sources} :status {:history history}})

(deftest record-facts-come-from-the-record
  (let [spec-now [{:repoURL lolice :ref "lolice"}
                  {:repoURL "https://charts.example" :helm {:valueFiles ["$lolice/argoproj/a/new.yaml"]}}]
        h1 {:id 1 :deployedAt "2026-11-01T00:00:00Z" :revisions ["A" "1.0.0"]
            :sources [{:repoURL lolice :ref "lolice"}
                      {:repoURL "https://charts.example" :helm {:valueFiles ["$lolice/argoproj/a/old.yaml"]}}]}]
    (testing "Helm ref source の values file は、今の spec ではなく同期記録の source から求める"
      (is (= [{:repo lolice :paths ["argoproj/a/old.yaml"]} {:repo "https://charts.example"}]
             (:sources (first (app-records (app spec-now [h1])))))))
    (testing "同期記録に source が無い → 今の spec で補わず :sources nil（分類は unknown）"
      (is (nil? (:sources (first (app-records (app spec-now [(dissoc h1 :sources)])))))))
    (testing "台帳に入った記録は、後から見えた同じ [uid id] の記録で上書きしない"
      (let [old {:records {["u1" 1] {:id 1 :sources [:old]}}}]
        (is (= {:id 1 :sources [:old]}
               (get-in (merge-records old [[["u1" 1] {:id 1 :sources [:new]}]
                                           [["u1" 2] {:id 2}]]) [:records ["u1" 1]])))
        (is (= {:id 2} (get-in (merge-records old [[["u1" 2] {:id 2}]]) [:records ["u1" 2]])))))
    (testing "source の無い記録は unknown（数えない）"
      (is (= [:baseline :unknown]
             (mapv :class (dora/analyze [{:uid "u1" :app "a" :id 1 :t 1 :revisions ["A"] :sources [{:repo lolice :path "x"}]}
                                         {:uid "u1" :app "a" :id 2 :t 2 :revisions ["B"] :sources nil}]
                                        {:tree (fn [r _] r)})))))))
(deftest changes-without-paths
  (with-redefs [mirror (str (fs/path repo ".git"))]
    (testing "仕様 3.1 の 6: 比べる path が 0 個なら変更は空（git log -- で repo 全体を返さない）"
      (is (= [] (changes A C [])))
      (is (= 2 (count (changes A C [""])))))))

(let [{:keys [fail error]} (try (run-tests) (finally (fs/delete-tree work)))]
  (System/exit (if (zero? (+ fail error)) 0 1)))
