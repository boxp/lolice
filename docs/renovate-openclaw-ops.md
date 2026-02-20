# OpenClaw イメージ更新 責務分離ルール

## 概要

OpenClaw Docker image の更新責務は、以下のように分離して管理する。

| リポジトリ | 更新対象 | 更新手段 |
|-----------|---------|---------|
| **boxp/lolice** | デプロイ先イメージタグ (`deployment-openclaw.yaml`) | ArgoCD Image Updater |
| **boxp/arch** | ベースイメージ (`docker/openclaw/Dockerfile` の `FROM ghcr.io/openclaw/openclaw`) | Renovate |

## boxp/lolice（本リポジトリ）

### ArgoCD Image Updater による管理

- `ghcr.io/boxp/arch/openclaw` の新タグ検出・デプロイは **ArgoCD Image Updater** が担当
- 設定: `argoproj/argocd-image-updater/imageupdaters/openclaw.yaml`
- 戦略: `newest-build`（12桁YYYYMMDDHHmmタグ）
- 書き戻し先: `.argocd-source-openclaw.yaml`（kustomize オーバーライド）

### Renovate の役割

- `ghcr.io/boxp/arch/openclaw` に対する Renovate 更新は **無効化** (`enabled: false`)
- `renovate.json` の `packageRules` で明示的に除外
- cloudflared など他のイメージは引き続き Renovate が管理

## boxp/arch

### Renovate による管理

- `docker/openclaw/Dockerfile` の上流ベースイメージ `ghcr.io/openclaw/openclaw` の更新は **Renovate** が担当
- 新しいベースイメージが検出されるとPRを自動作成
- PRマージ → GitHub Actions でカスタムイメージ (`ghcr.io/boxp/arch/openclaw`) をビルド・プッシュ
- その後、lolice側の ArgoCD Image Updater が新タグを検出してデプロイ

## 更新フロー全体像

```
[openclaw/openclaw 上流リリース]
  ↓ Renovate (boxp/arch)
[boxp/arch: Dockerfile ベースイメージ更新 PR]
  ↓ マージ
[GitHub Actions: ghcr.io/boxp/arch/openclaw ビルド・プッシュ]
  ↓ ArgoCD Image Updater (boxp/lolice)
[lolice: デプロイタグ自動更新 → ArgoCD sync]
```

## 注意事項

- lolice側で Renovate による OpenClaw イメージ更新PRが作成された場合は設定ミスの可能性があるため調査すること
- ArgoCD Image Updater が停止した場合は `deployment-openclaw.yaml` のイメージタグを手動更新する
