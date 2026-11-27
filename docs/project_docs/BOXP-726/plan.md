# BOXP-726: golyat-1 の接続記録の欠測を直し、他ノードの収集状況を点検する

## 目的
BOXP-721 の通信監査（観測専用）で、golyat-1 には `/var/log/kern.log` が無く、Calico Log の接続開始記録が欠けていた。golyat-1 だけを優先して最小の是正を入れ、他ノードは読取専用で棚卸しする。

## 方式
- Alloy の ConfigMap に `loki.source.journal "agent_egress_conn"` を追加する。`path` はノード名の対応表で golyat-1 のときだけ `/var/log/journal`、他は journal の無い `/etc/alloy`（何も読まない、kern.log と二重にしない）。
- `_TRANSPORT=kernel` の MESSAGE だけを既存の conn-filter（`calico-packet:` を含む行のみ）へ流す。label は job と node だけ。`max_age=12h` で golyat-1 にいた間の欠測分も埋める。
- DaemonSet は変えない（全 Alloy Pod の再起動を避け、他ノードを変えない）。
- 遮断・本番アラートは入れない。保持（Loki 全体の 90 日）とデータ最小化は変えない。

## 採らなかった方式
- `path` 未指定: Alloy コンテナの machine-id がホストと違い、ホストの journal を読めない（docker で再現）。
- 全ノードで `/var/log/journal`: journal の無いノードで設定読込全体が失敗する（BOXP-721 の CI と docker で再現）。
- ノードへの rsyslog 導入（arch の Ansible）: ノード設定変更で範囲が広い。

## 検証
- `tests/agent-egress-audit/test_alloy_pipeline.sh`（本物の Alloy v1.7.0 と systemd-journal-remote で作った journal）: 実装前 FAIL → 実装後 20/20 PASS。
- 反映後: golyat-1 の Alloy で journal component が healthy、`loki_source_journal_target_lines_total` 増加、Loki に `node="golyat-1"` の calico-packet 行。unhealthy なら直ちに revert（Pod 再起動で CrashLoop するため）。

## Lean
対象外: 設定のパイプライン（入力→出力の対応）で、順序に依存する状態機械の規則が無いため。期待値は仕様の例から取り、実 Alloy で直接確かめる。
