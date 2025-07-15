# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## プロジェクト概要

loliceは、ArgoCDを使用して個人用オンプレミスKubernetesクラスターを管理するためのGitOpsリポジトリです。様々なアプリケーションとインフラストラクチャコンポーネントのデプロイと管理のためのYAMLマニフェストが含まれています。

## 主要技術

- **Kubernetes**: コンテナオーケストレーションプラットフォーム
- **ArgoCD**: GitOps継続的デプロイメントツール
- **Kustomize**: Kubernetesマニフェスト管理
- **External Secrets Operator**: AWS SSM Parameter Storeからシークレットを同期
- **Calico**: ネットワーキング用コンテナネットワークインターフェース（CNI）
- **Longhorn**: 分散ストレージシステム
- **Prometheus & Grafana**: モニタリングと可視化

## リポジトリ構造

```
/argoproj/          # ArgoCDアプリケーション（全デプロイメント）
  /argocd/          # ArgoCD自体
  /calico/          # ネットワークCNI
  /longhorn/        # 分散ストレージ
  /prometheus-operator/  # モニタリング
  /external-secrets-operator/  # シークレット管理
  /tidb-operator/   # 分散SQLデータベース
  /openhands/       # AIアプリケーション
  /hitohub/         # アプリケーション
  # ... その他のアプリケーション

/k8s/               # 基本的なKubernetesセットアップ
/doc/               # ドキュメント
/.github/workflows/ # CI/CDパイプライン
```

## よく使うコマンド

これはYAMLマニフェストを使用したGitOpsリポジトリのため、従来のビルド/テストコマンドはありません。以下は便利なコマンドです：

### Kubernetesマニフェストの検証
```bash
# 特定のマニフェストを検証
kubectl --dry-run=client apply -f <manifest.yaml>

# kustomize出力を検証
kustomize build argoproj/<app-name>/ | kubectl --dry-run=client apply -f -
```

### ArgoCDアプリケーションステータスの確認
```bash
# 全ArgoCDアプリケーションをリスト
kubectl get applications -n argocd

# 特定のアプリケーションステータスを確認
kubectl describe application <app-name> -n argocd
```

### Kustomizeの操作
```bash
# 最終的なマニフェストをビルドして表示
kustomize build argoproj/<app-name>/

# 特定のオーバーレイでビルド
kustomize build argoproj/<app-name>/overlays/<env>/
```

## アーキテクチャとパターン

### GitOpsワークフロー
1. すべての変更はこのリポジトリのYAMLマニフェストに対して行われる
2. ArgoCDがリポジトリの変更を監視
3. 変更が検出されると、ArgoCDが自動的にクラスターに同期
4. GitHub ActionsがPRでdiffチェックを実行し、何が変更されるかを表示

### アプリケーション構造
`/argoproj/`配下の各アプリケーションは通常以下のパターンに従います：
```
<app-name>/
├── application.yaml     # ArgoCDアプリケーション定義
├── base/               # 基本Kubernetesリソース
│   ├── deployment.yaml
│   ├── service.yaml
│   ├── configmap.yaml
│   └── external-secrets.yaml
├── overlays/           # 環境固有のカスタマイズ
└── kustomization.yaml  # Kustomize設定
```

### External Secretsパターン
シークレットはExternal Secrets Operatorを介して管理され、AWS SSMから同期されます：
```yaml
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: <secret-name>
  namespace: <namespace>
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: parameterstore
    kind: ClusterSecretStore
  target:
    name: <k8s-secret-name>
  data:
  - secretKey: <key-in-secret>
    remoteRef:
      key: <ssm-parameter-name>
```

### ネットワークポリシー
Pod間通信を制限するNetworkPolicyリソースを通じてセキュリティが強化されています。新しいサービスを追加する際は常にネットワークポリシーを考慮してください。

## CI/CDパイプライン

リポジトリはCI/CDにGitHub Actionsを使用：
- **argocd-diff.yaml**: `/argoproj/`に影響するPRで実行され、ArgoCD diffを表示
- ArgoCDへの安全なAPIアクセスにCloudflare Accessを使用
- diff出力を自動的にPRにコメント

## 重要なガイドライン

1. **常にKustomizeを使用** - 環境間の差異を管理するため
2. **シークレットを直接コミットしない** - 代わりにExternal Secretsを使用
3. **マニフェストの妥当性をテスト** - コミット前にkubectl dry-runを使用
4. **既存のパターンに従う** - 参考として類似のアプリケーションを確認
5. **ドキュメントを更新** - 新しいアプリケーション追加時は`/doc/`を更新
6. **ネットワークポリシーを考慮** - 新しいサービス追加時

## archプロジェクトとの関係

loliceプロジェクトはarchプロジェクトと連携して動作します：
- **arch**: Terraformを介してクラウドインフラ（AWSリソース、IAM、ネットワーキング）を管理
- **lolice**: GitOpsを介してオンプレミスKubernetesアプリケーションを管理
- archで定義された外部リソース（SSMのCloudflareトンネルトークンなど）はloliceのExternalSecretsで参照される

## 必読ドキュメント

変更を行う前に、必ず以下を読んでください：
- `/doc/project-spec.md` - 詳細なプロジェクト仕様
- `/doc/project-structure.md` - ディレクトリ構造の説明