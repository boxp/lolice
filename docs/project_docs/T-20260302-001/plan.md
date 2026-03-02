# T-20260302-001: argocd diff Tailscale 経路復旧

## 根本原因

`boxp/arch` リポジトリの `terraform/tailscale/lolice/acl.tf` において、ACL ルールが `var.argocd_service_cluster_ip`（デフォルト `""`）に依存する条件式になっており、変数未設定のため `acls = []`（全トラフィック拒否）となっていた。

### 経緯

1. subnet router 方式を想定して ClusterIP ベースの ACL ルールを設計
2. K8s Operator proxy 方式（`tailscale.com/expose` annotation）に切り替え
3. ACL ルールが更新されず、`tag:ci` → `tag:k8s-operator` のルールが不在
4. `argocd_service_cluster_ip` 未設定 → `acls = []` → 全ノード間通信拒否

### 症状

- Tailscale WIF 接続は成功するが `tailscale ping lolice-argocd` が 3 分タイムアウト
- Cloudflare fallback 経由で argocd diff 自体は動作（`Auth path: cloudflare`）

## 修正

- **arch PR #7314**: `acl.tf` に `tag:ci` → `tag:k8s-operator:80,443` の無条件 ACL ルールを追加
- terraform apply 後、Tailscale 経路が有効化される

## 検証

- arch PR #7314 マージ → terraform apply
- lolice で argoproj/ 配下を変更する PR を作成
- `argocd-diff` ワークフローで `Auth path: tailscale` が表示されることを確認
