# T-20260217-009: Gate1/Gate2 承認UI ConfigMap設定

## 概要

openclaw の Gate1（実装開始承認）/ Gate2（マージ可否承認）Discord UI に対応する
ConfigMap 設定を追加する。

## 変更内容

### `argoproj/openclaw/configmap-openclaw.yaml`

Discord チャンネル設定に `gateApprovals` セクションを追加:

```json
"gateApprovals": {
  "enabled": true,
  "approvers": ["346180680851652618"],
  "target": "dm"
}
```

- `enabled`: Gate承認UIを有効化
- `approvers`: 承認権限を持つDiscordユーザーIDのリスト（owner のみ）
- `target`: 承認通知の送信先（`"dm"` = ダイレクトメッセージ）

## 関連PR

- openclaw 側: `T-20260217-009-gate-approvals` ブランチ
  - Gate承認の型定義、Manager、Gateway RPC、Discord UI、テストを追加

## リスク

- 低: 既存の `execApprovals` 設定に影響なし
- config-manager sidecar によるホットリロードで反映
