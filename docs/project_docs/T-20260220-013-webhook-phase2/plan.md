# T-20260220-013: Cron/Webhook Telemetry Phase 2 — Webhook設定導入

## 概要

Phase 1 (T-20260220-012) で OTLP メトリクス疎通を確立した後の Phase 2 として、
`OPENCLAW_CRON_WEBHOOK_TOKEN` の SSM → ExternalSecret → K8s Secret → Deployment env
の注入経路を構築し、ConfigMap に `cron` セクションを追加して webhook delivery を有効化する。

## 前提条件

- Phase 1 (PR #466) がマージ済みであること
- `diagnostics-otel` プラグインが有効化済み

## 変更内容

### 1. ExternalSecret (`argoproj/openclaw/external-secret.yaml`)

SSM Parameter Store `/lolice/openclaw/OPENCLAW_CRON_WEBHOOK_TOKEN` から K8s Secret `openclaw-credentials` へ同期するエントリを追加。

```yaml
- secretKey: OPENCLAW_CRON_WEBHOOK_TOKEN
  remoteRef:
    conversionStrategy: Default
    decodingStrategy: None
    key: /lolice/openclaw/OPENCLAW_CRON_WEBHOOK_TOKEN
    metadataPolicy: None
```

### 2. Deployment (`argoproj/openclaw/deployment-openclaw.yaml`)

`openclaw` コンテナの `env` セクションに環境変数を追加。

```yaml
- name: OPENCLAW_CRON_WEBHOOK_TOKEN
  valueFrom:
    secretKeyRef:
      name: openclaw-credentials
      key: OPENCLAW_CRON_WEBHOOK_TOKEN
```

### 3. ConfigMap (`argoproj/openclaw/configmap-openclaw.yaml`)

`openclaw.json` に `cron` トップレベルセクションを追加。

```json
"cron": {
  "enabled": true,
  "webhookToken": "${OPENCLAW_CRON_WEBHOOK_TOKEN}",
  "sessionRetention": "7d"
}
```

- `webhookToken`: env substitution で実行時に解決される。webhook POST 時に `Authorization: Bearer <token>` として付加。
- `sessionRetention`: cron セッションを 7 日間保持後に自動削除。
- `enabled`: cron スケジューラを有効化。

### 4. SSM Parameter Store（前提作業・別リポジトリ）

arch リポジトリの Terraform、または手動で以下を登録する必要がある:

```bash
TOKEN=$(openssl rand -hex 32)
aws ssm put-parameter \
  --name "/lolice/openclaw/OPENCLAW_CRON_WEBHOOK_TOKEN" \
  --type SecureString \
  --value "$TOKEN"
```

## delivery.mode=webhook 最小有効化手順

### 前提
- SSM に `/lolice/openclaw/OPENCLAW_CRON_WEBHOOK_TOKEN` が登録済み
- この PR がマージ・ArgoCD 同期済み

### 手順

1. SSM Parameter Store にトークンを登録（未登録の場合）
2. ArgoCD で openclaw アプリケーションを Sync
3. ExternalSecret の同期完了を確認: `kubectl get externalsecret openclaw-es -n openclaw`
4. Pod が再起動し、新しい env が注入されていることを確認
5. cron ジョブ設定で `delivery.mode=webhook` を指定:

```json
{
  "delivery": {
    "mode": "webhook",
    "to": "https://<receiver-endpoint>",
    "bestEffort": true
  }
}
```

### 失敗時フォールバック

webhook delivery は fire-and-forget (タイムアウト10秒、リトライなし)。
失敗してもジョブ本体には影響しない。

**announce モードへのフォールバック:**

ジョブ設定で `delivery.mode` を `announce` に変更:

```json
{
  "delivery": {
    "mode": "announce",
    "channel": "discord",
    "to": "channel:<channel-id>",
    "bestEffort": true
  }
}
```

## ロールバック手順

### ConfigMap のみロールバック（cron 無効化）

1. `configmap-openclaw.yaml` から `"cron"` セクションを削除
2. ArgoCD Sync → config-manager サイドカーが 60 秒以内に検出 → hot-reload
3. Pod 再起動不要

### 完全ロールバック（全変更を元に戻す）

1. この PR の全変更を revert する PR を作成
2. ArgoCD Sync
3. SSM Parameter は残しても問題なし（参照されなくなるだけ）

## リスク

| リスク | 影響度 | 緩和策 |
|---|---|---|
| SSM Parameter 未登録で ExternalSecret sync 失敗 | 中 | ExternalSecret の status.conditions を監視。Secret 全体の再作成は行われない（個別キー） |
| webhook 受信側がダウン | 低 | `bestEffort: true` + fire-and-forget。ジョブ本体に影響なし |
| トークン漏洩 | 中 | SSM SecureString + K8s Secret で管理。Pod 外には露出しない |
| NetworkPolicy でブロック | 低 | 外部 HTTPS (443) は既に許可済み。クラスタ内エンドポイントの場合のみ追加設定が必要 |

## 非スコープ

- Grafana ダッシュボード実装（Phase 3）
- アラートルール追加（Phase 4）
- 実際の cron ジョブの定義・有効化
- arch リポジトリ Terraform での SSM パラメータ定義
