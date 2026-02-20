# T-20260219-014: Cron Webhook / Usage Telemetry 運用組み込み計画

## 概要

OpenClaw v2026.2.17 で追加・改善された cron webhook delivery (`delivery.mode=webhook`) と usage telemetry (実行ごとの model + token usage ログ) を、既存の lolice k8s クラスター運用に組み込むための計画。

## 背景

### v2026.2.17 の関連変更

| コミット | 内容 |
|---|---|
| `bc67af6` | cron webhook POST delivery を announce から分離 (`delivery.mode=webhook`) |
| `ddea5458` | cron 実行ごとの model + token usage ログ記録、usage report スクリプト追加 |
| `dbe2ab6f` | usage telemetry を run-log types とエラーパスに保持 |
| `57c8f623` | webhook/cron セッションの既存 sessionId 再利用 |
| `80c7d04a` | cron run outcome telemetry types のリファクタリング |

### 現行構成

#### cronジョブ

| ジョブ | スケジュール | 種別 |
|---|---|---|
| ECR Token Updater | `0 */10 * * *` (10時間ごと) | K8s CronJob |
| Descheduler | `*/2 * * * *` (2分ごと) | K8s CronJob (Helm) |
| OpenClaw 内蔵 cron | 動的 (Gateway 内蔵スケジューラ) | アプリ内蔵 |

#### テレメトリ

| コンポーネント | 方式 | 送信先 |
|---|---|---|
| OpenClaw OTLP | Push (http/protobuf) | Prometheus `/api/v1/otlp` |
| Grafana Alloy | Log scraping | Loki |
| Prometheus scrape | Pull | Cloudflared metrics |

#### Grafana ダッシュボード

- **OpenClaw Container Monitoring** (uid: `openclaw-container-monitoring`): OOM/CPU/Memory 監視のみ
- アプリケーションレベルのメトリクスダッシュボードは未作成

---

## 1. Cron Webhook Delivery 設計

### 1.1 対象ユースケース

OpenClaw の isolated cron ジョブの実行結果を外部サービスに通知する。

想定される送出先:
1. **カスタム HTTP エンドポイント**: 汎用 JSON を受け取る自前のレシーバーサービス
2. **将来的な外部連携**: Slack Incoming Webhook、監視ツール等

> **注意: Discord Webhook への直接 POST は不可。**
> OpenClaw の cron webhook は `finished` イベントの汎用 JSON を POST するが、Discord Webhook API は `content`/`embeds` 形式を要求する。
> Discord に通知したい場合は以下のいずれかを選択:
> - `delivery.mode=announce` + `delivery.channel=discord` を使用 (推奨)
> - 中間変換サービス (例: Cloudflare Worker) を挟んで Discord 形式に変換
>
> **注意: Gateway 自己参照 (`/hooks/wake`) も現行 NetworkPolicy では不可。**
> OpenClaw の egress ポリシーは RFC1918 アドレスをブロックしているため、クラスタ内部の自己参照は到達しない。
> 自己参照が必要な場合は NetworkPolicy に localhost/自 Pod への egress ルール追加が必要。

### 1.2 推奨構成

```json5
// openclaw.json (ConfigMap) に追加する cron 設定例
{
  "cron": {
    "enabled": true,
    "webhookToken": "${OPENCLAW_CRON_WEBHOOK_TOKEN}",
    "sessionRetention": "7d"
  }
}
```

ジョブごとの webhook 設定例 (カスタム HTTP エンドポイント向け):
```json5
{
  "name": "Daily usage report",
  "schedule": { "kind": "cron", "expr": "0 9 * * *", "tz": "Asia/Tokyo" },
  "sessionTarget": "isolated",
  "wakeMode": "next-heartbeat",
  "payload": {
    "kind": "agentTurn",
    "message": "Generate daily usage report and post to monitoring channel."
  },
  "delivery": {
    "mode": "webhook",
    "to": "https://<custom-receiver-endpoint>",
    "bestEffort": true
  }
}
```

Discord 通知が必要な場合の推奨設定 (announce モード):
```json5
{
  "name": "Daily usage report (Discord)",
  "schedule": { "kind": "cron", "expr": "0 9 * * *", "tz": "Asia/Tokyo" },
  "sessionTarget": "isolated",
  "wakeMode": "next-heartbeat",
  "payload": {
    "kind": "agentTurn",
    "message": "Generate daily usage report."
  },
  "delivery": {
    "mode": "announce",
    "channel": "discord",
    "to": "channel:<channel-id>",
    "bestEffort": true
  }
}
```

### 1.3 ペイロード仕様

webhook POST のペイロードは cron の `finished` イベント:

```json
{
  "action": "finished",
  "jobId": "job-xxx",
  "status": "ok",
  "summary": "...",
  "sessionId": "...",
  "sessionKey": "cron:job-xxx",
  "runAtMs": 1740000000000,
  "durationMs": 12345,
  "nextRunAtMs": 1740086400000,
  "model": "openai-codex/gpt-5.3-codex",
  "provider": "openai-codex",
  "usage": {
    "input_tokens": 1500,
    "output_tokens": 800,
    "total_tokens": 2300
  }
}
```

ヘッダー:
- `Content-Type: application/json`
- `Authorization: Bearer <cron.webhookToken>` (設定時のみ)

### 1.4 失敗時の運用

#### リトライ

現在の実装 (`server-cron.ts:246-264`) では:
- タイムアウト: 10秒 (`CRON_WEBHOOK_TIMEOUT_MS`)
- リトライ: **なし** (fire-and-forget、`.catch()` でログのみ)
- 失敗時はジョブ自体の状態に影響しない

#### 可観測性

| 観測方法 | 詳細 |
|---|---|
| Gateway ログ | `cron: webhook delivery failed` (warn レベル) |
| Grafana Alloy → Loki | Pod ログとして自動収集される |
| Run log (JSONL) | `~/.openclaw/cron/runs/<jobId>.jsonl` に結果記録 |

#### 推奨アラート

Loki LogQL で webhook 失敗を検出:
```logql
{namespace="openclaw"} |= "webhook delivery failed"
```

Grafana Alert Rule として設定し、閾値を超えた場合に通知する。

### 1.5 lolice 変更箇所

#### 1.5.1 ConfigMap 変更 (`argoproj/openclaw/configmap-openclaw.yaml`)

cron セクションを追加:
```json
{
  "cron": {
    "enabled": true,
    "webhookToken": "${OPENCLAW_CRON_WEBHOOK_TOKEN}",
    "sessionRetention": "7d"
  }
}
```

#### 1.5.2 Secret 追加 (3箇所の変更が必要)

##### `OPENCLAW_CRON_WEBHOOK_TOKEN` の値と取得手順

このトークンは **自分で生成するランダム文字列** であり、外部サービスから取得するものではない。
OpenClaw が cron webhook を POST する際に `Authorization: Bearer <token>` ヘッダーとして付与し、
受信側エンドポイントがリクエストの正当性を検証するために使用する共有シークレット。

| 項目 | 内容 |
|---|---|
| **推奨値形式** | 暗号学的にランダムな 32 バイト以上の hex 文字列 (64文字) |
| **生成方法** | `openssl rand -hex 32` |
| **配置先** | AWS SSM Parameter Store (`SecureString`) |
| **注入経路** | SSM → ExternalSecret → K8s Secret → Deployment env |

**生成・登録手順:**

```bash
# 1. トークンを生成
TOKEN=$(openssl rand -hex 32)
echo "$TOKEN"   # 例: a3f8c1...d9e2b7 (64文字の hex 文字列)

# 2. SSM Parameter Store に登録 (arch リポジトリの Terraform で管理する場合はそちらで定義)
aws ssm put-parameter \
  --name "/lolice/openclaw/OPENCLAW_CRON_WEBHOOK_TOKEN" \
  --type SecureString \
  --value "$TOKEN"

# 3. webhook 受信側にも同じトークンを設定し、
#    Authorization: Bearer <token> の検証に使用する
```

> **補足**: Terraform で管理する場合は `aws_ssm_parameter` リソースで `type = "SecureString"` として定義する。
> 値は `random_password` リソースで自動生成するか、初回のみ手動で `aws ssm put-parameter` を実行し、
> Terraform state には `lifecycle { ignore_changes = [value] }` で値変更を無視させる運用が推奨。

以下の 3 箇所に変更が必要:

1. **SSM Parameter Store**: `/lolice/openclaw/OPENCLAW_CRON_WEBHOOK_TOKEN` を SecureString で登録 (arch リポジトリの Terraform で管理)
2. **ExternalSecret** (`argoproj/openclaw/external-secret.yaml`): 新しい secretKey エントリを追加
   ```yaml
   - secretKey: OPENCLAW_CRON_WEBHOOK_TOKEN
     remoteRef:
       conversionStrategy: Default
       decodingStrategy: None
       key: /lolice/openclaw/OPENCLAW_CRON_WEBHOOK_TOKEN
   ```
3. **Deployment** (`argoproj/openclaw/deployment-openclaw.yaml`): env セクションに環境変数を追加
   ```yaml
   - name: OPENCLAW_CRON_WEBHOOK_TOKEN
     valueFrom:
       secretKeyRef:
         name: openclaw-credentials
         key: OPENCLAW_CRON_WEBHOOK_TOKEN
   ```

> **重要**: ExternalSecret に追加するだけでは不十分。Deployment の env セクションに明示的に環境変数として注入する必要がある。現行の Deployment は `envFrom` (全キー自動注入) ではなく個別の `env[].valueFrom.secretKeyRef` 方式を使用している。

#### 1.5.3 NetworkPolicy (`argoproj/openclaw/networkpolicy.yaml`)

webhook の送出先は外部 HTTPS エンドポイント (ポート 443) を想定。
現行の NetworkPolicy では外部 HTTP/HTTPS (ポート 80/443) への egress が RFC1918 除外で許可されているため、**外部 HTTPS エンドポイント向けであれば NetworkPolicy 変更は不要**。

クラスタ内部エンドポイントへの webhook 送出が必要な場合は、対象 Pod/Namespace への egress ルール追加が必要:
```yaml
# 例: クラスタ内の webhook receiver への egress
- to:
    - namespaceSelector:
        matchLabels:
          kubernetes.io/metadata.name: <target-namespace>
      podSelector:
        matchLabels:
          app: <target-app>
  ports:
    - protocol: TCP
      port: <target-port>
```

---

## 2. Usage Telemetry 設計

### 2.1 データソース

v2026.2.17 で追加された telemetry データ:

| データ | 保存先 | 形式 |
|---|---|---|
| 実行ごとの usage | `~/.openclaw/cron/runs/<jobId>.jsonl` | JSONL (ローカルファイル) |
| アプリメトリクス | Prometheus (OTLP push) | 時系列データ |
| アプリログ | Loki (Alloy 経由) | ログストリーム |

### 2.2 収集方針

#### 2.2.1 OTLP メトリクス (既存設定の確認・有効化)

現在 `diagnostics.otel` は設定済みだが、Prometheus 上に `openclaw.*` メトリクスが見つからない。

確認事項:
1. OpenClaw Pod が Prometheus OTLP エンドポイントに正常接続できているか
2. Prometheus の `enableFeatures: ["otlp-write-receiver"]` が有効か
3. NetworkPolicy が正しく通信を許可しているか

対応:
- Pod ログで OTLP push エラーを確認
- `curl` で Pod 内から Prometheus OTLP エンドポイントへの疎通確認

想定されるメトリクス一覧:

> **注意**: OpenClaw は OTLP ドット区切り名 (例: `openclaw.tokens`) でメトリクスを送出する。
> Prometheus の OTLP Write Receiver はドットをアンダースコアに変換する場合があるため、
> 実際のメトリクス名は Phase 1 の疎通確認時に `prometheus/api/v1/label/__name__/values` で確定させる。
> 以下は OpenClaw ドキュメント記載のメトリクス名 (OTLP 形式) と想定される Prometheus 名の対応。

| OTLP メトリクス名 | 想定 Prometheus 名 | 型 | 説明 |
|---|---|---|---|
| `openclaw.tokens` | `openclaw_tokens` | Counter | トークン使用量 (input/output/cache) |
| `openclaw.cost.usd` | `openclaw_cost_usd` | Counter | 推定コスト (USD) |
| `openclaw.run.duration_ms` | `openclaw_run_duration_ms` | Histogram | エージェント実行時間 |
| `openclaw.context.tokens` | `openclaw_context_tokens` | Gauge | コンテキストウィンドウ使用量 |
| `openclaw.webhook.received` | `openclaw_webhook_received` | Counter | Webhook 受信イベント |
| `openclaw.webhook.error` | `openclaw_webhook_error` | Counter | Webhook エラー |
| `openclaw.webhook.duration_ms` | `openclaw_webhook_duration_ms` | Histogram | Webhook 処理時間 |
| `openclaw.message.queued` | `openclaw_message_queued` | Counter | メッセージキュー投入数 |
| `openclaw.message.processed` | `openclaw_message_processed` | Counter | メッセージ処理完了数 |
| `openclaw.message.duration_ms` | `openclaw_message_duration_ms` | Histogram | メッセージ処理時間 |
| `openclaw.queue.depth` | `openclaw_queue_depth` | Gauge | キュー深度 |
| `openclaw.session.state` | `openclaw_session_state` | Gauge | セッション状態 |

#### 2.2.2 Cron Run Log (JSONL)

- パス: `~/.openclaw/cron/runs/<jobId>.jsonl`
- 自動プルーニング: 最大 2MB / 2,000行
- PV (`longhorn`) に永続化済み (既存 OpenClaw Deployment の volume mount)
- レポート生成: `scripts/cron_usage_report.ts` で集計可能

### 2.3 可視化方針

#### 2.3.1 Grafana ダッシュボード新規作成

**OpenClaw Application Metrics** ダッシュボードを新規作成:

パネル構成案:

| セクション | パネル | データソース |
|---|---|---|
| **Token Usage** | Total tokens (over time) | Prometheus |
| | Input vs Output tokens | Prometheus |
| | Estimated cost (USD) | Prometheus |
| **Agent Runs** | Run duration (p50/p95/p99) | Prometheus |
| | Run count (by model) | Prometheus |
| | Context window utilization | Prometheus |
| **Cron Jobs** | Cron execution status | Loki (ログ解析) |
| | Webhook delivery status | Loki (ログ解析) |
| | Run duration per job | Loki (ログ解析) |
| **Messages** | Message throughput | Prometheus |
| | Queue depth | Prometheus |
| | Processing latency | Prometheus |
| **Sessions** | Active sessions | Prometheus |
| | Session states | Prometheus |

#### 2.3.2 Provisioning

ダッシュボード JSON は `argoproj/prometheus-operator/` 配下に ConfigMap として管理し、ArgoCD で同期する。
現行の `openclaw-container-monitoring` ダッシュボード (`argoproj/prometheus-operator/grafana-dashboard-openclaw.yaml`) と同様の方式。

具体的な変更:
1. `argoproj/prometheus-operator/grafana-dashboard-openclaw-app.yaml` を新規作成 (ダッシュボード JSON を ConfigMap として定義)
2. `argoproj/prometheus-operator/kustomization.yaml` の resources に追加
3. `argoproj/prometheus-operator/overlays/grafana.yaml` に volumeMount を追加:
   ```yaml
   - mountPath: /grafana-dashboard-definitions/0/openclaw-app
     name: grafana-dashboard-openclaw-app
     readOnly: true
   ```
4. 対応する volume 定義を追加

### 2.4 保存方針

| データ | 保存期間 | ストレージ |
|---|---|---|
| Prometheus メトリクス | 31日 (既存設定 `retention: 31d`) | Longhorn 60Gi PV |
| Loki ログ | Loki デフォルト設定に準拠 | Longhorn PV |
| Cron Run Log (JSONL) | 自動プルーニング (2MB/2,000行) | OpenClaw PV |
| Usage Report 出力 | オンデマンド生成、保存不要 | — |

---

## 3. 実行計画

### Phase 1: OTLP メトリクス疎通確認と修正

1. OpenClaw Pod ログで OTLP push エラーを確認
2. 必要に応じて ConfigMap / NetworkPolicy を修正
3. Prometheus で `openclaw_*` メトリクスの出現を確認

### Phase 2: Cron Webhook 設定

1. `OPENCLAW_CRON_WEBHOOK_TOKEN` を SSM Parameter Store に登録 (arch リポジトリ Terraform)
2. ExternalSecret マニフェストに secretKey エントリを追加 (`argoproj/openclaw/external-secret.yaml`)
3. Deployment の env セクションに環境変数を追加 (`argoproj/openclaw/deployment-openclaw.yaml`)
4. ConfigMap に `cron` セクションを追加 (`argoproj/openclaw/configmap-openclaw.yaml`)
5. (必要に応じて) 送出先に応じた NetworkPolicy egress ルール追加

### Phase 3: Grafana ダッシュボード作成

1. OpenClaw Application Metrics ダッシュボード JSON 作成
2. Grafana Provisioning マニフェスト追加
3. ArgoCD Application 更新

### Phase 4: アラートルール追加

1. Webhook delivery 失敗の Loki アラートルール
2. Token usage 異常値の Prometheus アラートルール
3. Cron ジョブ連続失敗の Prometheus/Loki アラートルール

---

## 4. リスク

| リスク | 影響度 | 緩和策 |
|---|---|---|
| OTLP メトリクスが Prometheus に到達しない | 中 | Phase 1 で疎通確認し、問題があれば NetworkPolicy / エンドポイントを修正 |
| OTLP メトリクス名が想定と異なる | 中 | Phase 1 で実データを確認し、ダッシュボード設計を実名に合わせて調整 |
| Webhook 送出先がダウンした場合のジョブ影響 | 低 | `bestEffort: true` 設定により、配信失敗がジョブ結果に影響しない |
| Discord Webhook への直接 POST が 400 エラー | 高 | announce モードを使用するか、中間変換サービスを挟む (1.1 節参照) |
| クラスタ内自己参照が NetworkPolicy でブロック | 中 | 内部エンドポイント使用時は egress ルールを明示的に追加 |
| Prometheus ストレージ不足 | 低 | 現行 60Gi / 31日、OpenClaw メトリクスの追加量は微量 |
| Webhook トークン漏洩 | 中 | SSM + ExternalSecrets で管理、Pod 外には露出しない |

---

## 5. 非スコープ

- 実際の cron ジョブの作成・有効化
- 本番環境への webhook エンドポイント設定
- Grafana ダッシュボード JSON の実装 (設計のみ)
- Prometheus アラートルールの実装 (設計のみ)

---

## 6. 次のアクション

1. **OTLP 疎通確認**: OpenClaw Pod から Prometheus OTLP endpoint への接続テスト
2. **SSM パラメータ登録**: `OPENCLAW_CRON_WEBHOOK_TOKEN` を arch リポジトリの Terraform で管理
3. **ConfigMap 更新**: cron セクション追加の PR 作成 (Phase 2)
4. **ダッシュボード実装**: Grafana JSON 作成の PR 作成 (Phase 3)
5. **アラート実装**: アラートルール追加の PR 作成 (Phase 4)
