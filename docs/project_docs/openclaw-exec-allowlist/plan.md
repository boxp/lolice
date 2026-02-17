# OpenClaw exec許可設定の恒久化

## 概要

OpenClawのexec運用で、`ls` / `ghq` / `gwq` / `gh`（read用途中心）を承認なしで実行可能にする設定を、
`boxp/lolice` のGitOpsソースに恒久反映する。

## 変更対象ファイル

### 1. `argoproj/openclaw/configmap-openclaw.yaml`

ConfigMap `openclaw-config` の `openclaw.json` に `tools` セクションを追加:

```json
"tools": {
  "exec": {
    "security": "allowlist",
    "ask": "on-miss",
    "safeBins": ["gh", "ls", "ghq", "gwq"]
  }
}
```

- `security: "allowlist"` — safeBinsに含まれるコマンドのみ承認なしで実行可能
- `ask: "on-miss"` — safeBinsに含まれないコマンドはユーザーに実行確認を求める
- `safeBins` — 承認なし実行を許可するコマンド一覧
- `pathPrepend` — 意図的に未設定。書き込み可能ディレクトリをPATH先頭に追加すると、safeBinsのコマンド名と同名のスクリプトを配置することで承認モデルを迂回されるリスクがあるため除外

### 2. `argoproj/openclaw/deployment-openclaw.yaml`

- 変更なし（`pathPrepend` を使用しないため、DeploymentのPATH・init-configへの変更も不要）

## 設計判断

### `channels.discord.execApprovals.enabled: false` について

**追加しない。** 理由:
- `tools.exec.security: "allowlist"` と `tools.exec.ask: "on-miss"` の組み合わせにより、
  safeBins内のコマンドは自動承認、それ以外はユーザー確認という制御が実現される
- `execApprovals` はDiscordの追加的な承認UIであり、上記設定で十分カバーされる
- 不要な設定を追加すると設定の複雑化を招く

### gh read-onlyポリシーの担保方法

**GITHUB_TOKENのスコープで制御する。** 理由:
- SSMに格納された `/lolice/openclaw/GITHUB_TOKEN` のスコープ（権限）がread-only相当を担保
- ラッパースクリプト方式は既存のDeployment構造で採用されておらず、不要な複雑化になる
- 将来ラッパーが必要な場合は、読み取り専用ボリュームからマウントする方式を検討

## セキュリティ境界

| 対象 | 許可 | 不許可 |
|------|------|--------|
| `ls` | ファイル一覧の参照 | — |
| `ghq` | リポジトリ管理 | — |
| `gwq` | worktree管理 | — |
| `gh` | GITHUB_TOKENスコープ内の操作 | トークンスコープ外の操作 |
| その他コマンド | — | ユーザー承認なしの実行 |

## リスクとロールバック

- **リスク**: safeBinsに含まれるコマンドが承認なしで実行されるため、予期しない操作が行われる可能性
- **緩和策**: safeBinsは最小限のコマンドに限定、ghはトークンスコープで制御
- **ロールバック**: ConfigMapの `tools` セクションを削除する
