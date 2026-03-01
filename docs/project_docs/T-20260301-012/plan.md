# T-20260301-012: argocd-diff Tailscale 未達（ping 失敗）原因切り分けと解消

## 背景

`argocd-diff` ワークフローで Tailscale WIF keyless 接続自体は成功するが、`ping: lolice-argocd`（`tailscale/github-action@v4` のパラメータ）が失敗し、Cloudflare fallback に落ちている。

原因候補:
1. Proxy Pod が未起動 or tailnet に未参加
2. ACL で `tag:ci` → `tag:k8s-operator` のアクセスが未許可
3. MagicDNS での `lolice-argocd` 解決失敗
4. NetworkPolicy による通信ブロック

## 変更内容

### 1. ワークフロー診断強化 (`.github/workflows/argocd-diff.yaml`)

- `tailscale/github-action` から `ping` パラメータを削除（失敗時に内部で何が起きているか不明なため）
- 新規ステップ `Tailscale reachability diagnostics` を追加:
  - `tailscale status`: tailnet 参加状態・peer 一覧
  - `getent hosts lolice-argocd`: DNS 解決確認
  - `tailscale ping -c 3 --timeout 10s lolice-argocd`: 到達性確認
- 新規ステップ `Determine Tailscale usability` を追加:
  - Tailscale 接続成功 AND 診断ステップ成功時のみ `usable=true`
  - fallback 判定を `steps.tailscale-ready.outputs.usable` に変更

### 2. Runbook 追加 (`docs/project_docs/T-20260301-012/runbook.md`)

Tailscale 経路未達時のトラブルシューティング手順:
- Proxy Pod 状態確認
- tailnet 登録確認
- ACL 確認
- MagicDNS 解決確認
- NetworkPolicy 確認
- GitHub Actions ログの読み方

## 非スコープ

- fail-fast 専用ジョブ追加
- Cloudflare fallback の削除
- クラスタへの直接 apply
- NetworkPolicy の変更（診断結果を待って別チケットで対応）

## 検証方法

1. PR作成 → `argocd-diff` ワークフローが実行される
2. ワークフローログで以下を確認:
   - `Tailscale reachability diagnostics` ステップの出力
   - `tailscale status` に `lolice-argocd` が表示されるか
   - `tailscale ping` の成否と詳細メッセージ
3. fallback 動作は既存通り維持されていること

## 関連チケット

- 親計画: T-20260227-016 (Tailscale Kubernetes Operator PoC)
- Phase 2/3 実装: T-20260301-003
- NetworkPolicy 修正: T-20260301-009
