# T-20260220-027: メトリクス収集状況の検証結果

## 検証日時

2026-02-20

## 概要

PR #471 で追加した Grafana Application Metrics ダッシュボードが参照する
`openclaw_*` メトリクスが Prometheus で収集されているかを検証した。

**結論: ダッシュボードが参照する全 18 メトリクス定義（Counter 11 + Histogram 7）が Prometheus に存在しない（0/18）。**

> 注: Histogram 系は Prometheus 上で `_bucket`/`_sum`/`_count` の3 series に展開されるため、
> 実際の time series 数は最大 32（Counter 11 + Histogram 7×3）となる。
> ここでの「18」はダッシュボード PromQL で参照されるベースメトリクス名の数。

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

Loki ログで以下のエラーを確認（LogQL: `{container="openclaw"} |~ "(?i)(otel|telemetry|Cannot find module)"`）:

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

#### 他要因の切り分け

| 確認項目 | 結果 | 根拠 |
|---------|------|------|
| プラグインロード | 失敗 | Loki: `Cannot find module '@opentelemetry/api'` が全 Pod で発生 |
| exporter 初期化ログ | 存在しない | LogQL `{container="openclaw"} \|~ "(?i)(MetricExporter\|otlp.*export\|metric.*reader)"` → 0件 |
| OTLP HTTP POST | 発生していない | Prometheus 側に `openclaw` 由来の series が 0 件 |
| 他の OTel ログ | 存在しない | LogQL `{container="openclaw"} \|~ "(?i)opentelemetry"` → プラグインロード失敗のみ |

プラグインロードの時点で `@opentelemetry/api` の import に失敗しているため、
SDK 初期化・exporter 起動・メトリクス送信のいずれも実行されていない。

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
| OTLP endpoint (base) | `http://prometheus-k8s.monitoring.svc:9090/api/v1/otlp` | OK |
| OTLP metrics ingest URL | 上記 base + `/v1/metrics` (SDK が自動付与) | OK (設定値は base のみで正しい) |
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

## 再検証 (T-20260220-028): Grafana MCP 経由の確認

### 検証日時

2026-02-20 18:30 UTC

### Grafana MCP 利用可否

Grafana MCP (`mcp__grafana__*`) ツール群の動作確認を実施。

| ツール | 結果 | 備考 |
|--------|------|------|
| `list_datasources` | OK | 3件取得: prometheus, Loki, alertmanager |
| `search_dashboards` (query: "openclaw") | OK | 2件: OpenClaw Application Metrics, OpenClaw Container Monitoring |
| `list_prometheus_metric_names` (regex: `openclaw_.*`) | OK (0件) | メトリクス未収集を再確認 |
| `list_loki_label_values` (container) | OK | `openclaw` コンテナ確認 |
| `query_loki_logs` | OK | ログ取得成功 |

**結論: Grafana MCP は全ツール正常動作中。**

### データソース一覧

| UID | 名前 | タイプ | デフォルト |
|-----|------|--------|-----------|
| `P1809F7CD0C75ACF3` | prometheus | prometheus | Yes |
| `P8E80F9AEF21F6940` | Loki | loki | No |
| `ee8z216blo1s0b` | alertmanager | alertmanager | No |

### Prometheus メトリクス再確認

```
list_prometheus_metric_names(regex="openclaw_.*") → []
list_prometheus_metric_names(regex="openclaw")    → []
```

**全 18 ベースメトリクス定義が依然として Prometheus に存在しない（0/18、前回検証と同一）。**

### Loki ログ再確認（最新 Pod 世代）

OTel プラグイン読み込みエラーは **現行 Pod でも継続発生中**:

| Pod | 発生時刻 (UTC) | エラー |
|-----|--------------|--------|
| `openclaw-559ddcc574-2cfwx` | 2026-02-20T14:33:27 | `Cannot find module '@opentelemetry/api'` |
| `openclaw-5579969465-658k4` | 2026-02-20T13:42:28 | 同上 |
| `openclaw-5869587775-z8n2v` | 2026-02-20T13:35:06 | 同上 |
| `openclaw-7dd4f57495-fxt8j` | 2026-02-20T11:10:19 | 同上 |

追加で検出された問題:
- **Discord Gateway セキュリティエラー**: `SECURITY ERROR: Gateway URL "ws://192.178.252.121:18789" uses plaintext ws:// to a non-loopback address.` が約5分間隔で発生中（メトリクス収集とは無関係だが要観察）

### 結論

前回検証 (T-20260220-027) からの状態変化なし:

1. `@opentelemetry/api` が Docker イメージに未インストールのまま
2. `diagnostics-otel` プラグインは config 上有効だが実行時にロード失敗
3. Prometheus への OTLP メトリクスプッシュは一切発生していない
4. Grafana MCP は正常に動作しており、メトリクス収集開始後のダッシュボード確認に利用可能

## 次のステップ

- [ ] `boxp/arch` リポジトリで OTel 依存追加の PR を作成
- [ ] 新イメージビルド → ArgoCD Image Updater による自動デプロイ
- [ ] メトリクス出現確認 (`count({__name__=~"openclaw_.*"})`)
- [ ] ダッシュボードでデータ表示を確認
