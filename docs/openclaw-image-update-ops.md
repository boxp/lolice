# OpenClaw イメージ更新 運用手順

## 概要

`ghcr.io/boxp/arch/openclaw` の新しいタグは **ArgoCD Image Updater** が自動検出し、`.argocd-source-openclaw.yaml` の kustomize オーバーライドを更新してデプロイする。Renovate による本イメージの更新は無効化されている。

## 通常運用

### 自動更新フロー

1. boxp/arch 側で `docker/openclaw/Dockerfile` のベースイメージが更新される（Renovate PR → マージ）
2. GitHub Actions が `ghcr.io/boxp/arch/openclaw` の新タグ（YYYYMMDDHHmm形式）をビルド・プッシュ
3. ArgoCD Image Updater が新タグを検出
4. `.argocd-source-openclaw.yaml` を自動更新（git write-back）
5. ArgoCD が sync を実行しデプロイ完了

### 確認方法

- ArgoCD UI でアプリケーション `openclaw` の状態を確認
- `.argocd-source-openclaw.yaml` の内容で現在適用されているタグを確認

## トラブルシューティング

### ArgoCD Image Updater が新タグを検出しない場合

1. Image Updater の Pod ログを確認
2. `imageupdaters/openclaw.yaml` の `allowTags` パターンを確認
3. ghcr.io レジストリへのアクセス権限を確認

### 緊急時の手動更新

ArgoCD Image Updater が停止している場合、`deployment-openclaw.yaml` のイメージタグを直接更新する:

```bash
# deployment-openclaw.yaml 内の全イメージタグを更新
# initContainers・containers の全箇所を同一タグに揃えること
```

## 関連ドキュメント

- [責務分離ルール](./renovate-openclaw-ops.md)
- ArgoCD Image Updater設定: `argoproj/argocd-image-updater/imageupdaters/openclaw.yaml`
