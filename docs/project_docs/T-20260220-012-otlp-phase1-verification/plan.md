# T-20260220-012: OTLP Phase 1 疎通確認結果

## 概要

OpenClaw Pod から Prometheus OTLP endpoint への到達性とメトリクス出現を検証した Phase 1 の結果報告。

## 検証日時

2026-02-20

## 検証項目と結果

### 1. lolice 環境の現行 OTLP 設定棚卸し

| コンポーネント | 設定 | 状態 |
|---|---|---|
| Prometheus `otlp-write-receiver` | `enableFeatures: ["otlp-write-receiver"]` | 有効 |
| OpenClaw ConfigMap `diagnostics.otel` | endpoint, protocol, serviceName 等 | 設定済み |
| NetworkPolicy (openclaw egress → prometheus:9090) | cross-namespace egress | 設定済み |
| NetworkPolicy (prometheus ingress ← openclaw) | cross-namespace ingress | 設定済み |
| NetworkPolicy (openclaw egress → grafana:3000) | cross-namespace egress | 設定済み |

**ConfigMap の diagnostics セクション (現行):**

```json
"diagnostics": {
  "enabled": true,
  "otel": {
    "enabled": true,
    "endpoint": "http://prometheus-k8s.monitoring.svc:9090/api/v1/otlp",
    "protocol": "http/protobuf",
    "serviceName": "openclaw",
    "traces": false,
    "metrics": true,
    "logs": false,
    "flushIntervalMs": 30000
  }
}
```

### 2. OpenClaw Pod → Prometheus OTLP endpoint 疎通確認

#### 結果: メトリクスが Prometheus に到達していない

Prometheus で `openclaw` を含むメトリクス名を検索した結果、**0件**。
`otel`, `otlp`, `target_info` (OTLP 由来) も **0件**。

`service_name` ラベルに `openclaw` は存在しない (存在する値は `external-secrets-webhook`, `longhorn-admission-webhook` のみ)。

#### Loki ログ確認

- `[diagnostic]` タグのログは出力されている (stuck session 検知等) → diagnostics コア機能は動作中
- OTLP/OpenTelemetry 関連のログ (SDK 初期化、エクスポートエラー等): **0件** (2026-02-17 以降)
- `prometheus` に言及するログ: **0件**

### 3. 根本原因の特定

**`diagnostics-otel` プラグインが有効化されていない。**

OpenClaw のプラグインシステムでは、`extensions/` ディレクトリ内のバンドルプラグインはデフォルトで無効化される (`resolveEnableState` の判定ロジック)。明示的に有効化が必要なプラグイン:

- `BUNDLED_ENABLED_BY_DEFAULT` セット: `device-pair`, `phone-control`, `talk-voice` のみ
- `diagnostics-otel` は**このセットに含まれない**

**現行 ConfigMap の `plugins.entries`:**

```json
"plugins": {
  "entries": {
    "discord": { "enabled": true },
    "telegram": { "enabled": true }
  }
}
```

`diagnostics-otel` が未登録のため、プラグインの `register()` 関数が呼ばれず、OTEL SDK も起動しない。

#### 影響範囲

`diagnostics.otel` セクションの設定は正しいが、プラグインがロードされないため全て無効化されている:

| 設定 | 状態 | 備考 |
|---|---|---|
| `diagnostics.otel.enabled: true` | 参照されない | プラグインのサービスが起動しないため |
| `diagnostics.otel.endpoint` | 参照されない | 同上 |
| NetworkPolicy (OTLP egress/ingress) | 有効だが使われない | 通信自体が発生しない |
| Prometheus `otlp-write-receiver` | 有効だが利用されない | プッシュが来ない |

### 4. OTLP メトリクス名前変換 (OTLP → Prometheus)

OpenClaw が送出するメトリクス名は OTLP ドット区切り形式。Prometheus OTLP Write Receiver はドットをアンダースコアに変換する。

| OTLP メトリクス名 | 想定 Prometheus 名 | 型 | 説明 |
|---|---|---|---|
| `openclaw.tokens` | `openclaw_tokens_total` | Counter | トークン使用量 |
| `openclaw.cost.usd` | `openclaw_cost_usd_total` | Counter | 推定コスト (USD) |
| `openclaw.run.duration_ms` | `openclaw_run_duration_ms` | Histogram | エージェント実行時間 |
| `openclaw.context.tokens` | `openclaw_context_tokens` | Histogram | コンテキストウィンドウ使用量 |
| `openclaw.webhook.received` | `openclaw_webhook_received_total` | Counter | Webhook 受信数 |
| `openclaw.webhook.error` | `openclaw_webhook_error_total` | Counter | Webhook エラー数 |
| `openclaw.webhook.duration_ms` | `openclaw_webhook_duration_ms` | Histogram | Webhook 処理時間 |
| `openclaw.message.queued` | `openclaw_message_queued_total` | Counter | メッセージキュー投入数 |
| `openclaw.message.processed` | `openclaw_message_processed_total` | Counter | メッセージ処理完了数 |
| `openclaw.message.duration_ms` | `openclaw_message_duration_ms` | Histogram | メッセージ処理時間 |
| `openclaw.queue.depth` | `openclaw_queue_depth` | Histogram | キュー深度 |
| `openclaw.queue.wait_ms` | `openclaw_queue_wait_ms` | Histogram | キュー待機時間 |
| `openclaw.queue.lane.enqueue` | `openclaw_queue_lane_enqueue_total` | Counter | レーンエンキュー数 |
| `openclaw.queue.lane.dequeue` | `openclaw_queue_lane_dequeue_total` | Counter | レーンデキュー数 |
| `openclaw.session.state` | `openclaw_session_state_total` | Counter | セッション状態遷移数 |
| `openclaw.session.stuck` | `openclaw_session_stuck_total` | Counter | スタックセッション数 |
| `openclaw.session.stuck_age_ms` | `openclaw_session_stuck_age_ms` | Histogram | スタック経過時間 |
| `openclaw.run.attempt` | `openclaw_run_attempt_total` | Counter | 実行試行回数 |

> **注意**: Counter は Prometheus 命名規約により `_total` サフィックスが付与される場合がある。実際の名前はプラグイン有効化後に確認が必要。

### 5. 想定される属性ラベル

OTEL SDK が `service.name=openclaw` としてリソース属性を設定するため、以下のラベルが付与される:

- `service_name="openclaw"`
- `service_namespace` (設定されている場合)
- `job` (Prometheus 側で自動付与)

メトリクス固有の属性:

| メトリクス | 属性 (ラベル) |
|---|---|
| `openclaw.tokens` | `direction` (input/output/cache_read/cache_write/prompt/total), `model`, `provider` |
| `openclaw.cost.usd` | `model`, `provider` |
| `openclaw.run.duration_ms` | `model`, `provider`, `status` |
| `openclaw.webhook.*` | `source`, `status` |
| `openclaw.message.*` | `channel`, `source` |
| `openclaw.session.*` | `sessionKey`, `state` |

## 修正内容

### ConfigMap 差分 (`argoproj/openclaw/configmap-openclaw.yaml`)

```diff
       "plugins": {
         "entries": {
           "discord": {
             "enabled": true
           },
           "telegram": {
             "enabled": true
+          },
+          "diagnostics-otel": {
+            "enabled": true
           }
         }
       },
```

この 1 行の追加で:

1. `diagnostics-otel` プラグインがロードされる
2. プラグインの `register()` で OTEL サービスが登録される
3. Gateway 起動時に `service.start()` が呼ばれ、`NodeSDK` が初期化される
4. `PeriodicExportingMetricReader` が 30 秒間隔で Prometheus OTLP endpoint にメトリクスをプッシュする

### 追加の変更は不要

| 項目 | 理由 |
|---|---|
| NetworkPolicy | openclaw → prometheus:9090 の egress/ingress は設定済み |
| Prometheus config | `otlp-write-receiver` 有効化済み |
| endpoint URL | `http://prometheus-k8s.monitoring.svc:9090/api/v1/otlp` は正しい |
| Grafana datasource | Prometheus datasource (`P1809F7CD0C75ACF3`) でそのまま参照可能 |

## リスク

| リスク | 影響度 | 緩和策 |
|---|---|---|
| OTLP メトリクス名が想定と異なる可能性 | 低 | ArgoCD 同期後に Prometheus で実名を確認し、ダッシュボード設計 (Phase 3) に反映 |
| Prometheus ストレージ使用量増加 | 低 | 18メトリクス × 少数ラベル組合せ、現行 60Gi / 31d に十分な余裕 |
| Pod の OTEL SDK メモリ使用量 | 低 | `flushIntervalMs: 30000` でバッチ送信、メモリ影響は微量 |
| config-manager sidecar 経由の hot-reload で即座に反映 | 低 | chokidar watcher で検知、Pod 再起動不要 |

## 次のアクション

1. **本 PR マージ後**: ArgoCD が ConfigMap を同期、config-manager sidecar が変更を検知し OpenClaw が hot-reload
2. **メトリクス出現確認**: 30 秒〜数分後に Prometheus で `openclaw_*` メトリクスの出現を確認
3. **Phase 2** (T-20260219-014): Cron Webhook Token 設定
4. **Phase 3** (T-20260219-014): Grafana Application Metrics ダッシュボード作成
