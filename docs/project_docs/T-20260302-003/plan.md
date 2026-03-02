# T-20260302-003: argocd diff 再調査・復旧

## 根本原因

arch PR #7314 で Tailscale ACL (空 → tag:ci→tag:k8s-operator 許可) を修正し
terraform apply も成功したが、argocd diff は依然として Cloudflare fallback で動作。

**真の原因**: ワーカーノード `golyat-1` 上の Tailscale Operator が
`fs.inotify.max_user_instances` 不足により fsnotify watcher を作成できず、
プロキシ pod (`lolice-argocd`) が一切作成されていなかった。

### 症状チェーン

1. `fs.inotify.max_user_instances` がデフォルト (128) で枯渇
2. Tailscale Operator が `failed to create fsnotify watcher: too many open files` を繰り返しログ出力 (数秒間隔)
3. Operator が Service annotation (`tailscale.com/expose`) を watch 不能
4. プロキシ pod 未作成 → `lolice-argocd` デバイスが tailnet に不在
5. CI の `tailscale ping lolice-argocd` が 3 分タイムアウト
6. Cloudflare fallback で argocd diff 自体は成功 (Auth path: cloudflare)

### 証拠

- Prometheus: `kube_pod_container_status_running{namespace="tailscale-operator"}` → operator pod のみ、proxy pod なし
- Prometheus: `kube_pod_container_status_restarts_total{namespace="tailscale-operator"}` → 16 回リスタート
- Loki: `{namespace="tailscale-operator"}` → 全ログが `failed to create fsnotify watcher: too many open files`
- CI ログ (run 22534839119): ACL 修正後 (06:02) でも `USE_TAILSCALE=false`, Cloudflare fallback

## 修正内容

### lolice (本 PR)

`argoproj/tailscale-operator/node-sysctl.yaml` に DaemonSet を追加:
- 全ノードで `fs.inotify.max_user_instances=8192`, `fs.inotify.max_user_watches=524288` を設定
- initContainer (privileged) で sysctl を書き換え、pause コンテナで常駐
- 既存ノード + 将来追加ノードに自動適用

### 残課題

- **Operator pod の再起動**: ArgoCD sync 後に operator pod を手動 restart する必要がある可能性
  (`kubectl rollout restart deployment/operator -n tailscale-operator`)
- **arch ansible**: golyat-1 が ansible 管理対象に入った際に sysctl を追加 (defense in depth)
- **inotify 根本対策**: ノード全体の inotify 消費量の監視メトリクスを検討

## 検証手順

1. 本 PR マージ → ArgoCD sync → DaemonSet が全ノードに展開
2. `kubectl rollout restart deployment/operator -n tailscale-operator`
3. proxy pod (`ts-*`) が tailscale-operator namespace に出現することを確認
4. lolice で argoproj/ 変更を含む PR を作成 → argocd-diff ワークフローで `Auth path: tailscale` を確認
