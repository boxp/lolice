# T-20260217-005: OpenClaw 承認依頼UX改善（最小スコープ）

## 概要

OpenClawの既存機能 `execApprovals` を有効化し、safeBins以外のコマンド実行時にDiscord DMでインタラクティブな承認ボタン（Allow once / Always allow / Deny）を表示する。

## 背景

- `tools.exec.ask` は `#444` で `"on-miss"` に変更済み → safeBins外コマンドで承認要求が発生する
- OpenClawには `channels.discord.execApprovals` という既存機能がある
- 設定追加のみで、Discord上でのボタンベース承認UIが有効化できる

## 変更内容

### 変更ファイル: `argoproj/openclaw/configmap-openclaw.yaml`

`channels.discord` に `execApprovals` セクションを追加:

```json
"execApprovals": {
  "enabled": true,
  "approvers": ["346180680851652618"],
  "target": "dm",
  "cleanupAfterResolve": true
}
```

### 設定の意味

| キー | 値 | 説明 |
|------|-----|------|
| `enabled` | `true` | Discord承認UI転送を有効化 |
| `approvers` | `["346180680851652618"]` | 承認権限を持つDiscordユーザーID（既存のDM allowFromと同一） |
| `target` | `"dm"` | 承認プロンプトをDMに送信 |
| `cleanupAfterResolve` | `true` | 承認・拒否・タイムアウト後にメッセージを自動削除 |

### 動作フロー

1. エージェントがsafeBins外のコマンド実行を要求
2. `ask: "on-miss"` により承認要求イベントが発火
3. `execApprovals.enabled: true` によりDiscord DMに承認ダイアログが送信
4. 3つのボタンが表示: Allow once (緑) / Always allow (青) / Deny (赤)
5. 承認者がボタンを押すと結果がエージェントに返送
6. `cleanupAfterResolve: true` により解決後メッセージは削除

## リスク

- **低**: ConfigMapのみの変更。OpenClawの既存コード機能を利用しており、本体コード変更なし
- 承認者ID設定ミスの場合、承認操作不能 → 既存 `allowFrom` と同一IDのため影響なし
- `target: "dm"` のためチャンネルへの影響なし

## 非スコープ

- OpenClaw本体への新規機能実装
- ボタンラベル・メッセージテキストのカスタマイズ（ハードコードのためコード変更が必要）
