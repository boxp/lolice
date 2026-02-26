# T-20260226: gh-wrapper GraphQL mutation allowlist 化

## 概要

gh-wrapper の `gh api` ハンドラを改修し、GraphQL エンドポイント(`graphql`)に対して
mutation 名ベースの allowlist フィルタリングを導入する。

## 現状

- `gh api` は GET のみ許可（POST/PATCH/PUT/DELETE はすべて拒否）
- GraphQL は POST を使うため、`gh api graphql` は事実上使用不可
- `gh issue` / `gh project` CLI サブコマンドは直接使えるが、GraphQL API 経由の操作ができない

## 変更方針

### `api)` ケースの分岐

1. **GraphQL パス** (`gh api graphql ...`):
   - `-f`/`--raw-field` で `query=`, `variables=`, `operationName=` のみ許可
   - `-F`/`--field` は `@file` バイパスリスクがあるため拒否
   - `--input` も拒否
   - ファイル参照（`query=@file`）も拒否
   - query（読み取り）はすべて許可
   - mutation は allowlist 内の名前のみ許可
   - **全トップレベル mutation フィールド名を抽出して全件 allowlist 判定**

2. **非 GraphQL パス** (`gh api <other>`):
   - 既存の GET-only ロジックを維持（変更なし）

### Mutation 名抽出ロジック（Codex レビュー反映版）

**方針: fail-closed — 抽出失敗時は拒否**

```
Step 1: tr で改行・タブを空白に正規化
Step 2: 先頭空白除去後、mutation で始まるか判定
Step 3: 最初の { 以降（selection set）を取得
Step 4: ネストした {...} を反復除去し、depth-0 のみ残す
Step 5: depth-0 から identifier( パターンを全件 grep -oE で抽出
Step 6: 抽出された全 mutation 名を allowlist と照合
```

**エイリアスバイパス対策:**
- GraphQL alias 構文: `aliasName: actualMutationName(args)`
- `grep -oE '[a-zA-Z_][a-zA-Z0-9_]*[[:space:]]*\('` は `actualMutationName(` を抽出
- エイリアス名は `:` の前にあり `(` の直前にないため抽出されない
- 例: `safe:deleteRepository(...)` → `deleteRepository` を抽出 → allowlist チェック

**複数 mutation 対策:**
- depth-0 の全 `identifier(` パターンを抽出
- 1つでも allowlist 外があれば全体を拒否

**抽出例:**
```
mutation { addComment(...) { ... } }                              → [addComment] → ✓
mutation Op($a: T!) { addComment(...) { ... } }                   → [addComment] → ✓
mutation { safe:deleteRepository(...) { ... } }                   → [deleteRepository] → ✗
mutation { addComment(...) { id } deleteRepository(...) { id } }  → [addComment, deleteRepository] → ✗
query { repository { ... } }                                      → (mutation でないので許可)
{ repository { ... } }                                            → (mutation でないので許可)
```

### 許可する Mutation 一覧

#### Issue 系
| Mutation 名 | 用途 |
|---|---|
| addComment | Issue/PR へのコメント追加 |
| updateIssueComment | コメント編集 |
| deleteIssueComment | コメント削除 |
| createIssue | Issue 作成 |
| updateIssue | Issue 更新 |
| closeIssue | Issue クローズ |
| reopenIssue | Issue 再オープン |
| deleteIssue | Issue 削除 |
| transferIssue | Issue 移動 |
| markIssueAsDuplicate | 重複マーク |
| unmarkIssueAsDuplicate | 重複マーク解除 |
| addLabelsToLabelable | ラベル追加 |
| removeLabelsFromLabelable | ラベル削除 |
| addAssigneesToAssignable | アサイン追加 |
| removeAssigneesFromAssignable | アサイン削除 |
| lockLockable | ロック |
| unlockLockable | ロック解除 |
| pinIssue | ピン留め |
| unpinIssue | ピン留め解除 |

#### ProjectV2 系
| Mutation 名 | 用途 |
|---|---|
| createProjectV2 | プロジェクト作成 |
| updateProjectV2 | プロジェクト更新 |
| deleteProjectV2 | プロジェクト削除 |
| copyProjectV2 | プロジェクトコピー |
| addProjectV2ItemById | アイテム追加 |
| updateProjectV2ItemFieldValue | フィールド値更新 |
| clearProjectV2ItemFieldValue | フィールド値クリア |
| deleteProjectV2Item | アイテム削除 |
| archiveProjectV2Item | アイテムアーカイブ |
| unarchiveProjectV2Item | アイテムアーカイブ解除 |
| updateProjectV2ItemPosition | アイテム並び替え |
| addProjectV2DraftIssue | ドラフトIssue追加 |
| updateProjectV2DraftIssue | ドラフトIssue更新 |
| deleteProjectV2DraftIssue | ドラフトIssue削除 |
| linkProjectV2ToRepository | リポジトリ連携 |
| unlinkProjectV2FromRepository | リポジトリ連携解除 |
| linkProjectV2ToTeam | チーム連携 |
| unlinkProjectV2FromTeam | チーム連携解除 |
| createProjectV2Field | カスタムフィールド作成 |
| updateProjectV2Field | カスタムフィールド更新 |
| deleteProjectV2Field | カスタムフィールド削除 |
| createProjectV2StatusUpdate | ステータス更新作成 |
| updateProjectV2StatusUpdate | ステータス更新編集 |
| deleteProjectV2StatusUpdate | ステータス更新削除 |

### 拒否される範囲（例）

- `createRepository`, `deleteRepository` — リポジトリ操作
- `createPullRequest`, `mergePullRequest` — PR 操作
- `createTeam`, `deleteTeam` — チーム管理
- `addStar`, `removeStar` — スター操作
- その他 allowlist 外のすべての mutation

## 変更対象ファイル

1. `argoproj/openclaw/configmap-gh-wrapper.yaml` — wrapper スクリプト本体
2. `tests/test-gh-wrapper.sh` — テストケース追加

## テスト計画

### 追加テストケース

#### 正常系
- GraphQL query が許可されること
- 省略形 query (`{ ... }`) が許可されること
- allowlist 内 mutation (addComment, addProjectV2ItemById 等) が許可されること
- named mutation が許可されること
- `-f variables=...` 併用が許可されること
- `-f operationName=...` 併用が許可されること

#### 拒否系
- allowlist 外 mutation (createRepository 等) が拒否されること
- **エイリアスバイパス** (`safe:deleteRepository(...)`) が拒否されること
- **複数 mutation** (許可+非許可混在) が拒否されること
- **パース不能 mutation** が拒否されること
- `-F`/`--field` が GraphQL で拒否されること
- `--input` が GraphQL で拒否されること
- ファイル参照 (`query=@file`) が拒否されること
- 許可外フィールド (`-f title=...`) が拒否されること

#### 回帰テスト
- 既存テスト（pr merge 拒否、api GET 許可等）が引き続きパスすること

## セキュリティ考慮

- **fail-closed 原則**: mutation 名の抽出に失敗した場合は拒否
- **全 mutation チェック**: 1 operation 内の全トップレベル mutation を検証
- **エイリアス耐性**: `grep -oE` で `identifier(` パターンを抽出するため、エイリアス名は無視される
- `-F`/`--field` は `@file` によるファイル読み込みでパースをバイパスできるため拒否
- `--input` も同様にファイル/stdin 経由のバイパスを防ぐため拒否
- query（読み取り操作）は既存方針に合わせてすべて許可
- ネスト除去の反復ループで depth-0 フィールドのみ抽出し、応答セレクション内の偽陽性を排除
- **マルチオペレーション・バイパス対策**: `mutation` キーワードをドキュメント全体で検索（先頭一致のみでなく）。mutation がドキュメント先頭以外にある場合は拒否（multi-operation + operationName による選択攻撃を防止）
- **コメント除去非実施**: GraphQL コメント (`#`) の除去は文字列リテラル内の `#` まで消してしまい、mutation キーワードを隠す攻撃に悪用されるため意図的に行わない。mutation の全文検索で十分検出可能
- **clientMutationId 誤検知防止**: `grep -qE` のワード境界チェック `(^|[^a-zA-Z0-9_])mutation([[:space:]{(]|$)` により、`clientMutationId` 等のサブストリング一致は除外
