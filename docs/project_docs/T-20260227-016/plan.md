# T-20260227-016: Tailscale Kubernetes Operator PoC

## 概要

IPハードコード（TS_ROUTES）依存を避けるため、Tailscale Kubernetes Operator を使った「tailnet 内プライベート公開」方式の PoC を作成する。

> **重要**: 本 PoC で公開されるサービスは **tailnet 限定** であり、パブリックインターネットには公開されません。tailnet に参加しているデバイスからのみアクセス可能です。

## 背景

### 既存方式（Subnet Router / PR #493）の課題

PR #493 で導入された subnet router 方式では：
- `TS_ROUTES` に Service の ClusterIP を `/32` で指定する必要がある
- ClusterIP はデプロイ後に動的に決まるため、2段階デプロイが必要
- サービス追加のたびに `TS_ROUTES` の更新が必要
- IP ベースの経路管理は運用負荷が高い

### Operator 方式のアプローチ

Tailscale Kubernetes Operator は：
- Service に `tailscale.com/expose: "true"` Annotation を付与するだけで tailnet に公開
- 各サービスが tailnet 上で一意のホスト名を持つ（DNS ベース）
- IP ハードコード不要
- サービス追加は Annotation のみで完結

## 設計

### アーキテクチャ

```
tailnet デバイス
  ↓ WireGuard (tailnet 限定)
Tailscale Operator (proxy Pod 自動生成)
  ↓ ClusterIP
対象 Service (e.g. argocd-server)
```

### ディレクトリ構成

```
argoproj/tailscale-operator/
├── application.yaml       # ArgoCD Application (Helm + Kustomize multi-source)
├── helm/
│   └── values.yaml        # Helm chart カスタム values
├── namespace.yaml          # tailscale-operator namespace
├── external-secret.yaml   # OAuth credentials (SSM → K8s Secret)
├── networkpolicy.yaml     # Calico NetworkPolicy
└── kustomization.yaml     # Kustomize 設定
```

### コンポーネント詳細

#### 1. Helm Chart

- Chart: `tailscale-operator` from `https://pkgs.tailscale.com/helmcharts`
- Version: `1.80.3` (PR #493 の tailscale イメージと揃える)
- OAuth credentials は ExternalSecret で管理

#### 2. ExternalSecret

- SSM パラメータ:
  - `/lolice/tailscale/operator-oauth-client-id`
  - `/lolice/tailscale/operator-oauth-client-secret`
- K8s Secret: `tailscale-operator-oauth`
- **前提**: `boxp/arch` 側で Tailscale OAuth Client の Terraform リソース作成が必要

#### 3. NetworkPolicy (Calico v3)

- Egress:
  - DNS (kube-system/kube-dns:53)
  - Kubernetes API (10.96.0.1/32:443) — リソース監視
  - Tailscale coordination server (443/TCP)
  - DERP relay (3478/UDP)
  - 対象 Service への通信（namespace 横断）
- Ingress:
  - Webhook (443/TCP) — K8s API Server からの Admission Webhook

#### 4. ArgoCD Application

- Multi-source pattern (grafana-alloy と同等):
  - Source 1: lolice リポジトリ参照 (`ref: lolice`)
  - Source 2: Helm chart (`tailscale-operator`)
  - Source 3: Kustomize パッチ（namespace, external-secret, networkpolicy）
- `ServerSideApply=true` (CRD 含むため)

### サービス公開方法（PoC 適用後）

対象サービスの Service リソースに Annotation を追加：

```yaml
metadata:
  annotations:
    tailscale.com/expose: "true"
    tailscale.com/hostname: "lolice-argocd"  # tailnet 上のホスト名
```

> **注意**: この Annotation 追加は本 PoC のスコープ外。Operator のインストールまでが PoC 範囲。

## 公開範囲について

**本 PoC で公開されるサービスは tailnet 限定です。**

- Tailscale Kubernetes Operator は、対象サービスを **tailnet 内のデバイスからのみ** アクセス可能にします
- パブリックインターネットには一切公開されません
- アクセスには tailnet への参加（Tailscale クライアントのインストールと認証）が必要です
- Tailscale ACL により、さらに細かいアクセス制御が可能です

## 既存方式との比較

| 項目 | Subnet Router (PR #493) | Operator (本 PoC) |
|------|------------------------|-------------------|
| IP ハードコード | 必要 (TS_ROUTES に ClusterIP/32) | 不要 |
| サービス追加 | TS_ROUTES 更新 + 再デプロイ | Annotation 追加のみ |
| DNS 名 | なし (IP 直指定) | あり (tailnet hostname) |
| デプロイ手順 | 2段階 (IP 確認後に再デプロイ) | 1段階 |
| CRD | なし | あり (Operator が管理) |
| リソース消費 | 低 (単一 Pod) | 中 (Operator + Service 毎に Proxy Pod) |
| K8s 権限 | 最小 (Secret のみ) | 広い (ClusterRole: Services, Endpoints 等) |
| Helm 依存 | なし | あり |
| 成熟度 | 安定 (シンプル) | Tailscale 公式サポート |
| tailnet 公開 | ルーティング経由 | Service 単位で個別公開 |

### メリット（Operator 方式）

1. **IP 管理不要**: ClusterIP のハードコードが不要
2. **DNS ベース**: tailnet 上で名前解決可能
3. **宣言的**: Service Annotation で公開/非公開を制御
4. **スケーラブル**: サービス追加が容易

### 制約（Operator 方式）

1. **CRD 依存**: Operator の CRD がクラスターに追加される
2. **ClusterRole 必要**: Operator が広い K8s 権限を必要とする
3. **リソース消費**: Operator Pod + 公開サービス毎に Proxy Pod
4. **OAuth Client 必要**: Auth Key ではなく OAuth Client Credentials が必要（`boxp/arch` 側での Terraform 追加作業）
5. **Helm 管理**: Chart バージョンの更新管理が発生

### 移行計画

1. **Phase 1 (本 PoC)**: Operator のインストールのみ。既存 Cloudflare 経路はそのまま
2. **Phase 2**: ArgoCD Service に Annotation を追加し、tailnet 経由でのアクセスを確認
3. **Phase 3**: GitHub Actions の argocd-diff を Tailscale 経由に切り替え
4. **Phase 4**: Cloudflare 経路の段階的撤去（十分な検証後）

### ロールバック手順

1. `argoproj/kustomization.yaml` から `tailscale-operator/application.yaml` を削除してコミット
2. ArgoCD が自動 sync → Application 削除 → 全関連リソース cascade 削除
3. CRD はクラスターに残るため、必要に応じて手動削除:
   ```bash
   kubectl delete crd connectors.tailscale.com proxyclasses.tailscale.com dnsconfigs.tailscale.com proxygroups.tailscale.com
   ```
4. 代替（即時）: `argocd app delete tailscale-operator --cascade`

## 前提条件（`boxp/arch` 側で必要な作業）

- Tailscale OAuth Client の作成（Terraform）
- OAuth Client ID / Secret を SSM に保存:
  - `/lolice/tailscale/operator-oauth-client-id`
  - `/lolice/tailscale/operator-oauth-client-secret`
- Tailscale ACL に `tag:k8s-operator` の権限設定

## 非スコープ

- 既存 Cloudflare 経路の即時撤去
- 本番完全切替（PoC 範囲に限定）
- GitHub Actions ワークフローの Tailscale 対応
- ArgoCD Service への Annotation 追加（Phase 2）
