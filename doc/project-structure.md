# loliceプロジェクト ディレクトリ構造

## 概要
loliceプロジェクトは、オンプレミスのKubernetesクラスタを管理するためのリポジトリです。

## ディレクトリ構造

```
/workspace/lolice/
├── .cursor/                 # Cursor IDE 設定 (開発環境依存)
│   └── rules/
│       └── common.mdc
├── .github/                 # GitHub関連の設定ファイル
│   ├── dependabot.yml       # Dependabotの設定ファイル
│   └── workflows/           # GitHub Actions ワークフロー
│       └── argocd-diff.yaml
├── .gitignore               # Gitの除外ファイル設定
├── .openhands/              # OpenHands AI 設定 (開発環境依存)
│   └── microagents/
│       └── repo.md
├── LICENSE                  # ライセンスファイル
├── README.md                # プロジェクトの概要説明
├── argoproj/                # ArgoCD関連のマニフェストディレクトリ
│   ├── argocd/              # ArgoCDのマニフェスト
│   ├── argocd-image-updater/ # ArgoCD Image Updaterのマニフェスト
│   ├── calico/              # Calicoネットワークプラグインのマニフェスト
│   ├── descheduler/         # Kubernetesのdeschedulerマニフェスト
│   ├── external-secrets-operator/ # External Secrets Operatorのマニフェスト
│   ├── hitohub/             # Hitohubアプリケーションのマニフェスト
│   ├── k8s/                 # Kubernetes関連のマニフェスト
│   ├── k8s-ecr-token-updater/ # AWS ECRトークン更新用のマニフェスト
│   ├── kubernetes-dashboard/ # Kubernetes Dashboardのマニフェスト
│   ├── local-volume-provisioner/ # ローカルボリュームプロビジョナーのマニフェスト
│   ├── longhorn/            # Longhornストレージのマニフェスト
│   ├── openhands/           # OpenHands AI アプリケーションのマニフェスト
│   ├── palserver/           # PALサーバーのマニフェスト
│   ├── prometheus-operator/ # Prometheus Operatorのマニフェスト
│   ├── prometheus-operator-crd/ # Prometheus Operator CRDのマニフェスト
│   ├── reloader/            # Reloaderのマニフェスト
│   └── tidb-operator/       # TiDB Operatorのマニフェスト
├── cluster.jpg              # クラスタの構成図
├── doc/                     # ドキュメントディレクトリ
│   ├── project-spec.md      # プロジェクト仕様書
│   ├── project-structure.md # プロジェクト構造の説明（本ファイル）
│   └── project_doc/         # プロジェクト関連ドキュメント
│       └── add-k8s-manifest-validate-action/
├── k8s/                     # Kubernetesマニフェストディレクトリ
│   └── calico/              # Calicoネットワークプラグインの設定
│       └── README.md
└── renovate.json            # Renovateボットの設定ファイル
```

## 各ディレクトリの説明

### .github/
GitHub Actions ワークフロー (`workflows/`) や Dependabot (`dependabot.yml`) の設定ファイルを含むディレクトリです。

### argoproj/
ArgoCDを使用したGitOpsのためのマニフェストファイルが格納されています。各サブディレクトリには、ArgoCD自身、Calico、Prometheus、OpenHands AIなど、異なるアプリケーションやサービスのマニフェストファイルが含まれています。

### doc/
プロジェクトに関するドキュメントが格納されています。プロジェクト構造 (`project-structure.md`)、プロジェクト仕様 (`project-spec.md`)、その他のドキュメント (`project_doc/`) が含まれています。

### k8s/
Kubernetesクラスタの基本設定に関するマニフェストファイルが格納されています。現在はCalicoネットワークプラグインの設定が含まれています。

## 主要ファイル

### README.md
プロジェクトの概要を説明するファイルです。オンプレミスのKubernetesクラスタについて簡単な説明があります。

### cluster.jpg
実際のクラスタ構成を視覚的に示す画像ファイルです。

### renovate.json
依存関係の自動更新を行うRenovateボットの設定ファイルです。

### LICENSE
プロジェクトのライセンス情報を記載したファイルです。
