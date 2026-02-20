# OpenClaw Cron Webhook 運用手順

## 概要

OpenClaw の cron webhook delivery 機能により、cron ジョブの実行結果を外部 HTTP エンドポイントに POST 通知できる。
認証には `OPENCLAW_CRON_WEBHOOK_TOKEN` (Bearer token) を使用する。

## アーキテクチャ

```
SSM Parameter Store
  /lolice/openclaw/OPENCLAW_CRON_WEBHOOK_TOKEN (SecureString)
       ↓ (ExternalSecret, 1h refresh)
K8s Secret: openclaw-credentials
  key: OPENCLAW_CRON_WEBHOOK_TOKEN
       ↓ (Deployment env)
OpenClaw Pod: $OPENCLAW_CRON_WEBHOOK_TOKEN
       ↓ (config: ${OPENCLAW_CRON_WEBHOOK_TOKEN})
openclaw.json → cron.webhookToken
       ↓ (webhook POST)
Authorization: Bearer <token> → 受信側エンドポイント
```

## Secret 管理

### トークン生成

```bash
openssl rand -hex 32
# → 64文字の hex 文字列 (例: a3f8c1d2...9e2b7f40)
```

### SSM Parameter Store 登録

```bash
aws ssm put-parameter \
  --name "/lolice/openclaw/OPENCLAW_CRON_WEBHOOK_TOKEN" \
  --type SecureString \
  --value "$(openssl rand -hex 32)"
```

### ExternalSecret 同期確認

```bash
kubectl get externalsecret openclaw-es -n openclaw -o jsonpath='{.status.conditions[*].message}'
# 期待: "Secret was synced"
```

### トークンローテーション

1. SSM で新しい値を上書き:
   ```bash
   aws ssm put-parameter \
     --name "/lolice/openclaw/OPENCLAW_CRON_WEBHOOK_TOKEN" \
     --type SecureString \
     --value "$(openssl rand -hex 32)" \
     --overwrite
   ```
2. ExternalSecret の refreshInterval (1h) 経過後に自動反映、または手動トリガー:
   ```bash
   kubectl annotate externalsecret openclaw-es -n openclaw \
     force-sync=$(date +%s) --overwrite
   ```
3. Pod 再起動で新しい env を取得:
   ```bash
   kubectl rollout restart deployment/openclaw -n openclaw
   ```
4. webhook 受信側でも同じトークンに更新する

## Cron Webhook 設定

### グローバル設定 (ConfigMap: openclaw.json)

```json
{
  "cron": {
    "enabled": true,
    "webhookToken": "${OPENCLAW_CRON_WEBHOOK_TOKEN}",
    "sessionRetention": "7d"
  }
}
```

| キー | 説明 | デフォルト |
|---|---|---|
| `enabled` | cron スケジューラの有効/無効 | `false` |
| `webhookToken` | Bearer token (env substitution) | なし |
| `sessionRetention` | セッション保持期間 (`"7d"`, `"24h"`, `false` で無効) | `"24h"` |

### ジョブ単位の webhook 設定

```json
{
  "name": "Example webhook job",
  "schedule": { "kind": "cron", "expr": "0 9 * * *", "tz": "Asia/Tokyo" },
  "sessionTarget": "isolated",
  "delivery": {
    "mode": "webhook",
    "to": "https://example.com/webhook-receiver",
    "bestEffort": true
  }
}
```

### delivery.mode の選択

| mode | 用途 | 注意 |
|---|---|---|
| `webhook` | 外部 HTTP(S) エンドポイントへ JSON POST | Discord Webhook 直接は不可 (形式非互換) |
| `announce` | Discord/Telegram チャンネルへ通知 | `channel` + `to` を指定 |

### webhook ペイロード例

```json
{
  "action": "finished",
  "jobId": "job-xxx",
  "status": "ok",
  "summary": "...",
  "sessionId": "...",
  "model": "openai-codex/gpt-5.3-codex",
  "usage": {
    "input_tokens": 1500,
    "output_tokens": 800,
    "total_tokens": 2300
  }
}
```

ヘッダー: `Content-Type: application/json`, `Authorization: Bearer <webhookToken>`

## 失敗時の挙動

- タイムアウト: 10秒
- リトライ: なし (fire-and-forget)
- ジョブ本体への影響: なし (`bestEffort: true` 推奨)
- ログ: `cron: webhook delivery failed` (warn レベル)

## 監視

### Loki クエリ (webhook 失敗検出)

```logql
{namespace="openclaw"} |= "webhook delivery failed"
```

### ExternalSecret 同期状態

```bash
kubectl get externalsecret openclaw-es -n openclaw
```

## ロールバック手順

### cron 機能のみ無効化

`configmap-openclaw.yaml` の `"cron"` セクションを削除し ArgoCD Sync。
config-manager サイドカーが 60 秒以内に変更を検出し、hot-reload される（Pod 再起動不要）。

### 完全ロールバック

1. ExternalSecret, Deployment, ConfigMap の変更を revert
2. ArgoCD Sync
3. SSM Parameter は残しても問題なし（参照されなくなるだけ）

## トラブルシューティング

| 症状 | 原因 | 対処 |
|---|---|---|
| ExternalSecret sync 失敗 | SSM Parameter 未登録 | SSM に登録後、ExternalSecret を再同期 |
| webhook 401 エラー | トークン不一致 | 送信側・受信側のトークンを確認 |
| webhook タイムアウト | 受信側応答遅延 | 受信側の処理を 10 秒以内に完了させる |
| webhook 送信されない | `cron.enabled` が false | ConfigMap を確認 |
| env substitution 失敗 | Deployment に env 未設定 | Deployment の env セクションを確認 |
