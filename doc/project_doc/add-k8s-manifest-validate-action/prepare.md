# argoproj配下のK8sマニフェスト検証GitHub Action追加のための事前調査

## 1. 既存のコードベース構造

### 1.1 argoprojディレクトリの位置づけ

loliceリポジトリでは、`argoproj`ディレクトリがArgoCDで管理されるKubernetesマニフェストを格納するディレクトリとして使用されています。ディレクトリ構造は以下の通りです：

```
lolice/
├── argoproj/            # ArgoCD関連のマニフェストディレクトリ
    ├── argocd/          # ArgoCDのマニフェスト
    ├── argocd-image-updater/ # ArgoCD Image Updaterのマニフェスト
    ├── calico/          # Calicoのマニフェスト
    ├── descheduler/     # Deschedulerのマニフェスト
    ├── external-secrets-operator/ # External Secrets Operatorのマニフェスト
    ├── hitohub/         # Hitohubアプリケーションのマニフェスト
    ├── k8s/             # Kubernetes関連のマニフェスト
    ├── k8s-ecr-token-updater/ # ECRトークンアップデーターのマニフェスト
    ├── kubernetes-dashboard/ # Kubernetes Dashboardのマニフェスト
    ├── local-volume-provisioner/ # ローカルボリュームプロビジョナーのマニフェスト
    ├── longhorn/        # Longhornのマニフェスト
    ├── palserver/       # Palserverのマニフェスト
    ├── prometheus-operator/ # Prometheus Operatorのマニフェスト
    ├── prometheus-operator-crd/ # Prometheus Operator CRDのマニフェスト
    ├── reloader/        # Reloaderのマニフェスト
    └── tidb-operator/   # TiDB Operatorのマニフェスト
```

### 1.2 既存のワークフロー

loliceリポジトリには、既に`argocd-diff.yaml`というGitHub Actionsワークフローが存在しています。このワークフローは、argoprojディレクトリの変更を検知して処理を行います：

```
.github/workflows/
└── argocd-diff.yaml    # ArgoCDのdiffを実行するワークフロー
```

この`argocd-diff.yaml`ワークフローは以下のような機能を持っています：

- PRで`argoproj/**`パスに変更があった場合に実行される
- 変更があったアプリケーションを特定する
- ArgoCDのCLIを使って、各アプリケーションのdiffを取得する
- diffの結果をPRにコメントとして投稿する

このワークフローは変更検出とdiff表示に特化しており、マニフェストの検証は行っていません。そのため、新しいワークフローとして検証機能を追加することで、プルリクエスト時の品質管理を強化できます。

## 2. K8sマニフェスト検証ツール

Kubernetesマニフェストを検証するための主なツールとして以下が挙げられます：

### 2.1 Kubeconform

Kubeconformは高速なKubernetesマニフェスト検証ツールで、以下の特徴があります：

- 高いパフォーマンス（複数のルーチンで並行処理）
- CRD（Custom Resource Definitions）のサポート
- リモートまたはローカルのスキーマ位置を設定可能
- 最新バージョンのKubernetesスキーマをサポート

使用例:
```bash
kustomize build <directory> | kubeconform -verbose
```

### 2.2 Kustomize

Kustomizeは標準的なKubernetesの構成管理ツールで、以下の検証が可能です：

- `kustomize build <directory>`でマニフェストを構築し、構文エラーを検出
- kustomization.yamlファイルの整合性チェック

### 2.3 YAML Lint

YAMLファイルの構文とスタイルを検証するツールです：

- YAMLの構文エラーを検出
- インデント、行の長さなどのスタイル規則を適用

### 2.4 Helm Lint

Helmチャートを検証するためのツールで、特にHelm使用時に有用です：

- チャートの構造と依存関係を検証
- テンプレート構文エラーを検出

## 3. GitHub Actionでの実装アプローチ

### 3.1 基本的なワークフロー構成

新しいワークフローファイル（例：`.github/workflows/validate-k8s-manifests.yaml`）を作成し、以下の内容を含めることが推奨されます：

```yaml
name: Validate Kubernetes Manifests

on:
  pull_request:
    paths:
      - 'argoproj/**'
  push:
    branches: [ main ]
    paths:
      - 'argoproj/**'

jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - name: Setup Kubernetes tools
        uses: yokawasa/action-setup-kube-tools@v0.9.2
        with:
          setup-tools: |
            kubeconform
            kustomize
          kubeconform: '0.5.0'
          kustomize: '4.5.7'
      - name: Validate Manifests
        run: |
          # 変更されたディレクトリを特定
          CHANGED_DIRS=$(find argoproj -type d -name "*" | sort | uniq)
          
          # 各ディレクトリのマニフェストを検証
          for DIR in $CHANGED_DIRS; do
            if [ -f "$DIR/kustomization.yaml" ]; then
              echo "Validating kustomize build for $DIR"
              kustomize build $DIR | kubeconform -verbose
            elif [ -f "$DIR/Chart.yaml" ]; then
              echo "Validating helm chart for $DIR"
              helm lint $DIR
            else
              echo "Checking YAML files in $DIR"
              find $DIR -name "*.yaml" -o -name "*.yml" | xargs -I{} kubeconform -verbose {}
            fi
          done
```

### 3.2 CRDサポートの追加

CustomResourceDefinitionをサポートするために、以下のオプションを追加することが推奨されます：

```bash
kubeconform -schema-location default -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'
```

### 3.3 ArgoCD固有の検証

ArgoCDアプリケーションの定義を検証するために、以下の点に注意する必要があります：

- `Application`および`ApplicationSet`リソースの検証
- パス参照の整合性チェック
- マニフェストのレンダリングとポリシー適用

## 4. 参考資料

1. [Kubeconform GitHub リポジトリ](https://github.com/yannh/kubeconform)
2. [Automating Kubernetes Manifest Validation using Kustomize, Kubeconform, and GitHub Actions](https://medium.com/@dangreenlee_/continually-validate-kubernetes-manifests-using-kubeconform-and-githubactions-ed74ed3ba4ca)
3. [How to validate GitOps manifests](https://developers.redhat.com/articles/2023/10/10/how-validate-gitops-manifests)
4. [GitHub Action example for kustomize and ArgoCD](https://github.com/ferr3ira-gabriel/github-actions-kustomize-argocd-manifests)
5. [ArgoCD Discussion on validation](https://github.com/argoproj/argo-cd/discussions/9753)
