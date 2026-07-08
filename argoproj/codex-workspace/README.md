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

## Runner Kubernetes debug access

`task-board-runner` は `system:serviceaccount:codex-workspace:codex-workspace` の projected token と `codex-workspace-kubeconfig` を使って Kubernetes API に接続します。既存の cluster-wide 権限は read-only で、debug 用の write/connect 権限は `codex-workspace` と `hermes-agent` namespace の `Role/codex-workspace-debug` に限定しています。

許可する subresource:

- `pods/exec create`: `kubectl exec` で許可 namespace 内の Pod に診断コマンドを実行する。
- `pods/log get`: exec/debug 前後のログ確認に使う。
- `pods/ephemeralcontainers patch/update`: `kubectl debug` で許可 namespace 内の Pod に ephemeral debug container を追加する。
- `pods/attach create`: `kubectl debug -it` で追加した ephemeral container に接続する。

許可しない subresource/verb:

- `pods create/update/delete`: `kubectl debug --copy-to` や任意 Pod 作成は許可しない。
- `secrets get/list/watch`: Secret の直接参照は許可しない。

RBAC では Pod label selector を強制できないため、対象 Pod の境界は `codex-workspace` / `hermes-agent` namespace と ServiceAccount によって管理します。これらの namespace には debug 対象外の workload を同居させない運用にします。対象コンテナに mount 済み、または env として注入済みの Secret、ServiceAccount token、PVC、到達可能なクラスタ内 network resource は `exec` / `debug` から参照できます。`hermes-agent` Pod の `cloudflared` sidecar には tunnel token が注入されているため、`hermes-agent` namespace の exec 権限はその token 露出リスクを含むものとして扱います。

確認コマンド:

```sh
SA=system:serviceaccount:codex-workspace:codex-workspace

kubectl auth can-i create pods/exec -n codex-workspace --as="${SA}"
kubectl auth can-i get pods/log -n codex-workspace --as="${SA}"
kubectl auth can-i patch pods/ephemeralcontainers -n codex-workspace --as="${SA}"
kubectl auth can-i update pods/ephemeralcontainers -n codex-workspace --as="${SA}"
kubectl auth can-i create pods/attach -n codex-workspace --as="${SA}"
kubectl auth can-i create pods/exec -n hermes-agent --as="${SA}"
kubectl auth can-i get pods/log -n hermes-agent --as="${SA}"
kubectl auth can-i patch pods/ephemeralcontainers -n hermes-agent --as="${SA}"
kubectl auth can-i update pods/ephemeralcontainers -n hermes-agent --as="${SA}"
kubectl auth can-i create pods/attach -n hermes-agent --as="${SA}"
kubectl auth can-i create pods -n codex-workspace --as="${SA}"
kubectl auth can-i create pods -n hermes-agent --as="${SA}"
kubectl auth can-i get secrets -n codex-workspace --as="${SA}"
kubectl auth can-i get secrets -n hermes-agent --as="${SA}"
kubectl auth can-i create pods/exec -n default --as="${SA}"
kubectl auth can-i patch pods/ephemeralcontainers -n default --as="${SA}"
```

期待値は `codex-workspace` と `hermes-agent` namespace の `pods/exec`、`pods/log`、`pods/ephemeralcontainers`、`pods/attach` が `yes`、Pod 作成、Secret 参照、その他 namespace の exec/debug/attach が `no` です。

実操作確認:

```sh
POD=$(kubectl -n codex-workspace get pod -l app=codex-workspace -o jsonpath='{.items[0].metadata.name}')
kubectl -n codex-workspace exec "${POD}" -c workspace -- id
kubectl -n codex-workspace debug "${POD}" -it --image=docker.io/library/busybox:1.37 --target=workspace -- /bin/sh
HERMES_POD=$(kubectl -n hermes-agent get pod -l app=hermes-agent -o jsonpath='{.items[0].metadata.name}')
kubectl -n hermes-agent exec "${HERMES_POD}" -c hermes-agent -- id
kubectl -n hermes-agent debug "${HERMES_POD}" -it --image=docker.io/library/busybox:1.37 --target=hermes-agent -- /bin/sh
kubectl -n default exec deploy/some-target-outside-debug-scope -- id
```

監査は kube-apiserver audit log で `user.username=system:serviceaccount:codex-workspace:codex-workspace` と `objectRef.subresource` (`exec`, `attach`, `log`, `ephemeralcontainers`) を確認します。Task Board 側の `/home/boxp/.codex-task-board/runs/{ticket}/{run}/events.jsonl`、`stderr.log`、`last-message.md` と突き合わせると、ticket/run 単位で誰がどの runner からどの Pod に操作したかを追えます。
