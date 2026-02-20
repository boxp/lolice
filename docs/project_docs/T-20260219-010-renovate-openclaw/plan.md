# T-20260219-010: Renovate半自動化 - OpenClawイメージ更新

## 概要

`ghcr.io/boxp/arch/openclaw` の12桁数字タグ（YYYYMMDDHHMM形式）をRenovateで検出し、PR作成まで自動化する。auto merge は無効のまま維持し、人間のレビューを必須とする。

## 背景

- OpenClawイメージは `ghcr.io/boxp/arch/openclaw:YYYYMMDDHHMM` 形式でタグ付けされる
- 現在は ArgoCD Image Updater が `.argocd-source-openclaw.yaml` を通じて kustomize オーバーライドで自動更新している
- `deployment-openclaw.yaml` 内のベースイメージタグは手動更新が必要
- Renovateでこのベースイメージタグの更新PRを自動作成することで追従を半自動化する

## 変更内容

### 1. renovate.json

- `customManagers` に openclaw イメージ検出用の regex ルールを追加
  - 対象ファイル: `argoproj/openclaw/deployment-openclaw.yaml`
  - matchStrings: `image:\s*ghcr\.io/boxp/arch/openclaw:(?<currentValue>\d{12})`
  - datasource: `docker`
  - versioning: `regex:^(?<major>\d{4})(?<minor>\d{2})(?<patch>\d{6})$`
- `packageRules` で以下を設定:
  - kubernetesマネージャーによるOpenClaw検出を無効化（regex側と重複するため）
  - custom.regexマネージャーのOpenClawに対して `automerge: false` を明示

### 2. 運用手順ドキュメント

- `docs/openclaw-image-update-ops.md` にレビュー時の確認ポイントを記載

## CI チェック

- **gitleaks**: シークレット漏洩チェック（設定変更のみのため影響なし）
- **argocd-diff**: `/argoproj/` 配下の変更がないため発動しない

## リスク

- **低**: Renovateがタグの新旧を正しく比較できない可能性
  - 軽減: regex versioning で YYYYMMDDHHMM を major/minor/patch に分割して数値比較
- **低**: 既存の ArgoCD Image Updater との競合
  - 軽減: Renovateは `deployment-openclaw.yaml` のベースタグのみ更新、ArgoCD Image Updater は `.argocd-source-openclaw.yaml` の kustomize オーバーライドを更新するため、役割が異なる

## 確認手順

1. PRがRenovateから正常に作成されることを確認
2. PRの差分で `deployment-openclaw.yaml` のイメージタグのみが変更されていることを確認
3. auto merge が発動しないことを確認
