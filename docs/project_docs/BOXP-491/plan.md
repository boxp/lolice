# 試行A 計画（BOXP-491）: Hermes cron 3本のterra化と Webhook 定型処理の省力化

> 正本: Obsidian vault `Projects/codex-quota-optimization/trial-a-plan.md`。このPRは「2. Webhook」の実装分。cron の model 変更は Hermes CLI による実行時設定で、このPRには含まれない。

- 承認: Owner が BOXP-490 で「とりあえずA」（event=BOXP-490-20261110T085649Z-…-done）。根拠と推定の限界は research.md / owner-summary.md を引き継ぐ。
- 目的: 今の subscription 枠のまま、通知と判断の質を保ちつつ Codex 消費（特に sol）を減らす。
- 担当: claude-opus（BOXP-491）。結果は trial-a-report.md にまとめる。

## 1. cron 3本（2026-11-10T09:17Z 変更済み）

| job | 名前 | 変更前 | 変更後 | 変えていないもの |
|---|---|---|---|---|
| 74a03bb016d3 | Morning Report cronjob（毎日10:00） | gpt-6.1-sol | **gpt-6.1-terra** | prompt・頻度・deliver=origin・attach_to_session・有効状態 |
| f4fcdaa7a88c | Weekday evening summary（平日18:00） | gpt-6.1-sol | **gpt-6.1-terra** | 同上・skills(quiet-hours, google-workspace, obsidian-task-board) |
| 410f33d091fc | Task Board monitor（180分ごと） | gpt-6.1-sol | **gpt-6.1-terra** + 先頭にガード段落 | 元のprompt全文（byte一致を確認）・頻度・skills・deliver・attach_to_session |

- 手段: Hermes 正規CLI `hermes cron edit <id> --model gpt-6.1-terra --provider openai-codex`。global model・config.yaml・jobs.json の直接編集はしていない。terra は Hermes の openai-codex model 一覧に実在。
- monitor のガード: monitor は実際に継続依頼・再queue（例: 11/10 BOXP-481、11/09 BOXP-471）や Owner への A/B 判断質問（11/09 BOXP-474）を行っていた。terra にこれらを任せないため、元 prompt の前に段落を1つ足した（`evidence/trial-a/monitor-guard-prefix.txt`）。
  - terra が行うこと: 取りこぼし照合、重複防止、担当報告の事実の取り次ぎ、[SILENT] 判定、Webhook停止疑いの1回通知。
  - terra が行わないこと: 継続依頼・再queue・レーン/担当変更・検収記録・判断質問の作成。候補は票を変えずに「【sol判断へ引継ぎ】」で通知し、Owner の返信を Telegram 会話（sol）で扱う。
  - 即時のイベント処理（継続・質問・検収）は従来どおり Webhook の sol 経路が担う。
- 前後の設定: `evidence/trial-a/cron-before-20261110T0917Z.json` / `cron-after-20261110T0918Z.json`（diff は model 3行だけ）。
- 対象外（変えていない）: apply-hold monitor（no-agent）、予定納税 reminder、PR日次整理、サンライズ監視などほかの cron、Hermes の既定モデル、Telegram 会話。

## 2. Webhook（lolice PR で配備）

- 実構成: route `task-board`（sol、署名検証あり、mirror_to_session）。route `alertmanager` はすでに LLM なし。
- 直近3日（11/07〜）の115イベントの処理結果: question 45 / notified 34 / silent 30 / continued 2。silent のうち **11件は runbook に登録のない票**で、LLM はルールどおり marker を書いて黙るだけだった。
- 変更: route に決定論 prefilter script（`task_board_webhook_filter.sh/.clj`）を付ける。
  - LLM なしで処理するのは「runbook に `BOXP-N` も番号 N も出てこない票の run-finished / lane-changed」だけ。marker（result=silent）を helper で追記し、readback できたときだけ [SILENT]。
  - それ以外（登録済みの可能性がある票、morning-digest、形式の違う・未知の payload、helper の失敗、runbook が読めない）は payload をそのまま sol 経路へ渡す。script の失敗もそのまま渡す側に倒す。
  - 人への通知は増やしも減らしもしない（未登録票は今も通知なし）。Owner 判断・質問・critical・Task Board の調整は今の sol 経路のまま。payload の本文に従う処理はない。
- 定型通知のテンプレート化（done の notified など）は今回はしない。done 通知 27件のうち4件で LLM が質問を足していて、決定論では見分けられない。また notified 系は送信 gate の receipt 契約が絡む。

## 3. 測定（24〜48h）

- 配備前の基準: codex primary 41%（2026-11-10 09:16Z）、直近6h・24h とも 0.83pt/h（BOXP-490 調査値）。reset 2026-11-15 02:40:59Z。
- 比べるもの: quota pt/h（1%刻み）、Langfuse の Hermes cron / webhook の token・呼出数・モデル、prefilter のログ `/opt/data/ai-ops/task-board-webhook-filter.jsonl`。
- 分けて見るもの: BOXP-476 など Studio 系の子委譲、ticket の構成、他モデルの自然な増減。
- 品質: 通知の遅延・欠落・二重、日本語の質、判断の取り次ぎ、許可外の操作、担当の再開（monitor の継続依頼が引継ぎに変わった影響）。
- 戻す条件: 判断の取り次ぎ漏れ、通知の欠落・二重、許可外の操作、担当再開の遅れが実害になった場合。対象の設定だけを保存済みの値に戻す（cron は `hermes cron edit <id> --model gpt-6.1-sol --provider openai-codex`、monitor は元 prompt も戻す。Webhook は route の script キーを外す PR）。
