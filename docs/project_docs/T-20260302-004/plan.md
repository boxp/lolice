# T-20260302-004: argocd-diff ping未達の再調査

## 調査対象

- **GitHub Actions Run**: [22534839119](https://github.com/boxp/lolice/actions/runs/22534839119)
- **Job**: 65354883857 (argocd-diff)
- **PR**: [#469](https://github.com/boxp/lolice/pull/469) (feat/google-integration-phase1)
- **実行日時**: 2026-03-02T06:01:58Z

## 根本原因

### 直接原因: inotify リソース枯渇によるProxy Pod未生成

ワーカーノード `golyat-1` の `fs.inotify.max_user_instances` がデフォルト値（128）で枯渇しており、
Tailscale Operator が fsnotify watcher を作成できず、proxy pod を生成できなかった。

**因果関係チェーン:**

1. `fs.inotify.max_user_instances` 枯渇（デフォルト: 128）
2. Tailscale Operator が `failed to create fsnotify watcher: too many open files` で繰り返しクラッシュ（16回再起動）
3. Operator が Service アノテーション (`tailscale.com/expose`) を watch 不能
4. `lolice-argocd` の proxy pod が tailnet 上に作成されない
5. CI の `tailscale ping lolice-argocd` が3分間タイムアウト
6. `USE_TAILSCALE=false` → Cloudflare fallback 経路で動作

### 補足: ACL修正のタイミング

arch PR #7314（`tag:ci` → `tag:k8s-operator` ACL ルール追加）は Run の4分前（05:57:33Z）にマージ済み。
ACL 自体は修正されたが、proxy pod が存在しないため ACL の有無は無関係。

### 注意: タスク要件との差異

タスク要件では「proxy pod は存在しているにもかかわらず」と前提されていたが、
実際には **proxy pod は存在していない**。inotify 枯渇が原因で Operator が pod を作成できない状態。

## ログ分析

### Tailscale 接続フェーズ（成功）

```
06:02:08Z ✅ tailscaled daemon is up and running!
06:02:10Z ✅ Tailscale up command completed successfully on attempt 1
06:02:10Z ✅ Tailscale is running and connected!
```

Tailscale 自体の接続は正常。WIF (Workload Identity Federation) 認証も成功。

### Ping フェーズ（失敗）

```
06:02:10Z Will ping hosts lolice-argocd up to 3 minutes each
06:02:10Z Pinging host lolice-argocd
06:02:10Z ▶️ ping host  (exponential backoff で繰り返し)
...
06:05:10Z ▶️ ping host  (5秒間隔で繰り返し)
06:05:10Z ##[error]❌ Ping host lolice-argocd did not respond
```

約3分間（06:02:10 → 06:05:10）`tailscale ping` を試行し、すべてタイムアウト。

### Fallback フェーズ（成功）

```
06:05:11Z USE_TAILSCALE: false
06:05:11Z ##[warning]Tailscale unavailable, falling back to Cloudflare path (server: argocd-api.b0xp.io:443)
06:05:14Z Changes detected for application: tailscale-operator
```

Cloudflare 経路で正常に ArgoCD diff を実行。

## ワークフロー分岐ロジック

### PR #469 ブランチ版（実行されたもの）

```yaml
- name: Connect to Tailscale (WIF keyless)
  continue-on-error: true
  uses: tailscale/github-action@v4
  with:
    ping: lolice-argocd  # action内蔵の3分ping

- name: Extract applications and check for changes
  env:
    USE_TAILSCALE: "${{ steps.tailscale.outcome == 'success' && 'true' || 'false' }}"
```

`ping: lolice-argocd` パラメータにより action 内部で `tailscale ping` を実行。
失敗時は `steps.tailscale.outcome = 'failure'` → `USE_TAILSCALE = 'false'`。

### main ブランチ版（PR #505 で改善済み）

main ブランチでは PR #505 により診断ステップが分離されている:
- `Tailscale reachability diagnostics` ステップ: `tailscale status` + DNS解決 + `tailscale ping -c 3 --timeout 10s`
- `Determine Tailscale usability` ステップ: 両ステップの outcome を組み合わせて判定

PR #469 ブランチはこの改善を取り込んでいない。

## 修正方針

### 必要な修正（既存PR）

| PR | リポジトリ | 内容 | 状態 |
|---|---|---|---|
| #508 | boxp/lolice | `node-sysctl-inotify` DaemonSet 追加 | OPEN (CI pass, レビュー済み) |
| #7314 | boxp/arch | ACL ルール追加 | MERGED |

**PR #508 のマージが根本解決に必須。** 追加のコード修正は不要。

### マージ後の検証手順

1. PR #508 マージ → ArgoCD sync で DaemonSet 展開
2. `kubectl rollout restart deployment/operator -n tailscale-operator`
3. proxy pod (`ts-*`) が `tailscale-operator` namespace に出現することを確認
4. `argoproj/` 変更を含む PR で argocd-diff を実行
5. `Auth path: tailscale` がログに出力されることを確認

## ping 依存判定の妥当性

`tailscale ping` は Tailscale 内蔵の到達性確認で、ICMP ではなく WireGuard 経路を使用。
ホストが tailnet 上に存在しない場合はタイムアウトするため、到達性確認として妥当。

ただし、proxy pod が存在しても ArgoCD gRPC-web ポート(80)への疎通は別問題となりうる。
現状の `tailscale ping` + Cloudflare fallback の構成は、疎通不可時のリスクを十分にカバーしている。

## 関連PR・タスク一覧

| ID | 内容 | 状態 |
|---|---|---|
| PR #502 | Tailscale WIF keyless path 追加 | MERGED (Mar 1) |
| PR #505 | Tailscale 診断ステップ改善 | MERGED (Mar 2) |
| PR #506 | ACL 空問題の根本原因ドキュメント | OPEN |
| PR #508 | inotify limits DaemonSet | OPEN (要マージ) |
| arch #7314 | ACL ルール追加 | MERGED (Mar 2) |
| T-20260302-001 | ACL 空問題の調査 | 完了 |
| T-20260302-003 | inotify 問題の調査 | 完了 |
