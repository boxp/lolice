# T-20260218-016: Secret漏えい予防の最小ガード導入 - 実装計画

## 現状分析

### boxp/lolice
- **種別**: GitOps (K8s YAML + Kustomize + ArgoCD)
- **既存CI**: argocd-diff.yaml, claude.yaml, dependabot.yml
- **既存セキュリティツール**: なし（Dependabot Docker更新のみ）
- **.gitignore**: Terraform向けのみ（.terraform, tfstate, tfvars等）
- **pre-commit / gitleaks**: 未設定

### boxp/arch
- **種別**: IaC (Terraform + Ansible + OPA)
- **既存CI**: test.yaml, apply.yaml, plan-ansible.yml 他多数
- **既存セキュリティツール**: Trivy, conftest/OPA, tflint, ghalint, actionlint, shellcheck
- **.gitignore**: Terraform + tfaction向け
- **pre-commit / gitleaks**: 未設定

## 導入方針（壊れにくい順序）

### 段階1: gitleaks CI GitHub Actions ワークフロー追加（最優先）
- 各リポジトリに `.github/workflows/gitleaks.yaml` を追加
- PR時とpush時に自動スキャン
- 既存CIに影響なし（独立ワークフロー）

### 段階2: .gitleaks.toml 設定ファイル
- 各リポジトリに `.gitleaks.toml` を追加
- 既知の誤検知のallowlist設定

### 段階3: .gitignore 強化
- 一般的なシークレットファイルパターンを追記
  - `.env`, `.env.*`
  - `*.pem`, `*.key`, `*.p12`, `*.pfx`
  - `credentials.json`, `service-account*.json`
  - `*.kubeconfig`
  - `secrets.yaml` (非K8sリソースとしての生ファイル)

### 段階4: pre-commit 設定（ローカル開発支援）
- `.pre-commit-config.yaml` を追加
- gitleaks pre-commit hook設定
- READMEへの記載は非スコープ（既存ドキュメント構造を尊重）

## 各リポジトリの実装内容

### 共通ファイル
1. `.github/workflows/gitleaks.yaml` - CI gitleaksスキャン
2. `.gitleaks.toml` - gitleaks設定
3. `.pre-commit-config.yaml` - pre-commit設定
4. `.gitignore` への追記 - シークレットファイルパターン

### gitleaks.yaml ワークフロー仕様
- トリガー: `pull_request`, `push` (main/master)
- gitleaks/gitleaks-action を使用
- `--no-git` ではなく `protect` モード（差分チェック）

## リスク評価
- **低リスク**: 独立ワークフロー追加のため既存CIに影響なし
- **低リスク**: .gitignore追記は既存パターンに追加するのみ
- **低リスク**: pre-commit設定はオプトイン（手動install必要）
- **注意**: gitleaks CIが既存コミットの誤検知でfailする可能性 → allowlistで対処
