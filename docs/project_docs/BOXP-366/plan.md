# BOXP-366: G2通知同報と専用共有decision制御の撤去

## 背景

BOXP-348 で Task Board 通知を Telegram と G2 メッセージ窓へ同報し、両入口の返答を Hermes plugin
`kohako-g2-context` の decision claim（BOXP-348/362）で一元化した。しかし Owner の実機では G2 に通知が届かず、
共有 decision gate は新しい独立指示（BOXP-360 の継続操作）を対応済み通知（BOXP-358）へ誤 bind して拒否した。
Owner 判断で本機能を撤去する。

## 撤去境界

| 対象 | 撤去 | 維持 |
| --- | --- | --- |
| Hermes plugin `kohako-g2-context` (lolice) | 通知検出 `_detect`、decision claim/settle/attestation、`post_tool_call` / `transform_llm_output` / `post_llm_call` hook、`attest-legacy-claim` CLI | `pre_llm_call` の G2 文脈参照（Owner Telegram DM のみ）、`pre_tool_call` の G2 tool gate（読み取り・Board 非削除変更は自由、他は公式承認、候補/他 API session は tool なし） |
| even-g2-lab BFF env (lolice) | `KOHAKO_NOTIFICATION_CLAIM` | 認証、Hermes/STT/Board 接続、共有文脈 `KOHAKO_SHARED_CONTEXT` |
| BFF (even-g2-lab) | 通知 inbox の poll、`/kohako/api/v1/notifications(/replies)`、通知設定 | 通常会話、共有文脈（通知配信行を除外する filter は維持）、承認、診断、STT |
| G2 アプリ (even-g2-lab) | 通知の取得・表示・返答 | 通常会話、メッセージ窓/portrait/scroll、音声、Task Board 入口、承認 |
| Telegram 通知 / Task Board 実行 lock | なし | すべて維持 |

## 旧データの扱い

- Hermes PVC 上の `/opt/data/kohako-decisions`（claims / attestations / turns）は削除しない。plugin は読み書きしなくなるため、
  旧 pending / unknown claim が新規承認や再実行に変換されることはない。監査履歴として残す。
- G2 端末に保存済みの通知状態は読み込み時に無視する。

## 回帰試験

- 過去の Task Board 通知が混在する Telegram 会話で、新しい独立指示の副作用 tool が plugin に止められない。
- G2 の旧 `[kohako-decision] reply` marker 付き入力も通常の G2 gate（承認/自由/拒否）だけで判定される。
- 既存 claim ファイル（claimed/unknown）が残っていても block されず、ファイルは不変。
- G2 文脈参照・G2 tool gate・候補 session の tool 拒否の既存試験は維持。
- BFF の通知 API は 404、旧保存状態の読み込みが壊れない。

## 配備

1. even-g2-lab PR merge → image 更新（BFF/G2）。
2. lolice PR merge → even-g2-lab の env 撤去は BFF pod 再作成のみ。
   Hermes plugin の configMap hash が変わるため **Hermes pod 再作成が 1 回必要**。既存の再起動予算は流用せず、Owner に一点相談してから merge する。
3. readback: ArgoCD Synced、plugin version 1.4.0 / hook 2 件、BFF image と env、通知 API 404、Telegram 通知継続。
