# OpenClaw Application Metrics Dashboard (Phase 3)

## 目的

Cron/Webhook telemetry Phase 3 として、OpenClaw の OTel メトリクスを可視化する
Grafana "Application Metrics" ダッシュボードを新規作成する。

## 背景

- Phase 1: OTel SDK 統合 + Prometheus OTLP Write Receiver 有効化 → 完了
- Phase 2: `diagnostics-otel` プラグインでメトリクス定義 → 完了
- **Phase 3 (本タスク)**: Grafana ダッシュボード整備
- Phase 4 (将来): アラートルール + SLO 定義

## 変更ファイル

| ファイル | 変更内容 |
|---------|---------|
| `argoproj/prometheus-operator/grafana-dashboard-openclaw-app.yaml` | 新規 ConfigMap（ダッシュボード JSON） |
| `argoproj/prometheus-operator/kustomization.yaml` | リソース追加 |
| `argoproj/prometheus-operator/overlays/grafana.yaml` | ボリュームマウント追加 |
| `docs/project_docs/openclaw-app-metrics-dashboard/plan.md` | 本ドキュメント |
| `docs/project_docs/openclaw-app-metrics-dashboard/operations.md` | 運用手順 + Phase 4 アラート計画 |

## ダッシュボード設計

### UID / タイトル

- UID: `openclaw-app-metrics`
- Title: "OpenClaw Application Metrics"
- Tags: `openclaw`, `application`, `telemetry`

### パネル一覧

#### Row 1: Overview
| ID | パネル名 | タイプ | メトリクス |
|----|---------|--------|-----------|
| 1 | Total Tokens (24h) | stat | `openclaw_tokens_total` |
| 2 | Estimated Cost (24h) | stat | `openclaw_cost_usd_total` |
| 3 | Webhook Success Rate (1h) | gauge | `openclaw_webhook_received_total`, `openclaw_webhook_error_total` |
| 4 | Queue Depth (current) | stat | `openclaw_queue_depth` |

#### Row 2: Token Usage & Cost
| ID | パネル名 | タイプ | メトリクス |
|----|---------|--------|-----------|
| 5 | Token Usage by Type | timeseries (stacked) | `openclaw_tokens_total` by `openclaw_token` |
| 6 | Token Usage by Model | timeseries | `openclaw_tokens_total` by `openclaw_model` |
| 7 | Cost Over Time (by Model) | timeseries (stacked) | `openclaw_cost_usd_total` by `openclaw_model` |
| 8 | Cost Over Time (by Provider) | timeseries (stacked) | `openclaw_cost_usd_total` by `openclaw_provider` |

#### Row 3: Run Duration & Attempts
| ID | パネル名 | タイプ | メトリクス |
|----|---------|--------|-----------|
| 9 | Run Duration Percentiles | timeseries | `openclaw_run_duration_ms` p50/p95/p99 |
| 10 | Run Duration by Model | timeseries | `openclaw_run_duration_ms` by `openclaw_model` |
| 11 | Run Attempts | timeseries (bars) | `openclaw_run_attempt_total` by `openclaw_attempt` |
| 12 | Context Window Usage | timeseries | `openclaw_context_tokens` by `openclaw_context` |

#### Row 4: Webhook
| ID | パネル名 | タイプ | メトリクス |
|----|---------|--------|-----------|
| 13 | Webhook Received vs Errors | timeseries | `openclaw_webhook_received_total`, `openclaw_webhook_error_total` |
| 14 | Webhook Processing Duration | timeseries | `openclaw_webhook_duration_ms` p50/p95/p99 |
| 15 | Webhook by Type | timeseries | `openclaw_webhook_received_total` by `openclaw_webhook` |

#### Row 5: Message Queue
| ID | パネル名 | タイプ | メトリクス |
|----|---------|--------|-----------|
| 16 | Messages Queued vs Processed | timeseries | `openclaw_message_queued_total`, `openclaw_message_processed_total` |
| 17 | Message Processing Duration | timeseries | `openclaw_message_duration_ms` p50/p95 |
| 18 | Queue Depth Over Time | timeseries | `openclaw_queue_depth` p50/p95 |
| 19 | Queue Wait Time | timeseries | `openclaw_queue_wait_ms` p50/p95 |
| 20 | Lane Enqueue / Dequeue | timeseries | `openclaw_queue_lane_enqueue_total`, `openclaw_queue_lane_dequeue_total` |
| 21 | Message Processing Outcome | timeseries | `openclaw_message_processed_total` by `openclaw_outcome` |

#### Row 6: Session State
| ID | パネル名 | タイプ | メトリクス |
|----|---------|--------|-----------|
| 22 | Session State Transitions | timeseries | `openclaw_session_state_total` by `openclaw_state` |
| 23 | Stuck Sessions (1h) | stat | `openclaw_session_stuck_total` |
| 24 | Stuck Session Age | timeseries | `openclaw_session_stuck_age_ms` p50/p95 |

### Prometheus メトリクス名マッピング

OTel メトリクスは OTLP Write Receiver 経由で Prometheus に送信される。
命名変換規則:

| OTel メトリクス名 | Prometheus メトリクス名 | 種類 | 主要ラベル |
|------------------|----------------------|------|-----------|
| `openclaw.tokens` | `openclaw_tokens_total` | Counter | `openclaw_channel`, `openclaw_provider`, `openclaw_model`, `openclaw_token` |
| `openclaw.cost.usd` | `openclaw_cost_usd_total` | Counter | `openclaw_channel`, `openclaw_provider`, `openclaw_model` |
| `openclaw.run.duration_ms` | `openclaw_run_duration_ms` | Histogram | `openclaw_channel`, `openclaw_provider`, `openclaw_model` |
| `openclaw.context.tokens` | `openclaw_context_tokens` | Histogram | `openclaw_context` |
| `openclaw.webhook.received` | `openclaw_webhook_received_total` | Counter | `openclaw_channel`, `openclaw_webhook` |
| `openclaw.webhook.error` | `openclaw_webhook_error_total` | Counter | `openclaw_channel`, `openclaw_webhook` |
| `openclaw.webhook.duration_ms` | `openclaw_webhook_duration_ms` | Histogram | `openclaw_channel`, `openclaw_webhook` |
| `openclaw.message.queued` | `openclaw_message_queued_total` | Counter | `openclaw_channel`, `openclaw_source` |
| `openclaw.message.processed` | `openclaw_message_processed_total` | Counter | `openclaw_channel`, `openclaw_outcome` |
| `openclaw.message.duration_ms` | `openclaw_message_duration_ms` | Histogram | `openclaw_channel` |
| `openclaw.queue.depth` | `openclaw_queue_depth` | Histogram | `openclaw_channel` |
| `openclaw.queue.wait_ms` | `openclaw_queue_wait_ms` | Histogram | `openclaw_lane` |
| `openclaw.queue.lane.enqueue` | `openclaw_queue_lane_enqueue_total` | Counter | `openclaw_lane` |
| `openclaw.queue.lane.dequeue` | `openclaw_queue_lane_dequeue_total` | Counter | `openclaw_lane` |
| `openclaw.session.state` | `openclaw_session_state_total` | Counter | `openclaw_state`, `openclaw_reason` |
| `openclaw.session.stuck` | `openclaw_session_stuck_total` | Counter | `openclaw_state` |
| `openclaw.session.stuck_age_ms` | `openclaw_session_stuck_age_ms` | Histogram | `openclaw_state` |
| `openclaw.run.attempt` | `openclaw_run_attempt_total` | Counter | `openclaw_attempt` |

### Provisioning アーキテクチャ

```
ConfigMap: grafana-dashboard-openclaw-app
  └─ openclaw-app.json (ダッシュボード JSON)

Grafana Deployment (strategic merge patch):
  volumes:
    - name: grafana-dashboard-openclaw-app
      configMap:
        name: grafana-dashboard-openclaw-app
  volumeMounts:
    - mountPath: /grafana-dashboard-definitions/0/openclaw-app
      name: grafana-dashboard-openclaw-app
```

## 注意事項

- メトリクス名は OTel → Prometheus OTLP Write Receiver の自動変換に基づく
  - ドット → アンダースコア
  - Counter → `_total` サフィックス
  - Histogram → `_bucket` / `_sum` / `_count` サフィックス
- 現時点で `openclaw_*` メトリクスが Prometheus に存在しない場合、
  OpenClaw アプリケーションの diagnostics-otel が有効化されメトリクス送信が
  開始されるまでダッシュボードは "No data" を表示する
