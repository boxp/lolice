# T-20260315-001: Daily Morning Research 用承認設定修正

## 背景

`daily-morning-trend-research` cron ジョブが 2026-03-11 以降 6 日間連続で承認ブロックされ、
完全に機能停止していた。

## 原因

`/home/node/.claude/settings.json` の `permissions.allow` に以下が不足:

1. `Bash(bash *)` パターン — `bash /home/node/.claude/skills/xai-x-search/scripts/search.sh` が承認要求される
2. `WebFetch` — はてなブックマーク等の外部 URL 取得が承認要求される

cron 実行時はインタラクティブな承認応答ができないため、全データソースへのアクセスがブロックされていた。

## 修正内容

`deployment-openclaw.yaml` のコンテナ起動スクリプトに、`settings.json` へのパッチ処理を追加:

- `Bash(bash /home/node/.claude/skills/xai-x-search/scripts/search.sh *)`
- `Bash(bash /home/node/.claude/skills/xai-web-search/scripts/search.sh *)`
- `WebFetch`

これにより、Pod 起動時に自動的に必要な許可設定が追加される。

## 設計判断

- **プロジェクトレベル `.claude/settings.json` ではなく deployment パッチを選択した理由**:
  Claude Code の `.claude` ディレクトリはセキュリティ上の制約があり、
  また Docker イメージ内に bake された settings.json を runtime でパッチする方が
  lolice リポジトリの責務範囲内で完結する
- **最小権限の原則**: `Bash(bash *)` のような広範なパターンではなく、
  特定のスクリプトパスのみを許可
- **冪等性**: `includes()` チェックにより重複追加を防止

## リスク

- スキルスクリプトのパスが変更された場合、パッチ対象のパスも更新が必要
- Node.js の `require('fs')` に依存（コンテナイメージに Node.js が存在する前提）
