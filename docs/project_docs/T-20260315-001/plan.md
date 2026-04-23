# T-20260315-001: Daily Morning Research 用承認設定修正

## 背景

`daily-morning-trend-research` cron ジョブが 2026-03-11 以降 6 日間連続で承認ブロックされ、
完全に機能停止していた。

## 原因

`/home/node/.claude/settings.json` の `permissions.allow` に以下が不足:

1. `Bash(bash /home/node/.claude/skills/xai-x-search/scripts/search.sh *)` — X検索スクリプト実行
2. `Bash(bash /home/node/.claude/skills/xai-web-search/scripts/search.sh *)` — Web検索スクリプト実行
3. `WebFetch` — はてなブックマーク等の外部 URL 取得

cron 実行時はインタラクティブな承認応答ができないため、全データソースへのアクセスがブロックされていた。

## 修正内容（v2: ConfigMap-only）

### 前案（v1: deployment 起動時パッチ）の問題点

- `deployment-openclaw.yaml` の起動スクリプトに `node -e` でインラインJavaScriptを埋め込み、
  `settings.json` を動的にパッチしていた
- deployment の責務が過剰（起動スクリプトが設定管理を担うべきではない）
- `settings.json` の内容が Docker イメージ内とパッチスクリプトの2箇所に分散
- パッチの前後で settings.json の内容が不透明（Git diff で最終状態が見えない）

### v2: ConfigMap-only アプローチ

`settings.json` 全体を ConfigMap (`configmap-claude-settings.yaml`) として定義し、
Deployment の volumeMount で `/home/node/.claude/settings.json` に直接マウントする。

**変更ファイル:**
- `configmap-claude-settings.yaml` (新規): settings.json の全内容を静的に定義
- `deployment-openclaw.yaml`: `node -e` パッチを削除、ConfigMap の volumeMount を追加
- `kustomization.yaml`: 新しい ConfigMap リソースを追加

**追加する権限:**
- `Bash(bash /home/node/.claude/skills/xai-x-search/scripts/search.sh *)`
- `Bash(bash /home/node/.claude/skills/xai-web-search/scripts/search.sh *)`
- `WebFetch`

## 設計判断

- **ConfigMap-only で成立する理由**: `settings.json` は Docker イメージに bake されていたが、
  内容は静的な設定であり、ConfigMap として外部化する方が GitOps の原則に合致する。
  ConfigMap の volumeMount (subPath) で直接ファイルをマウントするため、
  起動スクリプトでの動的パッチは不要。
- **dotfiles symlink の `settings.json` 除外は維持**: ConfigMap マウントされたファイルが
  dotfiles によって上書きされることを防ぐ安全策として、既存の case 除外をそのまま残す。
- **最小権限の原則**: `Bash(bash *)` のような広範なパターンではなく、
  特定のスクリプトパスのみを許可。

## リスク

- スキルスクリプトのパスが変更された場合、ConfigMap の更新が必要
- Docker イメージ内の settings.json と ConfigMap の内容が乖離する可能性があるが、
  volumeMount が優先されるため実害はない
