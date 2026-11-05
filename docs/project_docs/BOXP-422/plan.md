# BOXP-422: G2 依頼元の記録と 60 秒同期での完了結果表示

正本: Obsidian `Projects/kohako-even-g2/g2-request-result-tracking`（BOXP-418 調査）。

## 目的

G2 から作成・依頼した票の完了結果を、G2 が既存の 60 秒 sync で読み取り専用に取得して会話に表示する。
Telegram の通知・監督・会話・保留操作・承認は変更しない。相互同報しない。

## BOXP-418 推奨案からの変更

| 項目 | 418 案 | 本実装 | 理由 |
|---|---|---|---|
| 受付元の記録 | helper `--origin` と plugin gate で frontmatter へ | Hermes の session 記録（BFF だけが作れる `kohako-g2-*` の tool_calls と tool 結果）を公式 API で読み導出 | plugin・helper の変更は Hermes 再起動か PVC 更新を要する。本票では追加の Hermes 再起動は別確認。モデルの自由文には依存しない点は同じ |
| 要約 | 最終応答の冒頭 | 本文が `Owner向け要約:` で始まる Notes、最終応答の冒頭（見出しまで）、最新の担当 Notes の順 | 実データで最終応答が無関係な文になる例がある（BOXP-418） |

## 構成

```
G2 --60秒 sync--> BFF GET /kohako/api/v1/results（Access JWT 必須）
                   |- Hermes GET /api/sessions, /api/sessions/{id}/messages（読取のみ）
                   |     -> kohako-g2-*（cand 除く）の create / request-codex / update --lane Ready の成功記録 = 受付元
                   `- dashboard :8090 GET /api/kohako/results?ids=...（読取のみ）
                         -> Done 票の最新 done run の題名・要約・未確認事項
```

## 主な性質

- サーバーは未読状態を持たない。BFF 再起動後は session 記録から再計算する。
- 表示済みは G2 の localStorage で結果 ID（`<票>:<run>`）により管理する。
- 録音・確認中・送信中・返答の 2 頁目以降を読んでいる間は表示を保留し、次の sync で表示する。
- 結果は Hermes に渡さない。`lastExchange`、instructions、候補、承認に入らない。
- 対象は Done のみ。Blocked は今回対象外。

## 反映

1. lolice: dashboard（`codex-workspace` の計画 rollout が 1 回発生）
2. even-g2-lab: BFF と G2 クライアント（image updater で `even-g2-main` が rollout）

Hermes の再起動、新しい権限・secret、課金はない。

## 残る確認

- 実機での表示は Owner の操作が必要。
- Hermes の context 圧縮後の session では、圧縮前の tool 記録を読めない可能性がある。
