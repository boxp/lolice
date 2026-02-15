# Grafana MCP Integration - lolice リポジトリ変更計画

## 概要

OpenClawコンテナ内のClaude Codeエージェントが、Grafana MCP Server（grafana/mcp-grafana）を
通じてGrafanaのメトリクス・ダッシュボード・ログをコンテキストとして扱えるようにする。

## 前提条件

- PR #424: Prometheus OTLP Write Receiver有効化、OTLPメトリクスプッシュ設定（実装済み）
- PR #425: Grafana APIアクセス用NetworkPolicy、環境変数、ExternalSecret（実装済み）
- boxp/arch: mcp-grafanaバイナリのDockerイメージ組み込み、Claude Code settings.json MCP設定（本PR）

## 変更内容

### 1. Deployment (`argoproj/openclaw/deployment-openclaw.yaml`)
- `GRAFANA_SERVICE_ACCOUNT_TOKEN` 環境変数を追加
  - mcp-grafana v0.10.0が推奨する環境変数名
  - 既存の `GRAFANA_API_KEY` と同じSecretから取得（後方互換性維持）
  - mcp-grafanaプロセスが親プロセス（OpenClaw）から環境変数を継承して利用

### データフロー
```
Claude Code Agent (OpenClaw内)
  ↓ MCP Protocol (stdio)
mcp-grafana (同一コンテナ内子プロセス)
  ↓ Grafana HTTP API
Grafana (monitoring namespace, port 3000)
  ↓ Data Source Query
Prometheus / Loki (monitoring namespace)
```

### 利用可能なMCPツール（読み取り専用モード）
- `search_dashboards` - ダッシュボード検索
- `get_dashboard_by_uid` - ダッシュボード取得
- `query_prometheus` - PromQLクエリ実行
- `list_prometheus_metric_names` - メトリクス名一覧
- `query_loki_logs` - ログ検索
- `list_datasources` - データソース一覧
- `list_alert_rules` - アラートルール一覧

### セキュリティ
- `--disable-write` モードにより書き込み操作は全て無効化
- NetworkPolicyによりOpenClaw → Grafana (port 3000) のみ通信許可
- Grafana Service Account TokenはAWS SSM Parameter Store経由で管理
