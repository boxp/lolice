# OpenClaw Grafana MCP統合 - 実装計画

## 概要

OpenClawコンテナからアプリケーションメトリクスをPrometheusに送信し、Grafanaで監視できるようにする。

## 調査結果

### OpenClawのメトリクス機能
- OpenClawには `diagnostics-otel` プラグインが内蔵されている
- OTLP/HTTP (`http/protobuf`) プロトコルでメトリクスをプッシュする方式
- Prometheusスクレイプ (`/metrics` エンドポイント) ではなくプッシュ型
- 利用可能なメトリクス:
  - `openclaw.tokens` - トークン使用量（input/output/cache_read/cache_write/total）
  - `openclaw.cost.usd` - 推定コスト（USD）
  - `openclaw.run.duration_ms` - エージェント実行時間
  - `openclaw.context.tokens` - コンテキストウィンドウ使用量
  - `openclaw.webhook.received/error` - Webhookイベント
  - `openclaw.webhook.duration_ms` - Webhook処理時間
  - `openclaw.message.queued/processed` - メッセージ処理
  - `openclaw.message.duration_ms` - メッセージ処理時間
  - `openclaw.queue.depth/wait_ms` - キュー深度・待機時間
  - `openclaw.session.state/stuck` - セッション状態
  - `openclaw.run.attempt` - 実行試行回数

### 現在のモニタリングスタック
- Prometheus (kube-prometheus v0.16.0, `monitoring` namespace)
- Grafana (ローカルSQLite, `monitoring` namespace)
- Grafana Alloy (DaemonSet, ログ収集専用)
- Loki (ログ集約)

## 設計決定

### アプローチ: Prometheus OTLP Write Receiver

PrometheusのOTLP Write Receiver機能を有効化し、OpenClawから直接Prometheusにメトリクスをプッシュする。

**理由:**
1. Alloyに中間コレクターを追加するより直接的
2. PrometheusのOTLP受信はstable化に近い成熟した機能
3. 変更箇所が最小限（Prometheus CRDに1行追加）
4. 既存のGrafanaダッシュボードとの統合が容易

**代替案:**
- Grafana AlloyにOTLPレシーバーを追加 → DaemonSetでの運用が非効率
- OpenTelemetry Collector専用Deploymentを追加 → 過度に複雑

## 変更内容

### 1. Prometheus CRD (`prometheus-operator/overlays/prometheus.yaml`)
- `enableFeatures: ["otlp-write-receiver"]` を追加
- PrometheusがOTLP/HTTPメトリクスを9090ポートの `/api/v1/otlp` パスで受信可能になる

### 2. OpenClaw ConfigMap (`openclaw/configmap-openclaw.yaml`)
- `diagnostics.otel` セクションを追加
- endpoint: `http://prometheus-k8s.monitoring.svc:9090/api/v1/otlp`
- メトリクスのみ有効（traces/logs は無効）
- フラッシュ間隔: 30秒

### 3. OpenClaw NetworkPolicy (`openclaw/networkpolicy.yaml`)
- OpenClawからPrometheus (monitoring namespace, port 9090) へのegress通信を許可

### 4. Prometheus NetworkPolicy (`prometheus-operator/overlays/network-policy-prometheus-k8s.yaml`)
- OpenClaw (openclaw namespace) からPrometheusへのingress通信を許可

## boxp/arch への変更
今回の変更ではboxp/archリポジトリへの修正は不要。
Terraformで管理されているのはCloudflareトンネルとDNS設定のみであり、メトリクス収集の設定はすべてloliceリポジトリ内のKubernetesマニフェストで完結する。
