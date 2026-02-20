# OpenClaw イメージ更新 PRレビュー手順

## 概要

Renovateが `ghcr.io/boxp/arch/openclaw` の新しいタグ（YYYYMMDDHHMM形式）を検出すると、`deployment-openclaw.yaml` のイメージタグを更新するPRを自動作成する。auto merge は無効のため、手動レビューとマージが必要。

## レビュー時の確認ポイント

### 1. 変更対象の確認

- `argoproj/openclaw/deployment-openclaw.yaml` のみが変更されていること
- イメージタグが12桁数字（YYYYMMDDHHMM形式）であること
- initContainers・containers 全てのOpenClawイメージタグが同一であること

### 2. タグの妥当性確認

- 新しいタグが現在のタグより新しい日時であること
- レジストリでタグが実在することを確認:
  ```bash
  docker manifest inspect ghcr.io/boxp/arch/openclaw:<new-tag>
  ```

### 3. ArgoCD Image Updater との整合性

- `.argocd-source-openclaw.yaml` の kustomize オーバーライドが実際に適用されるタグを管理
- Renovate は `deployment-openclaw.yaml` のベースタグを更新（kustomize適用前のデフォルト値）
- ベースタグを最新に保つことで、Image Updater 停止時のフォールバックとして機能する

### 4. マージ後の動作

- ArgoCD が変更を検知し sync を実行
- ArgoCD Image Updater の kustomize オーバーライドが優先されるため、ベースタグ更新は通常運用に直接影響しない

## トラブルシューティング

### RenovateがPRを作成しない場合

1. Renovate ダッシュボードIssueでログを確認
2. `ghcr.io/boxp/arch/openclaw` へのレジストリアクセス権限を確認
3. `renovate.json` の regex パターンが `deployment-openclaw.yaml` の記述と一致しているか確認

### タグ比較が正しくない場合

- `versioningTemplate` が `regex:^(?<major>\d{4})(?<minor>\d{2})(?<patch>\d{6})$` であることを確認
- YYYYMMDDHHMM を YYYY(major) / MM(minor) / DDHHMM(patch) として数値比較する設計
