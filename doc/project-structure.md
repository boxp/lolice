# loliceプロジェクト ディレクトリ構造

## 概要
loliceプロジェクトは、オンプレミスのKubernetesクラスタを管理するためのリポジトリです。

## ディレクトリ構造

```
/home/boxp/program/misc/lolice/
├── .git/                      # Gitリポジトリの設定ファイル
├── .github/                   # GitHub関連の設定ファイル
│   └── dependabot.yml         # Dependabotの設定ファイル
├── .gitignore                 # Gitの除外ファイル設定
├── LICENSE                    # ライセンスファイル
├── README.md                  # プロジェクトの概要説明
├── argoproj/                  # ArgoCD関連のマニフェストディレクトリ
│   ├── argocd/                # ArgoCDのマニフェスト
│   ├── argocd-image-updater/  # ArgoCD Image Updaterのマニフェスト
│   ├── calico/                # Calicoネットワークプラグインのマニフェスト
│   ├── descheduler/           # Kubernetesのdeschedulerマニフェスト
│   ├── external-secrets-operator/ # External Secrets Operatorのマニフェスト
│   ├── hitohub/               # Hitohubアプリケーションのマニフェスト
│   ├── k8s/                   # Kubernetes関連のマニフェスト
│   ├── k8s-ecr-token-updater/ # AWS ECRトークン更新用のマニフェスト
│   ├── kubernetes-dashboard/  # Kubernetes Dashboardのマニフェスト
│   ├── local-volume-provisioner/ # ローカルボリュームプロビジョナーのマニフェスト
│   ├── longhorn/              # Longhornストレージのマニフェスト
│   ├── obsidian-self-live-sync/ # Obsidian同期アプリのマニフェスト
│   ├── palserver/             # PALサーバーのマニフェスト
│   ├── prometheus-operator/   # Prometheus Operatorのマニフェスト
│   ├── prometheus-operator-crd/ # Prometheus Operator CRDのマニフェスト
│   ├── reloader/              # Reloaderのマニフェスト
│   └── tidb-operator/         # TiDB Operatorのマニフェスト
├── cluster.jpg                # クラスタの構成図
├── doc/                       # ドキュメントディレクトリ
│   └── project-structure.md   # プロジェクト構造の説明（本ファイル）
├── k8s/                       # Kubernetesマニフェストディレクトリ
│   └── calico/                # Calicoネットワークプラグインの設定
└── renovate.json              # Renovateボットの設定ファイル
```

## 各ディレクトリの説明

### .github/
GitHubワークフローやアクションなどの設定ファイルを含むディレクトリです。Dependabotの設定も含まれています。

### argoproj/
ArgoCDを使用したGitOpsのためのマニフェストファイルが格納されています。各サブディレクトリには異なるアプリケーションやサービスのマニフェストファイルが含まれています。

### doc/
プロジェクトに関するドキュメントが格納されています。現在は本ファイル（project-structure.md）のみが含まれています。

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
