# T-20260223-004 n8n on lolice cluster 設計計画

## 目的
lolice cluster上でn8nを安全に運用するための設計ドキュメントとサンプルマニフェストを作成する。

## 成果物
1. **設計ドキュメント** (`docs/project_docs/n8n-external-secrets/designdoc.md`)
   - アーキテクチャ概要
   - External Secretsデータフロー
   - Kubernetesリソース一覧
   - セキュリティ要件
   - 運用手順（バックアップ/リストア/鍵ローテーション/障害対応）
   - 導入ステップ（PoC→本番）
   - レビューチェックリスト

2. **サンプルYAMLマニフェスト** (`argoproj/n8n/`)
   - ArgoCD Application定義
   - Kustomization
   - Namespace、Deployment、StatefulSet、Service、PVC
   - ExternalSecret（n8n、PostgreSQL、Cloudflare Tunnel）
   - NetworkPolicy

## 非スコープ
- `argoproj/kustomization.yaml` へのApplication追加（実デプロイは別タスク）
- 実クラスタへのapply
- AWS SSMパラメータの実際の作成（archプロジェクト側タスク）

## 注意事項
- サンプルYAMLは本番適用前にレビューと値調整が必要
- Cloudflare Tunnelのドメイン設定はCloudflareダッシュボード側で別途必要
- n8nのバージョンは`latest`タグ使用（本番運用時はバージョン固定推奨）
