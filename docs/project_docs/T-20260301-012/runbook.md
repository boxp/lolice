# Tailscale 経路未達時のトラブルシューティング Runbook

## 対象
`argocd-diff` ワークフローにおける `lolice-argocd` への Tailscale 経由の到達性確認・復旧手順。

## 前提
- Tailscale Kubernetes Operator が `tailscale-operator` namespace にデプロイ済み
- ArgoCD Server Service に `tailscale.com/expose: "true"` / `tailscale.com/hostname: lolice-argocd` を設定済み
- GitHub Actions WIF keyless 認証設定済み（`tag:ci`）

## チェック手順

### 1. Proxy Pod の状態確認

```bash
kubectl get pods -n tailscale-operator -l tailscale.com/parent-resource=argocd-server
```

- Pod が存在しない場合: Operator が Service annotation を検出できていない
  - `kubectl logs -n tailscale-operator deployment/tailscale-operator` でエラーを確認
  - `kubectl get svc argocd-server -n argocd -o yaml` で annotation が正しいか確認
- Pod が `CrashLoopBackOff` の場合: OAuth 認証情報の問題が多い
  - `kubectl get secret tailscale-operator-oauth -n tailscale-operator` で Secret の存在確認
  - ExternalSecret の同期状態: `kubectl get externalsecret -n tailscale-operator`

### 2. Tailscale ノード登録確認

proxy pod 内から tailscale status を確認:

```bash
kubectl exec -n tailscale-operator \
  $(kubectl get pods -n tailscale-operator -l tailscale.com/parent-resource=argocd-server -o name | head -1) \
  -- tailscale status
```

- `lolice-argocd` が表示されれば tailnet に登録済み
- 表示されない場合: OAuth client の権限・タグ設定を確認

### 3. ACL 確認

Tailscale Admin Console (https://login.tailscale.com/admin/acls) で以下を確認:

- `tag:ci` から `tag:k8s-operator` へのアクセスが許可されているか
- WIF Trust Credential の設定:
  - Issuer: `https://token.actions.githubusercontent.com`
  - Subject: `repo:boxp/lolice:pull_request`
  - Tags: `tag:ci`

### 4. MagicDNS 解決確認

tailnet に参加したノードから:

```bash
tailscale status | grep lolice-argocd
getent hosts lolice-argocd
tailscale ping -c 3 lolice-argocd
```

- `tailscale status` に表示されるが ping 失敗: ファイアウォール（NetworkPolicy）の可能性
- `tailscale status` に表示されない: proxy pod が tailnet に参加できていない

### 5. NetworkPolicy 確認

```bash
kubectl get networkpolicy -n tailscale-operator -o yaml
kubectl get networkpolicy -n argocd -o yaml
```

確認ポイント:
- `tailscale-operator` namespace の egress: TCP 443 (DERP/coordination), UDP 3478 (STUN) が許可されているか
- proxy pod から `argocd` namespace への TCP egress が許可されているか
- direct WireGuard (UDP 41641) を使う場合は ingress も必要

### 6. GitHub Actions ログ確認

`argocd-diff` ワークフローの以下のステップを確認:
1. **Connect to Tailscale (WIF keyless)**: WIF 認証・tailnet 参加の成否
2. **Tailscale reachability diagnostics**: `tailscale status`, DNS 解決, `tailscale ping` の結果
3. **Determine Tailscale usability**: 最終判定（`usable=true/false`）

## よくある原因と対処

| 症状 | 原因 | 対処 |
|------|------|------|
| WIF 接続自体が失敗 | OAuth Client ID/Audience の設定ミス | Secrets (`TS_OAUTH_CLIENT_ID`, `TS_AUDIENCE`) を確認 |
| proxy pod が存在しない | Operator が annotation を検出できていない | Operator ログ確認、Service annotation 確認 |
| proxy pod が CrashLoop | OAuth Secret 不一致 | ExternalSecret 同期確認、Secret 名の一致確認 |
| tailscale status に表示されない | proxy pod が tailnet 未参加 | proxy pod ログ確認、NetworkPolicy egress (TCP 443) 確認 |
| tailscale ping 失敗 | ACL 不許可 or NetworkPolicy | ACL で `tag:ci` -> `tag:k8s-operator` 許可確認 |
| DNS 解決失敗 | MagicDNS 無効 or hostname 未登録 | Tailscale Admin Console で MagicDNS 有効確認 |
