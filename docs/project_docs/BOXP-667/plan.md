# BOXP-667: Task Board の全票通知（監督登録に依存しない配送）

設計の正本は Obsidian vault の `Projects/task-board-notify-all-tickets/README.md` です。ここは PR 用の要約です。

## 原因

Task Runner（arch の dispatcher）は、Blocked / Done へ入った全票の event を Hermes へ送っています。欠落は Hermes の受信側 2 箇所で起きていました。

1. 前段 `task_board_webhook_filter.clj`: 監督 runbook（`/opt/data/kohako-task-supervision.md`）に票番号が無い票を、LLM を起動せず `result=silent` で処理済みにする。
2. route `task-board-channel` の prompt 手順 2: 「登録が無い ticket は通知も調整もしない」。

## 変更

| 対象 | 変更前 | 変更後 |
|---|---|---|
| 前段 filter | 未登録票を silent にして捨てる | 全票を通す。未登録票には `supervision=unregistered` を付ける。未登録票の重複配信（処理済み marker あり）だけを従来どおり落とす |
| prompt 手順 2 | 未登録票は通知も調整もしない | 全票が通知対象。登録は「継続操作をしてよいか」だけを決める。未登録票は report-only（担当報告の要約だけ。検収・状態変更・再 queue なし） |
| prompt 手順 5 | 継続条件に runbook 登録を含む | 同じ。report-only の票は対象外と明記 |
| Deployment | route revision | 値を更新し、Pod の通常再作成で反映 |

## 変えないもの

- 送信側（arch の dispatcher）、配送 channel（boxp-notify）、家の受信箱、催促 / fallback、夜間の受信箱保存と呼出し抑制（BOXP-614）、朝の digest、重複抑止と再送。
- 監督 runbook の登録内容と、登録済み票の継続契約。
- 過去に silent で処理済みの event は再通知しない（marker が残っており、再送対象にならない）。

## 検証

- `bb tests/hermes-agent/task-board-webhook-filter-test.bb`（未登録票が通ること、marker を書かないこと、重複だけ落とすこと、prompt の契約文）。
- CI の Hermes notify lab（既存の配送・重複・夜間・回復の回帰）。
- merge 後、監督未登録の実票の event が家の受信箱へ届くことを配送記録で確認する。

## 戻し方

この PR を revert する（filter・prompt・revision が元へ戻り、Pod が再作成される）。
