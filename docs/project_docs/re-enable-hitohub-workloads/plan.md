# Re-enable hitohub (vr-match) workloads

## 概要
hitohub (vr-match) の本番環境ワークロードを再開するため、Deployment の replicas を 0 から 1 に変更する。

## 変更対象ファイル
- `argoproj/hitohub/overlays/prod/deployment-hitohub-backend.yaml` - replicas: 0 → 1
- `argoproj/hitohub/overlays/prod/deployment-hitohub-frontend.yaml` - replicas: 0 → 1
- `argoproj/hitohub/overlays/prod/deployment-cloudflared.yaml` - replicas: 0 → 1

## 影響
- hitohub-back-end: バックエンドAPI (port 8080) が起動
- hitohub-frontend: フロントエンド (port 3000) が起動
- cloudflared: Cloudflare Tunnel が起動し、外部アクセスが可能に
