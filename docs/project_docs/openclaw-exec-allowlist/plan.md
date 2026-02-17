# OpenClaw exec許可設定の恒久化

## 概要

OpenClawのexec運用で、都度承認をほぼ不要にする設定を `boxp/lolice` のGitOpsソースに恒久反映する。

### 変更履歴

1. **Phase 1** (完了): `safeBins` に `gh`, `ls`, `ghq`, `gwq` を追加、gh-wrapper導入
2. **Phase 2** (本PR): `ask` を `"on-miss"` → `"off"` に変更、`safeBins` をread系コマンドで大幅拡張

### Phase 2 の方針

- `security: "allowlist"` を維持（safeBinsに含まれないコマンドは実行不可）
- `ask: "off"` に変更（safeBinsに含まれるコマンドの都度確認を廃止）
- `safeBins` をread系・テキスト処理系コマンド中心に拡張

`gh` コマンドについてはサブコマンド制御方式を維持し、以下のポリシーで安全境界を実現する:

- **`gh pr`** — `merge` 以外のすべてのサブコマンドを許可
- **`gh issue`** — すべてのサブコマンドを許可
- **`gh pr merge`** — 明示的に拒否（オプション付きでも）

## 変更対象ファイル

### 1. `argoproj/openclaw/configmap-openclaw.yaml`

ConfigMap `openclaw-config` の `openclaw.json` > `tools.exec` セクションを更新:

```json
"tools": {
  "exec": {
    "security": "allowlist",
    "ask": "off",
    "safeBins": [
      "gh", "ghq", "gwq",
      "ls", "cat", "grep",
      "head", "tail", "wc", "jq",
      "uniq", "cut", "tr",
      "pwd", "date", "stat",
      "dirname", "basename"
    ],
    "pathPrepend": ["/opt/gh-wrapper"]
  }
}
```

#### 変更点

| 項目 | 変更前 | 変更後 |
|------|--------|--------|
| `ask` | `"on-miss"` | `"off"` |
| `safeBins` | `["gh", "ls", "ghq", "gwq"]` (4個) | 上記17個に拡張 |

#### safeBinsカテゴリ分類

| カテゴリ | コマンド | 用途 |
|----------|----------|------|
| ツール管理 | `gh`, `ghq`, `gwq` | GitHub CLI (wrapper経由), リポジトリ管理, worktree管理 |
| ファイル参照 | `ls`, `cat`, `stat` | ファイル一覧・内容・属性の参照 |
| テキスト検索 | `grep` | パターン検索 |
| テキスト加工 | `head`, `tail`, `wc`, `jq`, `uniq`, `cut`, `tr` | テキストのフィルタリング・整形・変換 |
| パス操作 | `dirname`, `basename` | パス文字列の操作 |
| 環境情報 | `pwd`, `date` | 現在ディレクトリ・日時の取得 |

### 既存ファイル（変更なし）

- `configmap-gh-wrapper.yaml` — gh-wrapperスクリプト（Phase 1で導入済み）
- `deployment-openclaw.yaml` — gh-wrapperボリューム/マウント（Phase 1で導入済み）
- `kustomization.yaml` — リソース一覧（Phase 1で追加済み）

## 設計判断

### `ask: "off"` の採用理由

- `ask: "on-miss"` では、safeBinsに含まれないコマンドの実行時にユーザー確認が発生していた
- `ask: "off"` + `security: "allowlist"` の組み合わせにより、safeBins外コマンドは**確認なしで拒否**される
- これにより、Discordチャンネル等での都度承認操作が不要になり、自動運用が実現する
- 安全性は `security: "allowlist"` + `safeBins` のホワイトリストで担保される

### safeBins拡張の方針

**追加基準**: 読み取り専用、またはstdin/stdoutベースのテキスト処理に限定

- ファイルシステムを変更しないコマンドのみ（`cat`, `grep`, `head` 等）
- 子プロセス起動機能を持たないコマンドのみ（`find -exec` や `awk 'BEGIN{system(...)}'` のような
  任意コマンド実行が可能なコマンドは除外）
- テキストストリーム処理コマンド（`cut`, `tr` 等）はstdin/stdoutベースで副作用なし
- 環境情報の参照コマンド（`pwd`, `date`）は副作用なし

### bash/git等を safeBins に入れない理由

以下のコマンドは **意図的に safeBins から除外** している:

| 除外コマンド | 除外理由 |
|-------------|---------|
| `rg` | `--pre` オプションでプリプロセッサとして任意の外部コマンドを子プロセス実行可能。allowlist境界を回避できる |
| `sort` | `-o` オプションでファイルへの直接書き込みが可能。読み取り専用の原則に違反する |
| `bash`, `sh`, `zsh` | 任意コマンドの実行基盤となる。safeBinsの全ての制約をバイパスできてしまう（例: `bash -c "rm -rf /"`) |
| `find` | `-exec` オプションで任意コマンドを子プロセスとして実行可能。allowlist境界を回避できる |
| `awk` | `system()` 関数で任意コマンド実行可能。`BEGIN{system("rm -rf /")}` のようにallowlistを完全にバイパスできる |
| `sed` | `-e` と `w` コマンドでファイル書き込みが可能。GNU sedでは `e` コマンドで任意コマンド実行も可能 |
| `git` | `git push --force`, `git reset --hard` 等の破壊的操作が可能。リポジトリの履歴改変やデータ消失のリスク |
| `python`, `python3`, `node` | 任意コード実行が可能。ネットワークアクセス、ファイル書き込み等あらゆる操作の踏み台になる |
| `rm`, `mv`, `cp`, `chmod`, `chown` | ファイルシステムの破壊的変更が可能 |
| `curl`, `wget` | 任意URLへのデータ送信（exfiltration）、悪意あるスクリプトのダウンロード実行が可能 |
| `docker`, `kubectl` | コンテナ/クラスタレベルの操作が可能。権限昇格の踏み台になりうる |
| `ssh`, `scp` | リモートアクセス、データ転送が可能 |
| `dd`, `mkfs` | ブロックデバイスレベルの破壊的操作が可能 |
| `tee` | ファイルへの書き込みが可能（stdout分岐の副作用） |

**原則**: 「書き込み操作」または「任意実行の踏み台化」が可能なコマンドは safeBins に含めない。

### ghサブコマンド制御（ラッパースクリプト方式）

（Phase 1から変更なし）

OpenClawの `safeBins` はバイナリ名レベルでしかフィルタできず、サブコマンドの区別ができない。
GITHUB_TOKENのスコープに依存する方式では、トークンスコープの変更だけで安全境界が崩れるリスクがある。

そこで、ポリシーに基づくサブコマンド制御を行うラッパースクリプト `gh` を `/opt/gh-wrapper/gh` に配置し、
`pathPrepend` で本物の `/usr/bin/gh` より先にヒットさせる。

ラッパーはConfigMapボリュームとしてKubernetesがread-onlyで提供するため:
- コンテナ内からの書き換えは不可能
- PATHハイジャック（書き込み可能ディレクトリをPATH先頭に追加する問題）には該当しない
- 変更はGitOps経由のConfigMap更新でのみ可能

## セキュリティ境界 — 許可/禁止サブコマンド一覧

### 許可

| コマンド | サブコマンド | 説明 |
|----------|-------------|------|
| `gh pr` | **merge以外すべて** | create, list, view, diff, checks, status, checkout, close, comment, edit, lock, ready, reopen, revert, review, unlock, update-branch |
| `gh issue` | **すべて** | create, list, view, close, comment, delete, develop, edit, lock, pin, reopen, status, transfer, unlock, unpin |
| `gh auth` | `status` | 認証状態の確認 |
| `gh run` | `list`, `view` | ワークフロー実行の閲覧 |
| `gh api` | (GETのみ) | GitHub REST/GraphQL API (GET) |

### 禁止

| コマンド | サブコマンド | 理由 |
|----------|-------------|------|
| `gh pr` | `merge` | マージ操作はGitOps/CI経由で行うべき |
| `gh run` | `cancel`, `rerun`, `delete` | ワークフロー操作 |
| `gh api` | POST, PATCH, PUT, DELETE | 書き込みAPI操作 |
| `gh repo` | (全て) | リポジトリ管理操作 |
| `gh release` | (全て) | リリース管理操作 |
| `gh ssh-key` | (全て) | SSH鍵管理 |
| その他 | (全て) | 上記許可リスト以外は全て拒否 |

### safeBins の制約事項

OpenClawの `safeBins` はstdin-only用途を想定しており、positional引数が
パスっぽい（`/` 始まり等）場合は自動拒否される。そのため:

- `gh api /repos/owner/repo/...` — 先頭 `/` がパスと判定され **拒否** される
- `gh api repos/owner/repo/...` — パスではないので **許可** される

`gh api` を使用する場合は、先頭スラッシュなしの形式を使用すること。

## リスクとロールバック

### リスク

- `ask: "off"` により、safeBins内コマンドは完全に自動実行される
- safeBinsに含まれるコマンドの組み合わせで意図しない操作が行われる可能性

### 緩和策

- `security: "allowlist"` により、safeBins外のコマンドは実行不可（確認も出ない）
- safeBinsには読み取り専用/テキスト処理コマンドのみを含める
- `bash`, `git`, `python` 等の任意実行可能コマンドは除外
- `gh` はラッパースクリプトでサブコマンドレベルの制御を維持
- ConfigMapはread-onlyマウントでコンテナ内からの改竄不可

### ロールバック手順

1. `configmap-openclaw.yaml` の `tools.exec.ask` を `"on-miss"` に戻す
2. `tools.exec.safeBins` を `["gh", "ls", "ghq", "gwq"]` に戻す
3. ArgoCD syncまたはPod再起動で即時反映
