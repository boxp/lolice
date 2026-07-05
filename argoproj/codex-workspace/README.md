# codex-workspace Task Board Dashboard

codex-workspace Pod に同居する読み取り専用 Dashboard です。Task Board runner が PVC 上へ出力する `/home/boxp/.codex-task-board` を読み、agent run とログを PC-98 風 UI で表示します。

## Data sources

- `/home/boxp/.codex-task-board/runs/{ticket}/{run}/summary.edn`
- `/home/boxp/.codex-task-board/runs/{ticket}/{run}/events.jsonl`
- `/home/boxp/.codex-task-board/runs/{ticket}/{run}/stderr.log`
- `/home/boxp/.codex-task-board/runs/{ticket}/{run}/last-message.md`
- `/home/boxp/.codex-task-board/locks/{ticket}.edn`

実行中判定は lock file の存在を優先します。ログファイルがない run は missing として表示します。

## Local check

ローカル確認では `server.py` の ConfigMap 内容を一時ファイルへ展開し、次の環境変数で起動します。

```sh
TASK_BOARD_ROOT=/home/boxp/.codex-task-board PORT=8080 python3 /tmp/server.py
```

Kubernetes 上では `codex-task-board-dashboard` ClusterIP Service の port 8080 として公開され、Cloudflare Access GitHub login の背後で `https://codex-task-board.b0xp.io` から参照する想定です。既存の `codex-workspace` LoadBalancer Service には Dashboard port を載せません。

確認観点:

- `kubectl kustomize argoproj/codex-workspace` で manifest を生成できること。
- 空の `runs/` では `/api/runs` が空配列を返すこと。
- 完了済み run fixture では `summary.edn` から ticket / run id / lane / status を表示できること。
- 欠損ログでは log API が `missing: true` を返し、UI が missing 表示へフォールバックできること。
- lock file がある run fixture では status が `running` になり、`events.jsonl` / `stderr.log` / `last-message.md` を offset 付きで読めること。

## Read-only boundary

Dashboard サイドカーは `/home/boxp` PVC を `readOnly: true` で mount します。HTTP API は `GET` のみを受け付け、Task Board card、ticket file、run directory、lock file を変更しません。
