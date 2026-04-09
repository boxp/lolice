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

## 関連 Issue: openclaw/openclaw#6989

### Issue 概要

- **タイトル**: [Bug] diagnostics-otel plugin fails to load: "Cannot find module '@opentelemetry/api'"
- **リポジトリ**: `openclaw/openclaw`
- **Issue 番号**: [#6989](https://github.com/openclaw/openclaw/issues/6989)
- **状態**: Open
- **ラベル**: `bug`, `stale`
- **報告者**: Gaurang-Patel
- **報告日**: OpenClaw v2026.1.30 で報告

### 因果関係

Issue #6989 で報告されている現象は、本環境で観測されている
`openclaw_*` メトリクス欠損の**直接原因**と同一である。

```
[upstream Issue #6989]
  diagnostics-otel が @opentelemetry/api を require → MODULE_NOT_FOUND
    ↓
[本環境での影響]
  diagnostics-otel プラグインの register() が呼ばれない
    → OTEL NodeSDK が初期化されない
      → PeriodicExportingMetricReader が起動しない
        → Prometheus OTLP endpoint へのメトリクスプッシュが発生しない
          → Grafana ダッシュボード (PR #471) の全 18 パネルが "No data"
```

Issue #6989 が修正されない限り、本環境では `boxp/arch` Dockerfile での
OTel パッケージ追加インストール（前述 Option A）が必要となる。

## 再検証 (T-20260220-028 第2回): 2026-02-21 最新確認

### 検証日時

2026-02-21 00:07 UTC

### Grafana MCP 利用可否

| ツール | 結果 | 備考 |
|--------|------|------|
| `list_datasources` | OK | 3件: prometheus, Loki, alertmanager |
| `search_dashboards` (query: "openclaw") | OK | 2件: Application Metrics, Container Monitoring |
| `list_prometheus_metric_names` (regex: `openclaw_.*`) | OK (0件) | メトリクス未収集を再確認 |
| `list_prometheus_label_values` (service_name) | OK | `openclaw` なし (external-secrets-webhook, longhorn-admission-webhook のみ) |
| `query_loki_logs` | OK | ログ取得成功 |

**結論: Grafana MCP は全ツール正常動作中。**

### Prometheus メトリクス確認 (2026-02-21)

```
list_prometheus_metric_names(regex="openclaw_.*") → []  (0件)
```

全 18 ベースメトリクスが依然として Prometheus に存在しない。

### Loki ログ確認 (2026-02-21)

Pod `openclaw-7df8795bd7-gdskr` (2026-02-20T23:53:53Z 起動) が現行稼働中。
起動時に `@opentelemetry/api` 欠損エラーが発生:

| Pod | 起動時刻 (UTC) | エラー |
|-----|--------------|--------|
| `openclaw-7df8795bd7-gdskr` | 2026-02-20T23:53:53 | `Cannot find module '@opentelemetry/api'` |
| `openclaw-9b94c5fff-dnql2` | 2026-02-20T19:31:01 / 21:19:19 (再起動2回) | 同上 |
| `openclaw-559ddcc574-2cfwx` | 2026-02-20T14:33:27 | 同上 |
| `openclaw-5579969465-658k4` | 2026-02-20T13:42:28 | 同上 |
| `openclaw-5869587775-z8n2v` | 2026-02-20T13:35:07 | 同上 |

合計 5 Pod 世代で同一エラーが継続発生中。

### 追加観察: fsnotify エラー

現行 Pod で `failed to create fsnotify watcher: too many open files` エラーも観測。
メトリクス収集とは直接無関係だが、ファイルディスクリプタ枯渇の兆候として要注意。

### 結論 (2026-02-21)

前回検証 (2026-02-20) からの状態変化なし:

1. `@opentelemetry/api` が Docker イメージに未インストールのまま（Issue #6989 と同一原因）
2. `diagnostics-otel` プラグインは config 上有効だが実行時にロード失敗
3. Prometheus への OTLP メトリクスプッシュは一切発生していない
4. Grafana MCP は正常に動作しており、メトリクス収集開始後のダッシュボード確認に利用可能

## 次のステップ

- [x] `boxp/arch` リポジトリで OTel 依存追加の PR を作成（Issue #6989 のワークアラウンド） → **PR #7123 でマージ済み (2026-02-21)**
- [ ] upstream `openclaw/openclaw` Issue #6989 にコメントで本環境の再現情報を追記
- [x] 新イメージビルド → ArgoCD Image Updater による自動デプロイ → **`202602240649` で反映済み**
- [ ] メトリクス出現確認 (`count({__name__=~"openclaw_.*"})`)
- [ ] ダッシュボードでデータ表示を確認

---

## 再検証 (T-20260225-009): PR #7123 反映後の確認

### 検証日時

2026-02-25 16:00 UTC

### 背景

`boxp/arch` PR #7123 (`feat: install opentelemetry deps in openclaw image`) が 2026-02-21 にマージされ、
ArgoCD Image Updater により `ghcr.io/boxp/arch/openclaw:202602240649` としてデプロイ済み。
このPRで以下の OTel パッケージが追加された:

- `@opentelemetry/api`
- `@opentelemetry/sdk-node`
- `@opentelemetry/sdk-metrics`
- `@opentelemetry/exporter-metrics-otlp-proto`

インストール先は `/opt/otel-deps/node_modules`、`ENV NODE_PATH="/opt/otel-deps/node_modules"` で参照可能にしている。

### 環境情報

| 項目 | 値 |
|------|-----|
| OpenClaw バージョン | v2026.2.22-2 |
| Docker イメージ | `ghcr.io/boxp/arch/openclaw:202602240649` |
| 現行 Pod | `openclaw-54446c6df9-mqw64` |
| Pod 起動時刻 | 2026-02-24T07:00:27 UTC |
| Prometheus バージョン | 3.5.0 |

### Prometheus メトリクス確認

```
list_prometheus_metric_names(regex="openclaw_.*") → []  (0件)
list_prometheus_label_values(service_name) → ["external-secrets-webhook", "longhorn-admission-webhook"]
  → "openclaw" なし
target_info → 存在しない（OTel SDK 未初期化の証拠）
```

**全 18 ベースメトリクスが依然として Prometheus に存在しない（0/18）。**

### Loki ログ確認（変化あり）

#### 改善点: `@opentelemetry/api` エラー消失

```
LogQL: {container="openclaw"} |~ "(?i)(otel|telemetry|Cannot find module|opentelemetry)"
期間: 2026-02-24T00:00:00Z 〜 2026-02-25T23:59:59Z
結果: 0件
```

**PR #7123 の効果で `Cannot find module '@opentelemetry/api'` エラーが完全に消失した。**

前回検証 (2026-02-21) では全 Pod 世代で発生していたこのエラーが、
新イメージ反映後は一切発生していない。

#### 未改善点: プラグインロード成功ログも不在

```
LogQL: {container="openclaw"} |~ "(?i)(diagnostics-otel|plugin.*load|plugin.*fail)"
期間: 2026-02-24T00:00:00Z 〜 2026-02-25T23:59:59Z
結果: 0件
```

以前は `[gateway] [plugins] diagnostics-otel failed to load` というエラーが明確に出ていたが、
現在はプラグインに関するログが**成功も失敗も一切存在しない**。

#### 未改善点: OTLP export 関連ログ不在

```
LogQL: {container="openclaw"} |~ "(?i)(otlp|prometheus|metrics|export)"
期間: 2026-02-24T00:00:00Z 〜 2026-02-25T23:59:59Z
結果: 0件（exec コマンドログのみ、OTLP 関連はなし）
```

OTel SDK や Exporter の初期化・送信に関するログが一切存在しない。

#### 起動シーケンス分析 (2026-02-24T07:00:27)

| 時刻 (UTC) | タグ | メッセージ |
|------------|------|----------|
| 07:00:27.609 | `[canvas]` | host mounted |
| 07:00:27.863 | `[heartbeat]` | started |
| 07:00:27.870 | `[health-monitor]` | started (interval: 300s) |
| 07:00:27.878 | `[gateway]` | agent model: openai-codex/gpt-5.3-codex |
| 07:00:27.881 | `[gateway]` | listening on ws://0.0.0.0:18789 |
| 07:00:27.887 | `[gateway]` | log file path |
| 07:00:27.891 | `[gateway]` | security warning |
| 07:00:27.926 | `[browser/service]` | ready |
| 07:00:28.252 | `[gateway]` | update available v2026.2.23 |
| 07:00:28.703 | `[delivery-recovery]` | recovery |
| 07:00:28.716 | `[discord]` | starting provider |
| 07:00:31.258 | `[telegram]` | starting provider |

**注目: `[plugins]` タグのログが起動シーケンスに一切存在しない。**

以前のバージョンでは:
```
[gateway] [plugins] diagnostics-otel failed to load from /app/extensions/diagnostics-otel/index.ts
```
というログが起動時に出ていたが、現在のバージョン (v2026.2.22-2) ではプラグインシステムの
ログ出力自体が変更された可能性がある。

### 診断モジュールの動作確認

`[diagnostic]` タグのログは正常に出力されている:

```
[diagnostic] stuck session: sessionId=main state=processing age=154s queueDepth=0
[diagnostic] lane wait exceeded: lane=session:agent:main:main waitedMs=5833 queueAhead=0
```

これは診断（diagnostic）モジュール自体は動作しているが、
OTel メトリクスのエクスポート部分が機能していないことを示す。

### 根本原因分析

#### 状態変化のまとめ

| 項目 | 前回 (2026-02-21) | 今回 (2026-02-25) | 変化 |
|------|------------------|------------------|------|
| `@opentelemetry/api` エラー | 発生 | **消失** | 改善 |
| プラグインロード失敗ログ | 発生 | **消失** | 変化 |
| プラグインロード成功ログ | なし | なし | 変化なし |
| OTLP export ログ | なし | なし | 変化なし |
| `target_info` (OTel自動生成) | なし | なし | 変化なし |
| `openclaw_*` メトリクス | 0/18 | **0/18** | 変化なし |

#### 新たな根本原因仮説

PR #7123 により `@opentelemetry/api` のモジュール解決は成功するようになったが、
メトリクスが依然として Prometheus に到達していない。以下の仮説を提示する:

##### 仮説 1: diagnostics-otel プラグインの内部初期化失敗（最有力）

PR #7123 は OTel パッケージを `/opt/otel-deps/node_modules` にインストールし
`NODE_PATH` で参照可能にした。`@opentelemetry/api` の import は成功するが:

- プラグイン内部の `@opentelemetry/sdk-node` 初期化時に、
  `/app/node_modules` 内の既存パッケージとバージョン競合が発生している可能性
- OTel SDK の NodeSDK 初期化は try-catch でラップされており、
  失敗時にエラーログを出さず静かに失敗する実装が多い
- `target_info` すら存在しないことから、SDK 初期化自体が完了していないと推定

根拠:
- `@opentelemetry/api` import 成功 → エラー消失
- `target_info` 不在 → SDK 初期化未完了
- `[plugins]` ログ不在 → プラグインシステムの動作が不明

##### 仮説 2: OTLP エクスポーターの送信失敗（サイレント）

OTel SDK は初期化に成功したが、OTLP エクスポーターが Prometheus への
HTTP POST に失敗している。OTel JS SDK のデフォルト動作では export エラーは
内部の diag handler に送られ、stderr には出力されない。

根拠:
- Prometheus 3.5.0 での OTLP endpoint パスが変更されている可能性
- NetworkPolicy は設定済みだが、実際の接続テスト未実施

##### 仮説 3: OpenClaw v2026.2.22 でのプラグインシステム変更

v2026.2.22 でプラグインのロード方式が変更され、
`/app/extensions/diagnostics-otel/` からの動的ロードが行われなくなった可能性。

根拠:
- `[plugins]` タグのログが一切出力されなくなった
- 起動シーケンスにプラグイン関連のフェーズが見当たらない

#### 推定因果関係（更新版）

```
PR #7123 により OTel パッケージを /opt/otel-deps に追加
  → @opentelemetry/api の import エラーは解消
    → しかし、以下のいずれかが発生:
      (A) SDK 初期化時にバージョン競合/内部エラーで静かに失敗
      (B) Exporter が Prometheus OTLP endpoint へのプッシュに失敗（サイレント）
      (C) v2026.2.22 でプラグインシステムが変更され、拡張が読み込まれない
        → いずれの場合も OTel メトリクスがエクスポートされない
          → Prometheus に openclaw_* が存在しない (0/18)
```

### 設定面の再検証

| 項目 | 設定 | 状態 |
|------|------|------|
| `diagnostics.otel.enabled` | `true` | OK |
| `diagnostics.otel.metrics` | `true` | OK |
| `diagnostics.otel.endpoint` | `http://prometheus-k8s.monitoring.svc:9090/api/v1/otlp` | OK |
| `diagnostics.otel.protocol` | `http/protobuf` | OK |
| `diagnostics.otel.serviceName` | `openclaw` | OK |
| `diagnostics.otel.flushIntervalMs` | `30000` | OK |
| `plugins.entries.diagnostics-otel.enabled` | `true` | OK |
| Prometheus `otlp-write-receiver` | `enableFeatures: ["otlp-write-receiver"]` | OK |
| Prometheus バージョン | 3.5.0 | OK (OTLP はデフォルト有効) |
| NetworkPolicy (openclaw → prometheus:9090) | egress 許可 | OK |
| NetworkPolicy (prometheus ← openclaw) | ingress 許可 | OK |

**設定面には問題なし。**

### 対応案（更新版）

#### 短期: OTel デバッグログの有効化

OpenClaw の設定に OTel diagnostics の詳細ログを有効化して原因を特定する:

```json
{
  "diagnostics": {
    "otel": {
      "debug": true
    }
  }
}
```

または環境変数でOTel SDK のデバッグログを有効化:

```yaml
env:
  - name: OTEL_LOG_LEVEL
    value: "debug"
  - name: OTEL_TRACES_EXPORTER
    value: "none"
  - name: OTEL_METRICS_EXPORTER
    value: "otlp"
```

#### 短期: OpenClaw v2026.2.23 へのアップデート

起動ログで `update available (latest): v2026.2.23 (current v2026.2.22-2)` と表示されている。
v2026.2.23 で OTel プラグインの修正が含まれている可能性がある。

#### 中期: boxp/arch Dockerfile での OTel パッケージインストール方法の見直し

PR #7123 の `/opt/otel-deps` + `NODE_PATH` 方式が動作しない場合、
`/app/node_modules` 内に直接インストールする方式に変更:

```dockerfile
USER root
RUN cd /app && npm install --no-save \
    @opentelemetry/api \
    @opentelemetry/sdk-node \
    @opentelemetry/sdk-metrics \
    @opentelemetry/exporter-metrics-otlp-proto
USER node
```

### 次のステップ

- [ ] ConfigMap に `OTEL_LOG_LEVEL=debug` 環境変数を追加して再デプロイし、OTel 内部エラーを特定
- [ ] OpenClaw v2026.2.23 で diagnostics-otel の挙動変更がないか changelog を確認
- [ ] v2026.2.23 へのアップデートを検討
- [ ] 改善しない場合、OTel パッケージを `/app/node_modules` に直接インストールする方式に変更
- [ ] upstream `openclaw/openclaw` Issue #6989 の進捗を確認
