# T-20260302-005: argocd-diff ping未達 再々調査

## 根本原因分析

### 調査対象
- CI Run: 22534839119 / Job: 65354883857
- PR: #469 (`feat/google-integration-phase1`)

### 根本原因（`too many open files` 除外）

#### 1. PRブランチのワークフローが古い（Run 22534839119 固有）

PR #469 の `.github/workflows/argocd-diff.yaml` は T-20260301-012 の改善前のバージョンを使用:

| 項目 | PRブランチ (古い) | mainブランチ (現行) |
|------|------------------|-------------------|
| `ping` パラメータ | `ping: lolice-argocd` (アクション内蔵ping、3分タイムアウト) | なし（削除済み） |
| 診断ステップ | なし | `Tailscale reachability diagnostics` |
| 判定ステップ | なし | `Determine Tailscale usability` |
| `USE_TAILSCALE` 判定 | `steps.tailscale.outcome == 'success'` | `steps.tailscale-ready.outputs.usable` |

**影響**: `tailscale/github-action@v4` の内蔵pingが **3分間** 指数バックオフでリトライしてからフォールバック（main版は10秒で判定）。

#### 2. `lolice-argocd` プロキシデバイスがtailnet上に不在

最新Run（22565157223, PR #508）の診断ログ:

```
# tailscale status — lolice-argocd が不在
100.67.214.19  tailscale-operator  tagged-devices  linux  -

# getent hosts lolice-argocd — DNS解決失敗
getent failed; trying tailscale status grep
lolice-argocd not found in tailscale status

# tailscale ping — 名前解決エラー
error looking up IP of "lolice-argocd": lookup lolice-argocd on 127.0.0.53:53: server misbehaving
```

`tailscale-operator`（オペレータ自体）は存在するが、ArgoCD Service用のプロキシPod (`lolice-argocd`) が作成されていない。

#### 3. DNS解決エラーの原因

`server misbehaving` はsystemd-resolvedがMagicDNSに問い合わせた結果、レコードが存在しないことを示す。これはデバイスが不在であることの結果であり、DNS基盤の問題ではない。

### too many open files との関係（参考）

PR #508 の分析によると、プロキシ未作成の根本原因は `fs.inotify.max_user_instances` 枯渇。ただし本タスクではこれを既知不具合として除外し、CI側の改善に焦点を当てる。

## 実施した修正

### `.github/workflows/argocd-diff.yaml` の診断ステップ改善

1. **peer存在チェックの追加（早期終了）**: `tailscale status` で `lolice-argocd` が見つからない場合、10秒のpingタイムアウトを待たずに即座に失敗→フォールバック。具体的な原因候補をログに出力。

2. **エラーメッセージの改善**: 失敗箇所に応じた具体的なwarningメッセージ:
   - peer不在: 「proxy device does not exist in tailnet」+ 原因候補リスト
   - ping失敗: 「peer exists but is unreachable via WireGuard tunnel」
   - TCP失敗: 「ArgoCD gRPC-web endpoint may be down」

3. **TCP port接続チェックの追加**: `tailscale ping` 成功後に `nc -z -w 5 lolice-argocd 443` で実ポート疎通を確認。pingは通るがサービスがダウンしているケースを検出。

### ping依存判定の評価

- `tailscale ping` 自体は妥当（WireGuardトンネル経由の到達性確認）
- ただし **peer存在チェックを前段に追加** することで、プロキシ未作成時のタイムアウトを回避
- TCP port 443 チェックを後段に追加し、実サービスの可用性も確認
- `tailscale/github-action@v4` の内蔵 `ping` パラメータ（3分タイムアウト）は main では既に削除済み

## 検証手順

### 失敗再現
1. `lolice-argocd` プロキシがtailnet上に不在の状態で、`argoproj/` 配下を変更するPRを作成
2. ArgoCD Diff Check ワークフローが実行される
3. 期待結果: peer不在を即座に検出→Cloudflareフォールバック→成功

### 修正後の確認
1. 本PRをマージ後、`argoproj/` 配下を変更するPRで ArgoCD Diff Check が実行される
2. 期待結果:
   - peer不在時: 「lolice-argocd not found in tailscale peer list」warningが出力され、即座にCloudflareフォールバック（3分→数秒に短縮）
   - peer存在時: ping→TCP port 443→成功→Tailscaleパス使用

## 残課題

1. **プロキシ作成の復旧**: PR #508 (inotify limits fix) のマージ後、Tailscale Operator が `lolice-argocd` プロキシを作成できるか確認が必要
2. **PRブランチの更新**: PR #469 等の古いブランチは main にリベースすることで改善されたワークフローを取得できる
