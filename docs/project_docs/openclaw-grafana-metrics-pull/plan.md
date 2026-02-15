# OpenClaw Grafana Metrics Pull Integration

## Overview
OpenClawコンテナがlolice cluster内のGrafanaインスタンスからメトリクスデータを取得できるようにするための設定変更。

## Changes (boxp/lolice)

### 1. NetworkPolicy
- **openclaw-network** (egress): Grafana (monitoring namespace, port 3000) へのegress追加
- **grafana** (ingress): openclaw namespace の openclaw Pod からの ingress を許可

### 2. ExternalSecret
- `openclaw-es` に `GRAFANA_API_KEY` エントリ追加
- AWS Parameter Store のキー: `/lolice/openclaw/GRAFANA_API_KEY`

### 3. Deployment
- 環境変数 `GRAFANA_API_KEY` を `openclaw-credentials` Secret から注入
- 環境変数 `GRAFANA_URL` を `http://grafana.monitoring.svc.cluster.local:3000` として設定

## Changes (boxp/arch)

### 4. Terraform SSM Parameter
- `aws_ssm_parameter.grafana_api_key` を追加
- パス: `/lolice/openclaw/GRAFANA_API_KEY`

## Post-deployment Steps
- AWS Parameter Store で `/lolice/openclaw/GRAFANA_API_KEY` の値をGrafanaで生成したAPIキーに更新
- ExternalSecretが1時間以内に自動同期
