# lolice プロジェクト仕様書

## 1. プロジェクト概要

lolice プロジェクトは、個人用オンプレミス Kubernetes クラスターを構築・運用するためのリポジトリです。Kubernetes マニフェストと GitOps ワークフローを使用して、クラスター上のアプリケーションとサービスをコードとして管理します。自宅や小規模環境で Kubernetes の利点を活かしつつ、クラウドプロバイダーに依存せずインフラを管理することを目的としています。

**主な目的**：
- 高可用性の Kubernetes クラスターをオンプレミス環境に構築
- GitOps の原則に従い、インフラをコードとして管理
- 様々なアプリケーションやサービスを簡単にデプロイ・管理できる基盤の提供
- インフラ運用の自動化と効率化の実現
- クラウドサービスへのロックインを避けつつ、モダンなインフラ管理の実現

### 1.1 arch プロジェクトとの関係性

arch プロジェクトと lolice プロジェクトは密接に連携しており、以下の関係性があります：

1. arch プロジェクトは基盤インフラ（クラウドリソース、ネットワーク、IAM など）を Terraform で提供
2. lolice プロジェクトはオンプレミス Kubernetes クラスター上にアプリケーションとサービスをデプロイ
3. arch で定義された外部リソース（Cloudflare トンネルなどの SSM パラメータ）は lolice の ExternalSecrets で参照
4. 両プロジェクトともにバージョン管理され、CI/CD パイプラインで自動化

## 2. 技術スタック

### 2.1 基本コンポーネント

- **言語**: YAML (Kubernetes マニフェスト)
- **ツール**:
  - Kubernetes: コンテナオーケストレーション基盤
  - kubectl: Kubernetes クラスター管理 CLI
  - kustomize: Kubernetes マニフェスト管理
  - Argo CD: GitOps のための継続的デリバリープラットフォーム
  - GitHub Actions: CI/CD パイプライン

### 2.2 インフラストラクチャコンポーネント

- **ノード管理**:
  - kubeadm: クラスターブートストラップツール
  - Calico: ネットワークプラグイン（CNI）
  - Longhorn: 分散永続ストレージ

- **モニタリングと可視化**:
  - Prometheus: メトリクス収集
  - Grafana: メトリクス可視化
  - Kubernetes Dashboard: クラスター管理用 WebUI

### 2.3 補助コンポーネント

- **シークレット管理**:
  - External Secrets Operator: 外部シークレットの同期
  
- **クラスター最適化**:
  - Descheduler: ノード間のワークロードバランシング
  - Reloader: ConfigMap や Secret の変更時に自動リロード
  
- **データベース**:
  - TiDB Operator: 分散 SQL データベース管理
  
- **CI/CD**:
  - ArgoCD Image Updater: コンテナイメージの自動更新

## 3. ディレクトリ構造

```
lolice/
├── .github/                  # GitHub Actions ワークフロー定義
│   └── workflows/            # CI/CD パイプライン設定
│       ├── argocd-diff.yaml  # ArgoCD diff 用ワークフロー
│       └── ...
├── docs/                     # プロジェクトドキュメント
│   └── project-spec.md       # プロジェクト仕様書
├── argoproj/                 # Argo CD アプリケーション定義
│   ├── argocd/               # Argo CD 自体の設定
│   │   ├── base/             # 基本リソース
│   │   │   ├── cloudflared-api.yaml        # Cloudflared Deployment
│   │   │   ├── external-secrets.yaml       # ExternalSecret 定義
│   │   │   ├── github-actions-rbac.yaml    # GitHub Actions 用 RBAC
│   │   │   ├── network-policy.yaml         # ネットワークポリシー
│   │   │   └── ...
│   │   ├── overlays/         # カスタマイズオーバーレイ
│   │   └── kustomization.yaml # Kustomize 設定
│   ├── argocd-image-updater/ # ArgoCD Image Updater 設定
│   ├── prometheus-operator/  # Prometheus Operator 設定
│   ├── longhorn/            # Longhorn ストレージ設定
│   ├── calico/              # Calico ネットワーク設定
│   ├── descheduler/         # Descheduler 設定
│   ├── kubernetes-dashboard/ # Kubernetes Dashboard 設定
│   ├── tidb-operator/       # TiDB Operator 設定
│   ├── external-secrets-operator/ # External Secrets Operator 設定
│   ├── reloader/            # Reloader 設定
│   └── ...
├── k8s/                      # クラスターセットアップ用リソース
│   └── calico/               # Calico ネットワーク初期設定
└── ...
```

## 4. 主要コンポーネント

### 4.1 Argo CD Applications

lolice プロジェクトでは、各サービスやアプリケーションが Argo CD Application リソースとして定義されています。主なアプリケーションには：

- **argocd**: Argo CD 自体の設定
- **external-secrets-operator**: ExternalSecrets Operator の設定
- **prometheus-operator**: モニタリングシステム
- **longhorn**: 分散ストレージシステム
- **calico**: ネットワークポリシーとルーティング
- **kubernetes-dashboard**: クラスター可視化と管理
- **tidb-operator**: 分散データベース管理
- その他のアプリケーション

### 4.2 Kustomize による設定管理

Kustomize を使用して、異なる環境や要件に対応する構成管理を行っています：

- **base**: 基本的なリソース定義
- **overlays**: 環境ごとの上書き設定
- **kustomization.yaml**: リソースとパッチの定義

例：
```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - base/deployment.yaml
  - base/service.yaml
  - base/configmap.yaml
patchesStrategicMerge:
  - overlays/deployment-patch.yaml
```

### 4.3 External Secrets

AWS Systems Manager Parameter Store から Kubernetes Secrets へシークレットを同期するための ExternalSecrets が定義されています：

```yaml
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: argocd-api-tunnel-es
  namespace: argocd
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: parameterstore
    kind: ClusterSecretStore
  target:
    name: argocd-api-tunnel-credentials
    creationPolicy: Owner
  data:
  - secretKey: tunnel-token
    remoteRef:
      key: argocd-api-tunnel-token
```

### 4.4 Network Policies

セキュリティを強化するため、Kubernetes NetworkPolicy リソースを使用したポッド間通信の制限が定義されています。

例：
```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: argocd-server-network-policy
  namespace: argocd
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/name: argocd-server
  policyTypes:
  - Ingress
  ingress:
  - from:
    - podSelector:
        matchLabels:
          app: cloudflared
    ports:
    - protocol: TCP
      port: 8080
```