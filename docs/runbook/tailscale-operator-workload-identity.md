# Runbook: tailscale-operator Workload Identity Federation (beta) 移行

## 概要

tailscale-operator の認証を OAuth Secret → Workload Identity Federation (WIF) へ移行する手順書。
WIF により、長寿命の `client_secret` をクラスタ内に保持する必要がなくなる。

## 前提条件

- boxp/arch リポジトリの Terraform が適用済み（S3 OIDC バケット + WIF credential）
- kubeadm control plane ノードへの SSH アクセス
- `kubectl` で lolice クラスタに接続可能
- AWS CLI (`aws`) が設定済み

## ステータス

- **フェーズ**: beta
- **Helm chart**: v1.94.2 (WIF サポート: v1.92.0+)
- **Tailscale provider**: v0.28.0

---

## 導入手順

### Step 1: arch Terraform を apply (S3 + WIF credential)

```bash
cd terraform/tailscale/lolice
terraform plan
terraform apply
```

出力を控える:
```bash
terraform output k8s_operator_wif_client_id
terraform output k8s_operator_wif_audience
terraform output k8s_oidc_issuer_url
```

### Step 2: kubeadm API Server に OIDC issuer を設定

1. Ansible の group_vars で `k8s_oidc_issuer_url` を設定:

```yaml
# group_vars/control_plane.yml (または host_vars)
k8s_oidc_issuer_url: "https://lolice-k8s-oidc.s3.ap-northeast-1.amazonaws.com"
```

2. Ansible playbook を実行:

```bash
ansible-playbook -i inventory site.yml --tags kubeadm
```

3. kubeadm upgrade を実行（各 control plane ノードで）:

```bash
sudo kubeadm upgrade apply --config /etc/kubernetes/kubeadm-config.yaml
```

### Step 3: OIDC Discovery ドキュメントを S3 にアップロード

control plane ノードで JWKS を取得:
```bash
kubectl get --raw /openid/v1/jwks > /tmp/jwks.json
```

S3 にアップロード:
```bash
aws s3 cp /tmp/jwks.json s3://lolice-k8s-oidc/openid/v1/jwks \
  --content-type application/json
```

動作確認:
```bash
curl -s https://lolice-k8s-oidc.s3.ap-northeast-1.amazonaws.com/.well-known/openid-configuration | jq .
curl -s https://lolice-k8s-oidc.s3.ap-northeast-1.amazonaws.com/openid/v1/jwks | jq .keys[0].kid
```

### Step 4: lolice values.yaml のプレースホルダを更新

`argoproj/tailscale-operator/helm/values.yaml` の以下を更新:

```yaml
oauth:
  clientId: "<terraform output k8s_operator_wif_client_id の値>"
  audience: "<terraform output k8s_operator_wif_audience の値>"
```

コミットして push:
```bash
git add argoproj/tailscale-operator/helm/values.yaml
git commit -m "feat: set real WIF credentials for tailscale-operator"
git push origin main
```

### Step 5: 検証

ArgoCD が自動 sync するのを待つか、手動 sync:
```bash
argocd app sync tailscale-operator
```

確認事項:
```bash
# Operator Pod が Running
kubectl -n tailscale-operator get pods

# ログにエラーなし
kubectl -n tailscale-operator logs deploy/operator --tail=50

# Projected volume がマウントされている
kubectl -n tailscale-operator get deploy/operator -o jsonpath='{.spec.template.spec.volumes}' | jq .

# CLIENT_ID 環境変数が設定されている（CLIENT_SECRET_FILE は存在しないこと）
kubectl -n tailscale-operator exec deploy/operator -- env | grep CLIENT

# Tailscale admin console で operator ノードが tag:k8s-operator で表示
```

argocd-diff で Tailscale 経路確認:
```bash
# GitHub Actions workflow で argocd-diff を実行
# ログに "Auth path: tailscale" が表示されること
```

---

## ロールバック手順

### 条件

以下のいずれかに該当する場合はロールバックを検討:
- WIF 認証で operator が起動しない (CrashLoopBackOff)
- Tailscale admin console に operator ノードが表示されない
- Proxy Pod が作成されない
- OIDC discovery endpoint に到達できない

### 手順

1. **values.yaml を OAuth Secret 版に revert**:

```yaml
# oauth.clientId/audience を削除し、以下に置き換え:
oauth: {}

oauthSecretVolume:
  secret:
    secretName: tailscale-operator-oauth
```

2. **コミットして push** (ArgoCD が自動 sync):

```bash
git add argoproj/tailscale-operator/helm/values.yaml
git commit -m "revert: rollback tailscale-operator to OAuth Secret mode"
git push origin main
```

3. **確認**:

```bash
kubectl -n tailscale-operator get pods -w
kubectl -n tailscale-operator logs deploy/operator --tail=50
```

### 注意事項

- Helm chart version (1.94.2) は revert 不要。1.94.2 は OAuth Secret モードもサポート
- ExternalSecret は削除していないため、SSM から client_id/client_secret を引き続き取得可能
- SSM パラメータ (`operator-oauth-client-id`, `operator-oauth-client-secret`) は削除していない
- kubeadm の `--service-account-issuer` 変更は revert 不要（WIF 不使用時は無害）

---

## トラブルシューティング

### Operator が起動しない

```bash
kubectl -n tailscale-operator describe pod -l app=operator
kubectl -n tailscale-operator logs deploy/operator --previous
```

よくある原因:
- `oauth.clientId` / `oauth.audience` のプレースホルダが更新されていない
- S3 OIDC バケットが存在しない
- JWKS が S3 にアップロードされていない

### トークン交換エラー

Tailscale admin console → Trust credentials → 該当の federated identity → View/Edit でエラー詳細を確認。

API レスポンス: `{ "message": "Unauthorized. Visit [link] for details" }`
→ admin console でエラーの詳細を確認できる。

### OIDC discovery に到達できない

```bash
curl -v https://lolice-k8s-oidc.s3.ap-northeast-1.amazonaws.com/.well-known/openid-configuration
```

確認事項:
- S3 バケットの public access block 設定
- S3 バケットポリシー
- OIDC ドキュメントの Content-Type (`application/json`)

---

## SA signing key のローテーション

kubeadm の SA signing key をローテーションした場合、JWKS も更新が必要:

```bash
# 新しい JWKS を取得
kubectl get --raw /openid/v1/jwks > /tmp/jwks.json

# S3 にアップロード
aws s3 cp /tmp/jwks.json s3://lolice-k8s-oidc/openid/v1/jwks \
  --content-type application/json

# Terraform の変数も更新（次回 apply でドリフトしないよう）
# ※ oidc.tf の lifecycle { ignore_changes = [content] } により
#   JWKS は Terraform で管理しない設計
```
