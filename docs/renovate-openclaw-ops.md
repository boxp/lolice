# Renovate OpenClaw イメージ更新 運用手順

## 概要

Renovateが `ghcr.io/boxp/arch/openclaw` の新しいタグを検出すると、`deployment-openclaw.yaml` のイメージタグを更新するPRを自動作成します。auto merge は無効のため、手動レビューとマージが必要です。

## RenovateのPRレビュー時の確認ポイント

1. **変更対象の確認**
   - `argoproj/openclaw/deployment-openclaw.yaml` のみが変更されていること
   - イメージタグが12桁数字（YYYYMMDDHHMM形式）であること

2. **タグの妥当性確認**
   - 新しいタグが現在のタグより新しい日時であること
   - `ghcr.io/boxp/arch/openclaw` のレジストリでタグが実在することを確認
     ```bash
     # タグの確認
     docker manifest inspect ghcr.io/boxp/arch/openclaw:<new-tag>
     ```

3. **ArgoCD Image Updaterとの整合性**
   - `.argocd-source-openclaw.yaml` に記載されているタグとの関係を確認
   - ArgoCD Image Updater は kustomize オーバーライドで実際に適用されるタグを管理
   - Renovate は `deployment-openclaw.yaml` のベースタグを更新（Kustomize適用前のデフォルト値）

4. **マージ後の動作**
   - ArgoCD が変更を検知し、sync を実行
   - ただし ArgoCD Image Updater の kustomize オーバーライドが優先されるため、deployment のベースタグ更新は実運用タグに直接影響しない
   - ベースタグを最新に保つことで、Image Updater が何らかの理由で停止した場合のフォールバックとして機能する

## トラブルシューティング

### RenovateがPRを作成しない場合
- Renovate のダッシュボードIssueでログを確認
- `ghcr.io/boxp/arch/openclaw` へのレジストリアクセス権限を確認
- `renovate.json` の regex パターンが `deployment-openclaw.yaml` の記述と一致しているか確認

### タグ比較が正しくない場合
- `renovate.json` の `versioningTemplate` が `regex:^(?<major>\d{4})(?<minor>\d{2})(?<patch>\d{6})$` であることを確認
- YYYYMMDDHHMM を YYYY(major) / MM(minor) / DDHHMM(patch) として数値比較する設計
