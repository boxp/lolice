# T-20260301-003: argocd-diff Tailscale WIF keyless PoC 実装（Phase 2/3）

> **親計画**: [T-20260227-016/plan.md](../T-20260227-016/plan.md)（Tailscale Kubernetes Operator PoC）

## 概要

PR #501 の再ベースライン済み計画に基づき、Phase 2（ArgoCD Service の tailnet 公開）と Phase 3（argocd-diff の Tailscale WIF keyless 化）を実装する。

## 変更内容

### Phase 2: ArgoCD Service の tailnet 公開

`argocd-server` Service に Tailscale Operator の Annotation を追加し、tailnet 経由での到達を可能にする。

**変更ファイル:**
- `argoproj/argocd/overlays/argocd-server-tailscale.yaml` (新規)
- `argoproj/argocd/kustomization.yaml` (overlay 追加)

**Annotation:**
- `tailscale.com/expose: "true"` — Operator に Service の tailnet 公開を指示
- `tailscale.com/hostname: "lolice-argocd"` — tailnet 上のホスト名（MagicDNS で解決可能）

**動作:**
1. Tailscale Operator が Annotation を検知
2. `tailscale-operator` namespace に Proxy Pod を自動生成
3. Proxy Pod が tailnet デバイスとして `lolice-argocd` で登録
4. tailnet 参加済みデバイスから `lolice-argocd` で ArgoCD にアクセス可能

### Phase 3: argocd-diff の Tailscale WIF keyless 化

GitHub Actions ワークフロー `argocd-diff.yaml` を Tailscale WIF 経由に切り替える。既存 Cloudflare 経路はフォールバックとして保持。

**変更ファイル:**
- `.github/workflows/argocd-diff.yaml`

**認証フロー（変更前 / 変更後）:**

| 項目 | 変更前（Cloudflare） | 変更後（Tailscale WIF） |
|------|----------------------|-------------------------|
| ネットワーク経路 | Cloudflare Tunnel | Tailscale tailnet (WireGuard) |
| 認証方式 | Service Token（長寿命） | OIDC WIF（短寿命、自動発行） |
| 必要な Secrets | `ARGOCD_API_TOKEN_ID`, `ARGOCD_API_TOKEN_SECRET` | `TS_OAUTH_CLIENT_ID`, `TS_AUDIENCE` (構成識別子、非クレデンシャル) |
| ArgoCD API 認証 | `ARGOCD_AUTH_TOKEN` | `ARGOCD_AUTH_TOKEN` (変更なし) |
| サーバーアドレス | `vars.ARGOCD_SERVER_URL` (CF domain) | `lolice-argocd` (tailnet hostname) |
| CLI フラグ | `--grpc-web --insecure` + CF headers | `--grpc-web --plaintext` |

**WIF（Workload Identity Federation）の動作:**
1. GitHub Actions が OIDC トークンを自動発行（`id-token: write`）
2. `tailscale/github-action@v4` が OIDC トークンを Tailscale に提示
3. Tailscale が Trust Credential と照合し、エフェメラルノードとして認証
4. ランナーが tailnet に参加 → `lolice-argocd` に直接アクセス可能
5. ジョブ完了後、エフェメラルノードが自動削除

**フォールバック設計:**
- `tailscale/github-action` ステップに `continue-on-error: true` を設定
- Tailscale 接続失敗時は自動的に Cloudflare 経路にフォールバック
- PR コメントに使用した認証経路（tailscale / cloudflare）を表示

## 前提条件（arch 側で必要な作業）

以下は `boxp/arch` リポジトリで実施が必要:

1. **Tailscale ACL**: `tag:ci` → `tag:k8s-operator` へのアクセスルール追加
2. **Tailscale Trust Credential (WIF)**: Terraform で追加
   - Issuer: `https://token.actions.githubusercontent.com`
   - Subject: `repo:boxp/lolice:pull_request`
   - Tags: `tag:ci`
   - Scope: `auth_keys` (writable)
3. **GitHub リポジトリ設定**: `TS_OAUTH_CLIENT_ID`, `TS_AUDIENCE` を Secrets に登録

## 検証計画

### 段階的検証

1. **Phase 2 検証（annotation merge 後）:**
   - ArgoCD sync が成功し、Proxy Pod が生成されること
   - `tailscale status` で `lolice-argocd` デバイスが表示されること
   - tailnet 参加済みデバイスから `curl http://lolice-argocd` でアクセス可能

2. **Phase 3 検証（arch 側 WIF 設定後）:**
   - `argocd-diff` ワークフローで Tailscale ステップが成功
   - PR コメントに `Auth path: tailscale` が表示
   - `argocd app diff` が正常完了
   - Tailscale admin console でエフェメラルノードが出現→自動削除

### 成功条件

- [ ] `argocd-diff` が Tailscale WIF 経路で成功（PR コメントで確認）
- [ ] Cloudflare 経路がフォールバックとして機能（Tailscale 失敗時に自動切替）
- [ ] エフェメラルノードが自動削除される
- [ ] 既知インシデント（PROXY_TAGS型, OAuth secret名）の再発がない

## ロールバック手順

### Phase 2 ロールバック（Service annotation 撤去）

```bash
# 1. overlay ファイルを削除
git rm argoproj/argocd/overlays/argocd-server-tailscale.yaml

# 2. kustomization.yaml から参照を削除
# patchesStrategicMerge から "- overlays/argocd-server-tailscale.yaml" を削除

# 3. commit & push → ArgoCD auto sync で反映
git commit -m "revert: remove tailscale annotation from argocd-server"
git push origin main
```

Proxy Pod は Annotation 削除後に Operator が自動で撤去する。

### Phase 3 ロールバック（workflow 戻し）

```bash
# 1. argocd-diff.yaml を Cloudflare-only に戻す
# - "Connect to Tailscale" ステップを削除
# - env から ARGOCD_SERVER_TAILSCALE, USE_TAILSCALE を削除
# - ARGOCD_SERVER を vars.ARGOCD_SERVER_URL に直接設定
# - argocd app diff コマンドを Cloudflare header 版のみに戻す

# 2. commit & push
git commit -m "revert: restore cloudflare-only path for argocd-diff"
git push origin main
```

### 即時ロールバック（Phase 2 + 3 同時）

両方の変更を同時に revert する場合:

```bash
git revert <commit-hash>  # Phase 2/3 の commit を revert
git push origin main
```

## リスク

| リスク | 影響 | 緩和策 |
|--------|------|--------|
| Tailscale WIF 接続失敗 | diff が取得できない | Cloudflare フォールバックで自動復旧 |
| Proxy Pod リソース不足 | ArgoCD tailnet 接続不安定 | Operator の proxyResources で制限済み (CPU 100m, Mem 128Mi) |
| ACL 設定漏れ | CI ランナーから ArgoCD 到達不可 | `ping` オプションで事前検証 |
| tailnet 伝搬遅延 | ping タイムアウト | `tailscale/github-action` が最大3分待機 |

## 関連ドキュメント

| チケット | 内容 | 状態 |
|----------|------|------|
| [T-20260227-016](../T-20260227-016/plan.md) | Tailscale Operator PoC 全体計画 | Phase 1 完了 |
| [T-20260226-902](../T-20260226-902/plan.md) | Tailscale WIF PoC 初期計画 | 完了 |
| [T-20260228-004](../T-20260228-004/plan.md) | PROXY_TAGS 型不整合修正 | 完了 |
| [T-20260301-001](../T-20260301-001/plan.md) | OAuth Secret 名不一致修正 | 完了 |
