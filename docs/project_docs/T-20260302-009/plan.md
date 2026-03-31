# T-20260302-009: tailscale-operator Workload Identity(beta) 移行計画

## 背景

tailscale-operator の認証方式を現行の OAuth Secret ベースから Workload Identity Federation (WIF, beta) へ移行する。
Phase 1 (PR #494, #497, #500) で導入された OAuth Secret 方式は ExternalSecret + SSM Parameter Store で管理されているが、
WIF 化により長寿命の Secret をクラスタ内に保持する必要がなくなり、セキュリティと保守性が向上する。

## 前提条件の調査結果

### Tailscale WIF の要件
1. **OIDC issuer がパブリックアクセス可能** であること
2. Helm chart **v1.92.0 以上** が必要（v1.80.3 は WIF 未対応）
3. `oauth.clientId` + `oauth.audience` の2値で認証（Secret 不要）
4. Kubernetes の Projected ServiceAccount Token を自動マウント

### lolice クラスタの現状
- **kubeadm ベース**のオンプレミスクラスタ（EKS ではない）
- `--service-account-issuer` 未設定（デフォルト: `https://kubernetes.default.svc.cluster.local`）
- OIDC discovery endpoint **未公開**（API Server がプライベートネットワーク上）
- → **S3 での OIDC discovery ドキュメントホスティングが必要**

## 実装方針

### 変更対象と責務分離

| リポジトリ | 変更内容 | 優先度 |
|-----------|---------|--------|
| **boxp/arch** | S3 OIDC ホスティング (Terraform)、kubeadm config 変更 (Ansible)、WIF credential 作成 (Terraform) | 先行 |
| **boxp/lolice** | Helm chart v1.94.2 アップグレード、values.yaml WIF 対応、ドキュメント | arch 適用後 |

### Phase 1: S3 OIDC Discovery ホスティング (arch)

S3 バケットに OIDC discovery ドキュメントを公開ホスティングする。

**新規ファイル:** `terraform/aws/lolice-k8s-oidc/`
- `backend.tf` - S3 バックエンド + provider 宣言
- `provider.tf` - AWS provider (ap-northeast-1)
- `main.tf` - S3 バケット + パブリック読み取りポリシー
- `variables.tf` - バケット名等の変数
- `outputs.tf` - バケット URL をエクスポート

**ホスティングするドキュメント:**
- `.well-known/openid-configuration` - OIDC discovery メタデータ
- `openid/v1/jwks` - SA 署名用公開鍵 (JWKS)

### Phase 2: kubeadm config 変更 (arch)

**変更ファイル:** `ansible/roles/kubernetes_components/templates/kubeadm-config.yaml.j2`

API Server に以下の extraArgs を追加:
```yaml
apiServer:
  extraArgs:
    service-account-issuer: "{{ k8s_oidc_issuer_url }}"
    service-account-jwks-uri: "{{ k8s_oidc_issuer_url }}/openid/v1/jwks"
    api-audiences: "{{ k8s_oidc_audience }}"
```

**注意:** 複数の `--service-account-issuer` をサポート（K8s 1.22+）するため、
既存トークンの互換性は維持される。

### Phase 3: Tailscale WIF Credential 作成 (arch)

**変更ファイル:** `terraform/tailscale/lolice/wif.tf`

```hcl
resource "tailscale_federated_identity" "k8s_operator" {
  issuer  = var.k8s_oidc_issuer_url
  subject = "system:serviceaccount:tailscale-operator:operator"
  scopes  = ["auth_keys", "devices:core", "services"]
  tags    = ["tag:k8s-operator"]
  depends_on = [tailscale_acl.this]
}
```

エクスポート: `id` (= client_id), `audience`
→ SSM Parameter Store に格納して lolice 側で参照

### Phase 4: Helm Chart アップグレード + WIF 切り替え (lolice)

**変更ファイル:**
- `argoproj/tailscale-operator/application.yaml` - chart version 1.80.3 → 1.94.2
- `argoproj/tailscale-operator/helm/values.yaml` - WIF 認証に切り替え
- `argoproj/tailscale-operator/external-secret.yaml` - WIF 用パラメータに変更
- `argoproj/tailscale-operator/kustomization.yaml` - OIDC discovery ClusterRoleBinding 追加

**values.yaml 変更:**
```yaml
# Before (OAuth Secret)
oauth: {}
oauthSecretVolume:
  secret:
    secretName: tailscale-operator-oauth

# After (Workload Identity)
oauth:
  clientId: "<WIF_CLIENT_ID>"
  audience: "<WIF_AUDIENCE>"
# oauthSecretVolume は削除
```

### Phase 5: ドキュメント

- `docs/project_docs/T-20260302-009/plan.md` - 本計画書
- `docs/runbook/tailscale-operator-workload-identity.md` - 導入手順 + ロールバック手順

## ロールバック手順

### 条件
- WIF 認証で operator が起動しない場合
- Proxy Pod が作成されない場合
- OIDC discovery endpoint に到達できない場合

### 手順
1. `argoproj/tailscale-operator/helm/values.yaml` を OAuth Secret 版に revert
2. `argoproj/tailscale-operator/application.yaml` の chart version を 1.80.3 に revert（任意、1.94.2 は OAuth Secret もサポート）
3. ExternalSecret が SSM から client_id/client_secret を引き続き取得可能（SSM パラメータは削除しない）
4. ArgoCD が自動 Sync で適用

### ロールバック後の確認
- `kubectl -n tailscale-operator get pods` で operator Pod が Running
- `kubectl -n tailscale-operator logs deploy/operator` でエラーなし
- Tailscale admin console で operator ノードが表示される

## 既知リスク

1. **beta 機能**: WIF は Tailscale 側でベータ版。API/動作が変更される可能性
2. **kubeadm config 変更**: API Server 再起動が必要。ダウンタイムはないが注意
3. **OIDC discovery の可用性**: S3 が利用不可の場合、トークン検証が失敗する
4. **SA signing key のローテーション**: key をローテーションした場合、S3 の JWKS も更新が必要

## 段階移行手順 (Canary → 本番)

1. **Step 1**: arch PR をマージし Terraform apply（S3 バケット + WIF credential 作成）
2. **Step 2**: kubeadm config を適用し API Server を再起動
3. **Step 3**: S3 に OIDC discovery ドキュメントをアップロード
4. **Step 4**: lolice PR をマージ（ArgoCD が自動 Sync）
5. **Step 5**: 検証
   - `kubectl -n tailscale-operator get pods` で operator 正常起動
   - `kubectl -n tailscale-operator logs deploy/operator` でエラーなし
   - argocd-diff で Tailscale 経路確認
6. **Step 6**: 問題があればロールバック手順を実行
