# OpenClaw exec許可設定の恒久化

## 概要

OpenClawのexec運用で、`ls` / `ghq` / `gwq` / `gh`（read用途中心）を承認なしで実行可能にする設定を、
`boxp/lolice` のGitOpsソースに恒久反映する。

`gh` コマンドについてはサブコマンド制御方式を採用し、read-onlyサブコマンドのみを許可する
ラッパースクリプトで安全境界を実現する。

## 変更対象ファイル

### 1. `argoproj/openclaw/configmap-openclaw.yaml`

ConfigMap `openclaw-config` の `openclaw.json` に `tools` セクションを追加:

```json
"tools": {
  "exec": {
    "security": "allowlist",
    "ask": "on-miss",
    "safeBins": ["gh", "ls", "ghq", "gwq"],
    "pathPrepend": ["/opt/gh-wrapper"]
  }
}
```

- `security: "allowlist"` — safeBinsに含まれるコマンドのみ承認なしで実行可能
- `ask: "on-miss"` — safeBinsに含まれないコマンドはユーザーに実行確認を求める
- `safeBins` — 承認なし実行を許可するコマンド一覧
- `pathPrepend: ["/opt/gh-wrapper"]` — ラッパースクリプトのディレクトリをPATH先頭に追加。
  ConfigMapボリュームとしてread-onlyマウントされるため、書き込み可能ディレクトリの追加にはあたらず、
  PATHハイジャックリスクは発生しない

### 2. `argoproj/openclaw/configmap-gh-wrapper.yaml` (新規)

`gh` コマンドのラッパースクリプトを定義するConfigMap。
read-onlyサブコマンドのみを許可し、それ以外はexit code 126で拒否する。

### 3. `argoproj/openclaw/deployment-openclaw.yaml`

- `gh-wrapper` ConfigMapボリュームを追加（`defaultMode: 0555`でread-only実行可能）
- openclawコンテナに `/opt/gh-wrapper` マウントを追加（`readOnly: true`）

### 4. `argoproj/openclaw/kustomization.yaml`

- `configmap-gh-wrapper.yaml` をresourcesに追加

## 設計判断

### ghサブコマンド制御（ラッパースクリプト方式）

OpenClawの `safeBins` はバイナリ名レベルでしかフィルタできず、サブコマンドの区別ができない。
GITHUB_TOKENのスコープに依存する方式では、トークンスコープの変更だけで安全境界が崩れるリスクがある。

そこで、read-onlyサブコマンドのみを通すラッパースクリプト `gh` を `/opt/gh-wrapper/gh` に配置し、
`pathPrepend` で本物の `/usr/bin/gh` より先にヒットさせる。

ラッパーはConfigMapボリュームとしてKubernetesがread-onlyで提供するため:
- コンテナ内からの書き換えは不可能
- PATHハイジャック（書き込み可能ディレクトリをPATH先頭に追加する問題）には該当しない
- 変更はGitOps経由のConfigMap更新でのみ可能

### `channels.discord.execApprovals.enabled: false` について

**追加しない。** 理由:
- `tools.exec.security: "allowlist"` と `tools.exec.ask: "on-miss"` の組み合わせにより、
  safeBins内のコマンドは自動承認、それ以外はユーザー確認という制御が実現される
- `execApprovals` はDiscordの追加的な承認UIであり、上記設定で十分カバーされる
- 不要な設定を追加すると設定の複雑化を招く

## セキュリティ境界 — 許可/禁止サブコマンド一覧

### 許可（read-only操作）

| コマンド | サブコマンド | 説明 |
|----------|-------------|------|
| `gh auth` | `status` | 認証状態の確認 |
| `gh pr` | `list` | PR一覧の取得 |
| `gh pr` | `view` | PR詳細の閲覧 |
| `gh pr` | `diff` | PR差分の閲覧 |
| `gh pr` | `checks` | CIチェック状態の確認 |
| `gh pr` | `status` | PR状態サマリーの取得 |
| `gh run` | `list` | ワークフロー実行一覧の取得 |
| `gh run` | `view` | ワークフロー実行詳細の閲覧 |
| `gh issue` | `list` | Issue一覧の取得 |
| `gh issue` | `view` | Issue詳細の閲覧 |
| `gh api` | (GETのみ) | GitHub REST/GraphQL API (GET) |

### 禁止（write/destructive操作）

| コマンド | サブコマンド | 理由 |
|----------|-------------|------|
| `gh pr` | `merge`, `close`, `reopen`, `edit`, `create`, `review` | リポジトリ状態の変更 |
| `gh run` | `cancel`, `rerun`, `delete` | ワークフロー操作 |
| `gh issue` | `create`, `close`, `reopen`, `edit`, `delete` | Issue状態の変更 |
| `gh api` | POST, PATCH, PUT, DELETE | 書き込みAPI操作 |
| `gh repo` | (全て) | リポジトリ管理操作 |
| `gh release` | (全て) | リリース管理操作 |
| `gh ssh-key` | (全て) | SSH鍵管理 |
| その他 | (全て) | 上記許可リスト以外は全て拒否 |

### その他の安全なコマンド

| コマンド | 許可 | 不許可 |
|----------|------|--------|
| `ls` | ファイル一覧の参照 | — |
| `ghq` | リポジトリ管理 | — |
| `gwq` | worktree管理 | — |
| その他 | — | ユーザー承認なしの実行 |

### safeBins の制約事項

OpenClawの `safeBins` はstdin-only用途を想定しており、positional引数が
パスっぽい（`/` 始まり等）場合は自動拒否される。そのため:

- `gh api /repos/owner/repo/...` — 先頭 `/` がパスと判定され **拒否** される
- `gh api repos/owner/repo/...` — パスではないので **許可** される

`gh api` を使用する場合は、先頭スラッシュなしの形式を使用すること。

## リスクとロールバック

- **リスク**: ラッパースクリプトの不備により、意図しないサブコマンドが許可される可能性
- **緩和策**:
  - ラッパーはホワイトリスト方式（明示的に許可したもの以外は全て拒否）
  - ConfigMapはread-onlyマウントでコンテナ内からの改竄不可
  - `gh api` はGETメソッドのみ許可、`--input` も拒否
- **ロールバック**:
  1. ConfigMapの `tools.exec` から `pathPrepend` を削除し `safeBins` から `gh` を除外
  2. `configmap-gh-wrapper.yaml` をkustomization.yamlから除外
  3. deployment-openclaw.yaml から gh-wrapper のボリュームとマウントを削除
  - いずれもhot-reloadまたはPod再起動で即時反映
