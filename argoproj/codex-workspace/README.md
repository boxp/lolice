# codex-workspace

## Availability model

`codex-workspace` は `/home/boxp` の RWO PVC と singleton writer の整合性を優先し、`replicas: 1` / `Recreate` を維持します。workspace、Obsidian sync、Task Board draft watcher、Codex cron、Task Board runner、Docker daemon は複製しません。Dashboard は PVC を read-only mount しますが、単独で複製しても runner の可用性は改善しないため同じ Pod に置きます。

Task Board lock には downward API から渡した Pod UID (`CODEX_TASK_BOARD_OWNER_ID`) と runner instance ID を記録します。計画 rollout では runner process 自身の SIGTERM shutdown hook と container の `preStop` が `/home/boxp/.codex-task-board/terminating-owners/` に同じ owner marker を書きます。`Recreate` で旧 Pod が完全に終了した後、別 UID の新 Pod だけが marker と owner / instance の一致する lock を `interrupted` にして再受付します。marker の作成・lock acquire・最終再走査・marker 削除は PVC 上の owner guard で直列化し、削除前に `/home/boxp/.codex-task-board/owners/` の旧 owner state を `:terminated` にするため、待機していた旧 owner が marker 削除直後に lock を作ることもありません。

SIGTERM 時の終了側の動作（BOXP-252）: `task-board-runner` は `tini` を PID 1 にし、bb runner を子として起動します。runner の shutdown hook は marker を書いた後、`setsid` で起動した agent CLI の process group に TERM を送り、`CODEX_TASK_BOARD_SHUTDOWN_AGENT_GRACE_SECONDS`（既定 20 秒、上限 45 秒）だけ待ってから残りを KILL します。進行中の run は終了側が `:interrupted`（reason `planned workspace shutdown`）で記録し、Task Board のレーン・frontmatter・Notes は変更せず、lock も解放しません。lock の回収は上記の次 Pod の marker 経路に任せるため、agent や remote 推論の終了を確認できないまま別 Pod が同じ run を二重に始めることはありません。終了予算は preStop 約 3 秒 + agent 猶予 20 秒 + finalize 5 秒で、開始済みの lifecycle commit（レーン・frontmatter・summary・lock の一連の書き込み）だけは hook 開始から `CODEX_TASK_BOARD_SHUTDOWN_COMMIT_BUDGET_SECONDS`（既定 45 秒、上限 55 秒）まで待ってから抜けるため、いずれも `terminationGracePeriodSeconds: 60` の内側です。lock と一致しなかった他 owner の marker は 1 時間後に `:terminated` にして削除されます。設計正本は Obsidian `Projects/codex-task-board-runner/graceful-shutdown/design.md` です。

旧 image からの初回 rollout、SIGKILL、node 障害など shutdown hook と preStop の両方が実行されない場合は heartbeat を使います。stale timeout は 180 秒、poll は 30 秒なので、最後の heartbeat から通常 210 秒以内に lock を回収します。marker のない fresh lock、owner または instance が一致しない marker、現在の Pod UID が所有する planned-shutdown markerは回収しません。

### Rollout

force delete は使わず、通常の Argo CD sync / image updater による Deployment rollout を使います。

初回導入では、旧 manifest の `CODEX_TASK_BOARD_LOCK_STALE_SECONDS=1800` が runner の default を上書きしないよう、次の順序を必須とします。

1. [boxp/lolice PR #723](https://github.com/boxp/lolice/pull/723) を先に merge / Argo CD sync し、現行 arch image のまま Deployment を rollout します。終了する旧 Pod は新しい preStop をまだ持たないため、この1回目は marker なしの fallback になります。
2. rollout 完了後、実 Pod の stale 180 秒 / poll 30 秒を確認し、旧 lock がある場合も最後の heartbeat から約 210 秒以内に受付が再開することを確認します。
3. 上記を確認してから [boxp/arch PR #11010](https://github.com/boxp/arch/pull/11010) を merge し、image updater の rollout を許可します。この2回目に終了する Pod は preStop を持ちますが、旧 runner の `prepare-shutdown` は失敗します。それでも先に適用済みの timeout で約 210 秒以内に復旧します。
4. 新 image の起動後は SIGTERM hook と preStop が有効になり、以後の計画 rollout では owner / instance が一致する lock を即時回収します。

手順 2 が完了するまで arch PR を merge しません。arch を先に rollout すると、既存 manifest の 1,800 秒設定により初回復旧が5分を超えるためです。

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
kubectl -n "${NS}" exec "${NEW_POD}" -c task-board-runner -- sh -c \
  'printf "stale=%s poll=%s\n" "$CODEX_TASK_BOARD_LOCK_STALE_SECONDS" "$CODEX_TASK_BOARD_POLL_SECONDS"'
kubectl -n "${NS}" logs "${NEW_POD}" -c task-board-runner --since-time="${STARTED_AT}" --timestamps
```

2 回目以降の計画 rollout では、実行中 run があった場合に次の log と ticket Notes を確認します。

```text
closing lock from planned owner shutdown: BOXP-N owner=<OLD_UID>
Codex run <run-id> was marked interrupted after planned workspace shutdown of owner <OLD_UID>.
```

初回 rollout で旧 lock に owner metadata がない場合は `closing stale lock` が 180 秒後に出るのが fallback の期待動作です。受付再開時間は新 Pod の `processing BOXP-N` log から計測し、停止開始から 5 分未満であること、同じ ticket の lock / run が同時に 2 個存在しないことを記録します。

### Rollback

`boxp/arch` の runner image とこの manifest は対になるため、rollback は active lock がない maintenance window で両 PR の revert を一体として適用します。image updater / Argo CD sync を調整し、旧 runner image と旧 manifest の desired revision を同じ変更単位で反映してください。旧 image だけを先行 rollout すると新 manifest の `prepare-shutdown` hook が失敗するため、単独 image downgrade は行いません。両 repository の適用を調整できない場合は downgrade せず forward fix を優先します。既存 lock の追加 EDN key は旧 runner から無視されます。

rollback 前後で `replicas: 1` / `Recreate` と RWO PVC は変更しません。適用直前に active lock がないことを再確認し、rollback 後は上記 rollout 手順と同じ観点で Pod Ready、runner log、active lock を確認します。

### Lock diagnosis and safe recovery

```sh
NS=codex-workspace
POD=$(kubectl -n "${NS}" get pod -l app=codex-workspace -o jsonpath='{.items[0].metadata.name}')
POD_UID=$(kubectl -n "${NS}" get pod "${POD}" -o jsonpath='{.metadata.uid}')

kubectl -n "${NS}" exec "${POD}" -c task-board-runner -- \
  find /home/boxp/.codex-task-board/locks -maxdepth 1 -type f -print
kubectl -n "${NS}" exec "${POD}" -c task-board-runner -- \
  find /home/boxp/.codex-task-board/terminating-owners -maxdepth 1 -type f -print
kubectl -n "${NS}" exec "${POD}" -c task-board-runner -- \
  find /home/boxp/.codex-task-board/owners -maxdepth 1 -type f -print
kubectl -n "${NS}" logs "${POD}" -c task-board-runner --since=10m --timestamps
```

lock の `:owner-id`、`:owner-instance-id`、`:heartbeat-at` と現在の `POD_UID` を比較します。current Pod UID の fresh lock は active とみなし、削除しません。別 owner の marker が lock と完全一致する場合、または heartbeat が 180 秒を超えた場合は loop の次 tick が自動回収します。marker 回収後に残る旧 owner state の `:status :terminated` は lock 再作成を防ぐ正常な tombstone です。自動回収されない場合は、まず非受付の recovery command を実行します。

```sh
kubectl -n "${NS}" exec "${POD}" -c task-board-runner -- \
  /opt/codex-workspace/task-board/task_board_runner.bb recover
```

lock の手動削除は、Deployment が 1 replica、旧 owner UID の Pod が存在しない、heartbeat が stale、`recover` が error になったことをすべて確認した場合の最終手段です。削除前に lock と run directory を退避し、対応 run の `summary.edn` を `interrupted` として残し、ticket Notes に理由を追記します。Task Board lane を source of truth とし、frontmatter や card status を古い run state から巻き戻しません。

### Remaining single points of failure

- 単一 `codex-workspace` Pod、RWO home PVC、配置 node
- Kubernetes control plane / Longhorn、Obsidian remote、GitHub / agent API
- Pod 再作成中の SSH / Even Terminal 切断と、agent session の非 live-migration
- SIGKILL / node 障害など shutdown hook と preStop の両方が実行されない場合の 180 秒 heartbeat fallback

## Novel Board runner

`novel-board-runner` sidecar（BOXP-246）は `Boards/Novel Board.md` を読み、`assignee::localllm`（lolice の `local-llm` / llama-server `gemma4-26b`）や `claude-*` / `codex*` が割り当てられたカードだけを起動します。image の entrypoint が `CODEX_WORKSPACE_ROLE=novel-board-runner` で `/opt/codex-workspace/novel-board/novel_board_runner.bb loop` を起動し、runner を含まない image では idle するため、sidecar の追加と image 更新はどちらの順でも安全です。

- state: `/home/boxp/.novel-board/`（0700。`work/NOVEL-N/` の原稿・挿絵、`runs/`、`locks/`、`published/`）。Task Board runner とは別 root で lock を共有しません。
- lock / heartbeat / planned shutdown は Task Board runner と同じ方式です（`CODEX_NOVEL_BOARD_OWNER_ID` = Pod UID、stale 180 秒、poll 30 秒、`preStop` で `prepare-shutdown`）。
- SIGTERM 時も Task Board runner と同じです（BOXP-252）: entrypoint が `tini -- novel_board_runner.bb loop` を起動し、hook が cloud agent の process group を TERM → `CODEX_NOVEL_BOARD_SHUTDOWN_AGENT_GRACE_SECONDS`（既定 20 秒）→ KILL、localllm の stream は閉じて `work/NOVEL-N/manuscript.partial.md` に部分出力を残します。部分出力は `manuscript.md` に昇格せず、lane / assignee / lock は次 Pod の marker 回収に任せます。llama-server 側の生成は停止を確認できないため終了側は再要求しません。
- local-llm は `--parallel 1` のため runner も localllm カードを同時に1件しか走らせません。llama-server は `codex-workspace` の NetworkPolicy で既に egress 許可済みです（`networkpolicy.yaml`）。
- 挿絵は `GEMINI_API_KEY` と home PVC 上の `~/.claude/skills/generate-image/generate-image.bb` で runner が生成します。
- 確認: `kubectl -n codex-workspace logs deploy/codex-workspace -c novel-board-runner --tail=50`。起動時に `novel-board runner started, ... localllm=http://llama-server.local-llm.svc.cluster.local:8080/v1 model=gemma4-26b` が出ます。
- rollback: この sidecar を deployment から削除するだけで停止します（`.novel-board` のデータは残す）。image の巻き戻しは不要です。

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
- 完了済み run fixture では `summary.edn` から ticket / run id / agent / lane / status を表示できること。`agent` がない旧runは `-` と表示すること。
- 欠損ログでは log API が `missing: true` を返し、UI が missing 表示へフォールバックできること。
- lock file がある run fixture では status が `running` になり、`events.jsonl` / `stderr.log` / `last-message.md` を offset 付きで読めること。

## Read-only boundary

Dashboard サイドカーは `/home/boxp` PVC を `readOnly: true` で mount します。HTTP API は `GET` のみを受け付け、Task Board card、ticket file、run directory、lock file を変更しません。

## Kohako G2 Board projection (BOXP-329)

Dashboard サイドカーは別 port `8090`（Service `codex-task-board-kohako`）で、Kohako G2 BFF 向けの読み取り専用 projection だけを返します。

- `GET /api/kohako/board`（schema `kohako-board/v1`）と `GET /healthz` のみ。他の path は 404、GET 以外は 405。既存 `:8080` の runs / log API はこの port から見えません。
- 入力は vault（`KOHAKO_VAULT_ROOT`、既定 `/home/boxp/Documents/obsidian-headless/BOXP`）の `Boards/Task Board.md` と `Tickets/BOXP-N.md`。ID は `BOXP-数字` だけ、ファイルは 512KB まで読みます。
- 返すのは In Progress → Review → Blocked → Ready → Done（48 時間以内に更新）の最大 30 件と、id・title・lane・status・担当（カード / 票）と不一致 flag・priority・更新時刻・最新 Notes の時刻 / source・進捗 / 阻害 / 要判断の 80 字抜粋・読取エラーだけです。抜粋は URL の path / query、メール、`token` / `key` / `secret` / `password` の値、32 字以上の英数字列を `…` に置き換えます。阻害・要判断はキーワードによる推定です。
- NetworkPolicy で `8090` への ingress は `even-g2-lab` の `app == 'even-g2-main'`（Kohako BFF sidecar）だけに許可します。
- 試験: `python3 -B -m unittest tests/workflows/test_kohako_board_projection.py`（yq v4 が必要）。
- コードは ConfigMap の `kohako_board.py` / `server.py` なので、反映は Pod の次の再起動（image 自動更新など）時です。

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
