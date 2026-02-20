# T-20260220-027: メトリクス収集状況の検証結果

## 検証日時

2026-02-20

## 概要

PR #471 で追加した Grafana Application Metrics ダッシュボードが参照する
`openclaw_*` メトリクスが Prometheus で収集されているかを検証した。

**結論: 全 18 メトリクスが Prometheus に存在しない（0/18）。**

## 検証手順と結果

### 1. ダッシュボードで使用しているメトリクス一覧

`grafana-dashboard-openclaw-app.yaml` から抽出した全メトリクス:

| # | Prometheus メトリクス名 | 種類 | ダッシュボードパネル |
|---|----------------------|------|-------------------|
| 1 | `openclaw_tokens_total` | Counter | Total Tokens, Token Usage by Type/Model |
| 2 | `openclaw_cost_usd_total` | Counter | Estimated Cost, Cost Over Time |
| 3 | `openclaw_webhook_received_total` | Counter | Webhook Success Rate, Webhook Received vs Errors, Webhook by Type |
| 4 | `openclaw_webhook_error_total` | Counter | Webhook Success Rate, Webhook Received vs Errors |
| 5 | `openclaw_run_attempt_total` | Counter | Run Attempts |
| 6 | `openclaw_message_queued_total` | Counter | Messages Queued vs Processed |
| 7 | `openclaw_message_processed_total` | Counter | Messages Queued vs Processed, Processing Outcome |
| 8 | `openclaw_session_state_total` | Counter | Session State Transitions |
| 9 | `openclaw_session_stuck_total` | Counter | Stuck Sessions |
| 10 | `openclaw_queue_lane_enqueue_total` | Counter | Lane Enqueue / Dequeue |
| 11 | `openclaw_queue_lane_dequeue_total` | Counter | Lane Enqueue / Dequeue |
| 12 | `openclaw_run_duration_ms_bucket` | Histogram | Run Duration Percentiles, Run Duration by Model |
| 13 | `openclaw_webhook_duration_ms_bucket` | Histogram | Webhook Processing Duration |
| 14 | `openclaw_message_duration_ms_bucket` | Histogram | Message Processing Duration |
| 15 | `openclaw_queue_depth_bucket` | Histogram | Queue Depth |
| 16 | `openclaw_queue_wait_ms_bucket` | Histogram | Queue Wait Time |
| 17 | `openclaw_context_tokens_bucket` | Histogram | Context Window Usage |
| 18 | `openclaw_session_stuck_age_ms_bucket` | Histogram | Stuck Session Age |

### 2. Prometheus 検索結果

```promql
# メトリクス名検索 — 結果: 0 件
{__name__=~"openclaw_.*"}

# service_name ラベル検索 — "openclaw" なし
# 存在する値: external-secrets-webhook, longhorn-admission-webhook のみ
```

**全 18 メトリクスが Prometheus に存在しない。**

### 3. 根本原因

#### 直接原因: `@opentelemetry/api` モジュール欠損

Loki ログで以下のエラーを確認:

```
2026-02-20T14:33:27.038Z [gateway] [plugins] diagnostics-otel failed to load
  from /app/extensions/diagnostics-otel/index.ts:
  Error: Cannot find module '@opentelemetry/api'
```

このエラーは複数の Pod 世代で継続的に発生:

| Pod | 発生時刻 (UTC) |
|-----|--------------|
| `openclaw-7dd4f57495-fxt8j` | 2026-02-20T11:10:19 |
| `openclaw-5869587775-z8n2v` | 2026-02-20T13:35:06 |
| `openclaw-5579969465-658k4` | 2026-02-20T13:42:28 |
| `openclaw-559ddcc574-2cfwx` | 2026-02-20T14:33:27 |

#### 因果関係

```
@opentelemetry/api が Docker イメージに未インストール
  → diagnostics-otel プラグインの import が失敗
    → プラグインの register() が呼ばれない
      → OTEL NodeSDK が初期化されない
        → PeriodicExportingMetricReader が起動しない
          → Prometheus OTLP endpoint へのメトリクスプッシュが発生しない
            → 全 openclaw_* メトリクスが欠損
```

#### 設定面の検証（問題なし）

| 項目 | 設定 | 状態 |
|------|------|------|
| `diagnostics-otel` プラグイン有効化 | `plugins.entries.diagnostics-otel.enabled: true` | OK (PR #466 で修正済み) |
| OTLP endpoint | `http://prometheus-k8s.monitoring.svc:9090/api/v1/otlp` | OK |
| OTLP protocol | `http/protobuf` | OK |
| metrics 有効化 | `diagnostics.otel.metrics: true` | OK |
| Prometheus `otlp-write-receiver` | `enableFeatures: ["otlp-write-receiver"]` | OK |
| NetworkPolicy (openclaw → prometheus:9090) | egress 許可 | OK |
| NetworkPolicy (prometheus ← openclaw) | ingress 許可 | OK |

## 対応案

### 必須: OpenClaw Docker イメージに OTel 依存を追加

**対象リポジトリ: `boxp/arch` (docker/openclaw/Dockerfile)**

現在の base image `ghcr.io/openclaw/openclaw:2026.2.19` に
`@opentelemetry/api` が含まれていない。以下のいずれかで対応:

#### Option A: boxp/arch Dockerfile で追加インストール（推奨）

```dockerfile
# After FROM ghcr.io/openclaw/openclaw:...
USER root
RUN cd /app && npm install @opentelemetry/api \
    @opentelemetry/sdk-node \
    @opentelemetry/sdk-metrics \
    @opentelemetry/exporter-metrics-otlp-proto
USER node
```

- メリット: 即座に対応可能、upstream に依存しない
- デメリット: バージョン管理が手動、upstream 更新で不整合の可能性

#### Option B: upstream openclaw イメージの修正

`openclaw/openclaw` リポジトリの `package.json` に OTel 依存を追加し、
新バージョンをリリースしてもらう。

- メリット: 根本的な修正
- デメリット: upstream への PR + リリース待ちが必要

### 推奨アクション

1. **短期 (Option A)**: `boxp/arch` で Dockerfile に OTel パッケージを追加
2. **検証**: 新イメージデプロイ後、Prometheus で `openclaw_*` メトリクス出現を確認
3. **中期 (Option B)**: upstream に Issue/PR を作成

## 次のステップ

- [ ] `boxp/arch` リポジトリで OTel 依存追加の PR を作成
- [ ] 新イメージビルド → ArgoCD Image Updater による自動デプロイ
- [ ] メトリクス出現確認 (`count({__name__=~"openclaw_.*"})`)
- [ ] ダッシュボードでデータ表示を確認
