# T-20260226 gh-wrapper 制限緩和（project/issue系）

## 目的
OpenClaw環境で `gh project` / `gh issue` / Project操作に必要な `gh api graphql` 書き込み系が拒否されていたため、GitHub Project移行作業を可能にする。

## 変更対象
- `argoproj/openclaw/configmap-gh-wrapper.yaml` — gh-wrapperスクリプト本体
- `tests/test-gh-wrapper.sh` — テストスイート

## 変更内容

### 1. `gh project` サブコマンドの許可
- `project)` ケースを追加し、全サブコマンドを許可（`issue` と同様のポリシー）
- 対象: list, view, create, edit, close, delete, copy, item-list, item-add, item-edit, item-delete, item-archive, item-create, field-list, field-create, field-delete, mark-template, link, unlink

### 2. `gh api graphql` へのPOST許可
- APIエンドポイント検出ロジックを追加（最初の位置引数を解析）
- `graphql` エンドポイントに対して POST メソッドを許可（GraphQL queries/mutations に必要）
- `-f`/`-F`/`--raw-field`/`--field` も `graphql` エンドポイントに限り許可
- `--input` は引き続き全エンドポイントで拒否
- 非graphqlエンドポイントは従来通りGETのみ

### 3. 維持される拒否範囲
- `gh pr merge` — 引き続き拒否
- `gh auth` — `status` のみ許可
- `gh run` — `list`/`view` のみ許可
- `gh repo` 等その他 — 引き続き拒否
- `gh api` 非graphqlエンドポイント — GETのみ
- `gh api --input` — 全エンドポイントで拒否

## テスト結果
全75テスト PASS、0 FAIL
