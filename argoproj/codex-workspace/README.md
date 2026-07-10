# codex-workspace

## Availability model

`codex-workspace` は `/home/boxp` の RWO PVC と singleton writer の整合性を優先し、`replicas: 1` / `Recreate` を維持します。workspace、Obsidian sync、Task Board draft watcher、Codex cron、Task Board runner、Docker daemon は複製しません。Dashboard は PVC を read-only mount しますが、単独で複製しても runner の可用性は改善しないため同じ Pod に置きます。

Task Board lock には downward API から渡した Pod UID (`CODEX_TASK_BOARD_OWNER_ID`) と runner instance ID を記録します。計画 rollout の `preStop` は `/home/boxp/.codex-task-board/terminating-owners/` に owner marker を書きます。`Recreate` で旧 Pod が完全に終了した後、別 UID の新 Pod だけが marker と owner / instance の一致する lock を `interrupted` にして再受付します。

旧 image からの初回 rollout、SIGKILL、node 障害など preStop が実行されない場合は heartbeat を使います。stale timeout は 180 秒、poll は 30 秒なので、最後の heartbeat から通常 210 秒以内に lock を回収します。marker のない fresh lock、owner または instance が一致しない marker、現在の Pod UID が所有する planned-shutdown markerは回収しません。

### Rollout

force delete は使わず、通常の Argo CD sync / image updater による Deployment rollout を使います。

```sh
NS=codex-workspace
DEPLOY=codex-workspace

kubectl -n "${NS}" get deploy "${DEPLOY}" \
  -o jsonpath='replicas={.spec.replicas} strategy={.spec.strategy.type}{"\n"}'
OLD_POD=$(kubectl -n "${NS}" get pod -l app=codex-workspace -o jsonpath='{.items[0].metadata.name}')
OLD_UID=$(kubectl -n "${NS}" get pod "${OLD_POD}" -o jsonpath='{.metadata.uid}')
STARTED_AT=$(date -u +%FT%TZ)

# Git merge / Argo CD sync または image updater の更新後
kubectl -n "${NS}" rollout status deploy/"${DEPLOY}" --timeout=5m
kubectl -n "${NS}" get pod -l app=codex-workspace \
  -o custom-columns='NAME:.metadata.name,UID:.metadata.uid,READY:.status.containerStatuses[*].ready,START:.status.startTime'
NEW_POD=$(kubectl -n "${NS}" get pod -l app=codex-workspace -o jsonpath='{.items[0].metadata.name}')
kubectl -n "${NS}" logs "${NEW_POD}" -c task-board-runner --since-time="${STARTED_AT}" --timestamps
```

2 回目以降の計画 rollout では、実行中 run があった場合に次の log と ticket Notes を確認します。

```text
closing lock from planned owner shutdown: BOXP-N owner=<OLD_UID>
Codex run <run-id> was marked interrupted after planned workspace shutdown of owner <OLD_UID>.
```

初回 rollout で旧 lock に owner metadata がない場合は `closing stale lock` が 180 秒後に出るのが fallback の期待動作です。受付再開時間は新 Pod の `processing BOXP-N` log から計測し、停止開始から 5 分未満であること、同じ ticket の lock / run が同時に 2 個存在しないことを記録します。

### Rollback

`boxp/arch` の runner image とこの manifest は対になるため、通常は両 PR を revert して Argo CD sync します。先に manifest だけ戻すと preStop marker は使われなくなりますが、既存 lock の追加 EDN key は旧 runner から無視され、180 秒設定を戻した場合は旧 timeout に戻ります。先に image だけ戻すと manifest の `prepare-shutdown` hook が失敗するため避けてください。

rollback 中も `replicas: 1` / `Recreate` と RWO PVC は変更しません。rollback 後は上記 rollout 手順と同じ観点で Pod Ready、runner log、active lock を確認します。

### Lock diagnosis and safe recovery

```sh
NS=codex-workspace
POD=$(kubectl -n "${NS}" get pod -l app=codex-workspace -o jsonpath='{.items[0].metadata.name}')
POD_UID=$(kubectl -n "${NS}" get pod "${POD}" -o jsonpath='{.metadata.uid}')

kubectl -n "${NS}" exec "${POD}" -c task-board-runner -- \
  find /home/boxp/.codex-task-board/locks -maxdepth 1 -type f -print
kubectl -n "${NS}" exec "${POD}" -c task-board-runner -- \
  find /home/boxp/.codex-task-board/terminating-owners -maxdepth 1 -type f -print
kubectl -n "${NS}" logs "${POD}" -c task-board-runner --since=10m --timestamps
```

lock の `:owner-id`、`:owner-instance-id`、`:heartbeat-at` と現在の `POD_UID` を比較します。current Pod UID の fresh lock は active とみなし、削除しません。別 owner の marker が lock と完全一致する場合、または heartbeat が 180 秒を超えた場合は loop の次 tick が自動回収します。自動回収されない場合は、まず非受付の recovery command を実行します。

```sh
kubectl -n "${NS}" exec "${POD}" -c task-board-runner -- \
  /opt/codex-workspace/task-board/task_board_runner.bb recover
```

lock の手動削除は、Deployment が 1 replica、旧 owner UID の Pod が存在しない、heartbeat が stale、`recover` が error になったことをすべて確認した場合の最終手段です。削除前に lock と run directory を退避し、対応 run の `summary.edn` を `interrupted` として残し、ticket Notes に理由を追記します。Task Board lane を source of truth とし、frontmatter や card status を古い run state から巻き戻しません。

### Remaining single points of failure

- 単一 `codex-workspace` Pod、RWO home PVC、配置 node
- Kubernetes control plane / Longhorn、Obsidian remote、GitHub / agent API
- Pod 再作成中の SSH / Even Terminal 切断と、agent session の非 live-migration
- preStop が実行されない障害で必要になる 180 秒 heartbeat fallback

## Task Board Dashboard

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
