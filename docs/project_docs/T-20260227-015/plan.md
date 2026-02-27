# T-20260227-015: Tailscale Subnet Router Kubernetes マニフェスト追加

## 概要

`boxp/arch` で作成済みの Tailscale Terraform リソース（auth key、ACL、WIF）を利用するため、`boxp/lolice` に Tailscale subnet router の Kubernetes マニフェストを追加する。

## 背景

- `boxp/arch` の `terraform/tailscale/lolice/` で以下が作成済み:
  - `tailscale_tailnet_key.subnet_router`: 事前承認済み auth key（`tag:subnet-router`）
  - `aws_ssm_parameter.subnet_router_auth_key`: SSM パラメータ `/lolice/tailscale/subnet-router-auth-key`
  - `tailscale_acl.this`: `tag:ci` → ArgoCD API アクセス許可、`tag:subnet-router` の subnet route 自動承認
  - `tailscale_federated_identity.github_actions_argocd_diff`: GitHub Actions WIF

- lolice 側で必要なのは、subnet router Pod のデプロイのみ

## 設計

### ディレクトリ構成

```
argoproj/tailscale-subnet-router/
├── application.yaml         # ArgoCD Application 定義
├── namespace.yaml           # tailscale namespace
├── deployment.yaml          # Tailscale subnet router Deployment
├── external-secret.yaml     # ExternalSecret (SSM → K8s Secret)
├── serviceaccount.yaml      # ServiceAccount
├── role.yaml                # Role (tailscale state 保存用)
├── rolebinding.yaml         # RoleBinding
├── networkpolicy.yaml       # Calico NetworkPolicy
└── kustomization.yaml       # Kustomize 設定
```

### 設計方針

1. **既存パターンに準拠**: bastion アプリケーションの構成パターンを踏襲
2. **Userspace モード**: `TS_USERSPACE=true` により NET_ADMIN capability 不要、Namespace は `baseline` PSS で十分
3. **ExternalSecrets**: SSM `/lolice/tailscale/subnet-router-auth-key` から auth key を取得
4. **NetworkPolicy (Calico)**: DNS + K8s API + Tailscale coordination + DERP + ArgoCD Service への通信のみ許可
5. **ConfigMap 不要**: TS_ROUTES 等は環境変数で直接指定（シンプルさ優先）
6. **State 永続化**: `TS_KUBE_SECRET` で K8s Secret に state を保存（Pod 再起動時の再認証を回避）

### リソース詳細

#### 1. Namespace (`tailscale`)
- Pod Security Standards: `baseline`（userspace モードでは NET_ADMIN 不要）

#### 2. Deployment
- イメージ: `ghcr.io/tailscale/tailscale:v1.80.3`（固定タグ）
- replicas: 1
- 環境変数:
  - `TS_AUTHKEY`: ExternalSecret 経由
  - `TS_EXTRA_ARGS`: `--advertise-tags=tag:subnet-router`
  - `TS_USERSPACE`: `true`（userspace networking — NET_ADMIN 不要）
  - `TS_KUBE_SECRET`: `tailscale-state`（Tailscale state を K8s Secret に保存）
  - `TS_ACCEPT_DNS`: `false`（クラスター DNS を優先）
  - `TS_ROUTES`: ArgoCD Service ClusterIP/32（初回デプロイ後に確認して設定）
- securityContext:
  - runAsNonRoot: true
  - runAsUser: 1000
  - runAsGroup: 1000
  - allowPrivilegeEscalation: false
  - capabilities: drop ALL
  - readOnlyRootFilesystem: true
- automountServiceAccountToken: true（TS_KUBE_SECRET 利用に必要）
- volumes: tmpfs（/tmp）
- resources: requests 50m CPU / 64Mi mem、limits 100m CPU / 128Mi mem
- readinessProbe / livenessProbe: 未設定（tailscale コンテナに標準的なヘルスチェックエンドポイントがないため）

#### 3. ExternalSecret
- SSM key: `/lolice/tailscale/subnet-router-auth-key`
- K8s Secret name: `tailscale-auth`
- Secret key: `TS_AUTHKEY`

#### 4. ServiceAccount + RBAC
- tailscale が state を K8s Secret として保存するために必要
- Role: secrets に対する `create`, `get`, `update`, `patch` 権限（resourceNames: `tailscale-state`）
- automountServiceAccountToken: true

#### 5. NetworkPolicy (Calico `projectcalico.org/v3`)
- Egress:
  - DNS (kube-system/kube-dns:53 TCP/UDP)
  - Kubernetes API (kubernetes.default.svc:443/TCP) — TS_KUBE_SECRET による state 永続化
  - Tailscale coordination server (外部 HTTPS 443/TCP)
  - Tailscale DERP relay (外部 3478/UDP — STUN)
  - ArgoCD Service (argocd namespace:443/TCP, 8080/TCP)
- Ingress: 全拒否（subnet router は受信接続不要。WireGuard トラフィックは tailscale userspace が egress 方向で確立した接続で処理）

#### 6. ArgoCD Application
```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: tailscale-subnet-router
  namespace: argocd
spec:
  destination:
    namespace: tailscale
    server: https://kubernetes.default.svc
  project: default
  sources:
    - path: argoproj/tailscale-subnet-router
      repoURL: https://github.com/boxp/lolice
      targetRevision: main
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
```

### kustomization.yaml 更新

`argoproj/kustomization.yaml` に以下を追加:
```yaml
- tailscale-subnet-router/application.yaml
```

## TS_ROUTES について

subnet router がアドバタイズするルートは ArgoCD Service の ClusterIP に依存する。

**ClusterIP 固定化戦略**: ArgoCD Service の ClusterIP は Service リソースが削除・再作成されない限り変更されない。PoC フェーズでは手動確認で十分だが、将来的には ArgoCD Service の `spec.clusterIP` を固定値で宣言することでドリフトを防止可能。

**初期デプロイ手順**:
1. `TS_ROUTES` を空で初回デプロイ（tailnet 接続のみ成立）
2. ArgoCD Service の ClusterIP を確認:
   ```bash
   kubectl get svc argocd-server -n argocd -o jsonpath='{.spec.clusterIP}'
   ```
3. `deployment.yaml` の `TS_ROUTES` を `<ClusterIP>/32` に更新してコミット
4. `boxp/arch` 側の `variables.tf` で `argocd_service_cluster_ip` を同じ値に設定

## 適用手順

1. PR をマージし ArgoCD が自動 sync
2. tailscale-subnet-router Pod が起動し tailnet に接続
3. ArgoCD Service の ClusterIP を確認
4. `deployment.yaml` の `TS_ROUTES` を更新してコミット（2回目の sync）
5. `boxp/arch` 側の `variables.tf` で `argocd_service_cluster_ip` を設定

## ロールバック手順

1. `argoproj/kustomization.yaml` から `tailscale-subnet-router/application.yaml` を削除してコミット
2. ArgoCD が自動 sync → Application が削除 → 全関連リソースが cascade 削除
3. 代替（即時）: `argocd app delete tailscale-subnet-router --cascade` で即時削除
4. tailscale-state Secret は namespace 削除時に自動削除。tailnet 上のデバイスは ephemeral key のため自動消去

## 非スコープ

- GitHub Actions ワークフロー (`argocd-diff.yaml`) の Tailscale 対応（別タスク）
- Cloudflare 経路の変更・削除
- 本番全面切替
