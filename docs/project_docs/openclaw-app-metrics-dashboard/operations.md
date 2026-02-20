# OpenClaw Application Metrics - 運用手順 & Phase 4 アラート計画

## 1. ダッシュボード運用手順

### 1.1 ダッシュボードへのアクセス

Grafana にログイン後、以下のいずれかの方法でアクセス:
- Dashboards → Browse → タグ `openclaw` でフィルタ
- 直接 URL: `/d/openclaw-app-metrics/openclaw-application-metrics`

### 1.2 日常監視チェックリスト

| 確認項目 | パネル | 正常基準 | 異常時アクション |
|---------|--------|---------|---------------|
| トークン消費量 | Total Tokens (24h) | 前日比 ±50% 以内 | モデル変更やプロンプト肥大化を確認 |
| コスト | Estimated Cost (24h) | 日次予算以内 | モデル / provider の使用比率を確認 |
| Webhook 成功率 | Webhook Success Rate | ≥ 99% | エラーログを確認、チャネル側の障害チェック |
| キュー深度 | Queue Depth | ≤ 10 | 処理遅延の原因調査（CPU/メモリ、外部API） |
| スタックセッション | Stuck Sessions | 0 | セッション状態と age を確認、必要に応じ再起動 |

### 1.3 トラブルシューティング

#### メトリクスが "No data" の場合

1. OpenClaw の diagnostics-otel が有効か確認:
   ```bash
   kubectl get configmap openclaw -n openclaw -o jsonpath='{.data.config\.json}' | jq '.diagnostics'
   ```
2. Prometheus OTLP エンドポイントへの疫通確認:
   ```bash
   kubectl exec -n openclaw deploy/openclaw -- \
     curl -s -o /dev/null -w '%{http_code}' \
     http://prometheus-k8s.monitoring.svc:9090/api/v1/otlp/v1/metrics
   ```
3. Prometheus でメトリクス存在確認:
   ```promql
   count({__name__=~"openclaw_.*"})
   ```

#### diagnostics-otel プラグインのロード失敗

OTel 依存パッケージが Docker イメージに含まれていない場合、以下のエラーが発生する:

```
[plugins] diagnostics-otel failed to load from /app/extensions/diagnostics-otel/index.ts:
  Error: Cannot find module '@opentelemetry/api'
```

確認方法:
```logql
{namespace="openclaw", container="openclaw"} |~ "Cannot find module.*opentelemetry"
```

対処:
1. `boxp/arch` の `docker/openclaw/Dockerfile` で OTel パッケージを追加インストール
2. 必要なパッケージ: `@opentelemetry/api`, `@opentelemetry/sdk-node`, `@opentelemetry/sdk-metrics`, `@opentelemetry/exporter-metrics-otlp-proto`
3. 新イメージビルド後、ArgoCD Image Updater が自動デプロイ

#### Webhook エラー率が高い場合

1. エラーの内訳を確認:
   ```promql
   sum by (openclaw_webhook, openclaw_channel) (rate(openclaw_webhook_error_total[5m]))
   ```
2. Container Monitoring ダッシュボードで OOM / リスタートを確認
3. Loki でエラーログを検索:
   ```logql
   {namespace="openclaw"} |~ "webhook.error|webhook.*failed"
   ```

#### キュー詰まりの場合

1. キュー深度の推移を確認（Queue Depth Over Time パネル）
2. メッセージ処理時間の増加を確認（Message Processing Duration パネル）
3. 処理 outcome を確認（Message Processing Outcome パネル）
4. リソース不足を確認（Container Monitoring ダッシュボード）

---

## 2. Phase 4 アラートルール計画

### 2.1 予定アラート一覧

| アラート名 | 条件 | Severity | for |
|-----------|------|----------|-----|
| `OpenClawWebhookErrorRateHigh` | Webhook エラー率 > 5% (5分間) | warning | 5m |
| `OpenClawWebhookErrorRateCritical` | Webhook エラー率 > 20% (5分間) | critical | 2m |
| `OpenClawQueueDepthHigh` | キュー深度 p95 > 20 | warning | 10m |
| `OpenClawQueueDepthCritical` | キュー深度 p95 > 50 | critical | 5m |
| `OpenClawSessionStuck` | スタックセッション数 > 0 (15分間) | warning | 15m |
| `OpenClawRunDurationSlow` | Run duration p95 > 120000ms (2分) | warning | 10m |
| `OpenClawCostBudgetExceeded` | 日次コスト > $50 (推定) | warning | 30m |
| `OpenClawTokenUsageSpike` | トークン使用量が前日の 3x | warning | 15m |

### 2.2 PromQL テンプレート

```yaml
# Webhook エラー率
- alert: OpenClawWebhookErrorRateHigh
  expr: |
    sum(rate(openclaw_webhook_error_total[5m]))
    / clamp_min(sum(rate(openclaw_webhook_received_total[5m])), 0.001)
    > 0.05
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "OpenClaw webhook error rate is above 5%"
    dashboard: "/d/openclaw-app-metrics/openclaw-application-metrics"

# キュー深度
- alert: OpenClawQueueDepthHigh
  expr: |
    histogram_quantile(0.95,
      sum by (le) (rate(openclaw_queue_depth_bucket[5m]))
    ) > 20
  for: 10m
  labels:
    severity: warning

# スタックセッション
- alert: OpenClawSessionStuck
  expr: |
    sum(increase(openclaw_session_stuck_total[15m])) > 0
  for: 15m
  labels:
    severity: warning

# Run duration 劣化
- alert: OpenClawRunDurationSlow
  expr: |
    histogram_quantile(0.95,
      sum by (le) (rate(openclaw_run_duration_ms_bucket[5m]))
    ) > 120000
  for: 10m
  labels:
    severity: warning
```

### 2.3 SLO 指標案

| SLI | SLO | 計測方法 |
|-----|-----|---------|
| Webhook 可用性 | 99.5% (月間) | `1 - (error_total / received_total)` |
| メッセージ処理レイテンシ | p95 < 30s | `histogram_quantile(0.95, message_duration_ms)` |
| キュー待機時間 | p95 < 10s | `histogram_quantile(0.95, queue_wait_ms)` |
| スタックセッション | 0 (24h window) | `sum(increase(session_stuck_total[24h]))` |

### 2.4 通知チャネル

Phase 4 で設定予定:
- **warning**: Slack `#openclaw-alerts` チャネル
- **critical**: Slack `#openclaw-alerts` + PagerDuty

---

## 3. ダッシュボード更新手順

1. `grafana-dashboard-openclaw-app.yaml` の JSON を編集
2. `kustomize build` でマニフェスト検証
3. Git commit & push → ArgoCD が自動同期
4. Grafana で変更が反映されていることを確認
