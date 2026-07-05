# BOXP-53 codex-task-board dashboard plan

## Scope

codex-workspace 上の Task Board / Codex agent run を眺めるための読み取り専用 Dashboard を追加する。操作機能は持たせず、Task Board card、ticket file、run directory、lock file を変更しない。

## Data Source

- Source of truth は codex-workspace PVC 上の `/home/boxp/.codex-task-board`。
- run 一覧は `/home/boxp/.codex-task-board/runs/{ticket}/{run}` から生成する。
- 実行中判定は `/home/boxp/.codex-task-board/locks/{ticket}.edn` の存在を優先する。
- run metadata は `summary.edn`、補助情報は `events.jsonl` の timestamp / action / event count を読む。
- ログ表示は `events.jsonl`、`stderr.log`、`last-message.md` の存在するファイルを対象にする。

## Implementation

- `argoproj/codex-workspace` Deployment に `task-board-dashboard` サイドカーを追加する。
- サイドカーは `python:3.13-alpine` で ConfigMap から Python stdlib HTTP server と静的 UI を起動する。
- `/home/boxp` PVC は `readOnly: true` で mount する。
- API は `GET /api/runs`、`GET /api/runs/{ticket}/{run}`、`GET /api/runs/{ticket}/{run}/log?file=...&offset=...` のみ。
- UI は PC-98 風の低解像度風フォント、限定色、枠線、CSS pixel art sprite、ログウィンドウを持つ。
- polling は run 一覧 3 秒、ログ 2 秒。

## Exposure

- Dashboard 専用の ClusterIP Service に `dashboard` port 8080 を追加し、既存 LoadBalancer Service には載せない。
- NetworkPolicy は既存の `k8s` namespace cloudflared からの 8080/TCP を許可する。
- Cloudflare 側は `boxp/arch` で `codex-task-board.b0xp.io` を k8s tunnel 経由に追加し、GitHub login 必須の Access application を追加する。

## Validation

- 実データがある状態で API を起動し、`/api/runs` とログ API を確認する。
- 空の `runs/` を持つ fixture で UI/API が壊れないことを確認する。
- `last-message.md` などログ欠損 fixture で missing response と UI 表示を確認する。
- `kubectl kustomize argoproj/codex-workspace` で manifest build を確認する。
