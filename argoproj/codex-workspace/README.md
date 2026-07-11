# codex-workspace

## Availability model

`codex-workspace` は `/home/boxp` の RWO PVC と singleton writer の整合性を優先し、`replicas: 1` / `Recreate` を維持します。workspace、Obsidian sync、Task Board draft watcher、Codex cron、Task Board runner、Novel Board runner、Docker daemon は複製しません。Dashboard は PVC を read-only mount しますが、単独で複製しても runner の可用性は改善しないため同じ Pod に置きます。

Task Board lock には downward API から渡した Pod UID (`CODEX_TASK_BOARD_OWNER_ID`) と runner instance ID を記録します。計画 rollout では runner process 自身の SIGTERM shutdown hook と container の `preStop` が `/home/boxp/.codex-task-board/terminating-owners/` に同じ owner marker を書きます。`Recreate` で旧 Pod が完全に終了した後、別 UID の新 Pod だけが marker と owner / instance の一致する lock を `interrupted` にして再受付します。marker の作成・lock acquire・最終再走査・marker 削除は PVC 上の owner guard で直列化し、削除前に `/home/boxp/.codex-task-board/owners/` の旧 owner state を `:terminated` にするため、待機していた旧 owner が marker 削除直後に lock を作ることもありません。

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

`novel-board-runner` は `Boards/Novel Board.md` を source of truth とし、Task Board runner とは別の `/home/boxp/.novel-board` に lock、run log、作業中原稿を保存します。`Draft` と `Review` は human review point で、対応 agent が再アサインされるまで起動しません。人間が承認してカードを `Done` へ移した後だけ完成版を配置します。Pi route は `/etc/pi-agent-config/models.json` を mount し、画像入力対応の `llama.cpp/gemma4-26b-vision` を明示選択します。runner の `--mode text` は stdout 形式の指定で、管理ノートの vault-local 画像埋め込みは `@image` として別途入力されます。

手動で作品を追加する場合は `Novel Board` の `Backlog` に `- [ ] タイトル` の未リンクカードを保存します。次の poll で runner が `NOVEL-N` を採番し、正式カードと `Templates/Novel Management.md` 由来の `Novels/NOVEL-N.md` を scaffold します。担当省略時は `assignee::boxp` のため agent は起動しません。Board 冒頭の短いルールと `Novels/README.md` を運用 source とし、レーン変更は管理ノート frontmatter ではなく Board 上で行います。

### Rollout

この変更は companion の `boxp/arch` PR #11014 の head commit `dd41a339522767aa5f803e29f32600a5bb00513f` を workflow dispatch で公開した `sha-dd41a33` image を `.argocd-source-codex-workspace.yaml` に固定しています。[publishing run](https://github.com/boxp/arch/actions/runs/29149777509) の成功、registry digest `sha256:0975c8a89331b88d6b77855655e3c7acb3e490f8e1a09b9a24019de7e7edee66`、image 内の Novel runner / title-only scaffold / レーン別 `#novel-rule` と管理ノート Workflow seed / Pi vision input / process-shared note update lock / root・UID 1000 の両方に対応する `CODEX_WORKSPACE_ROLE=novel-board-runner` entrypoint 契約を確認済みです。image updater による別 tag への更新だけを前提にしないため、manifest を先行させても runner role を起動できない image は選ばれません。

```sh
NS=codex-workspace
DEPLOY=codex-workspace

kubectl -n "${NS}" rollout status deploy/"${DEPLOY}" --timeout=5m
POD=$(kubectl -n "${NS}" get pod -l app=codex-workspace -o jsonpath='{.items[0].metadata.name}')
kubectl -n "${NS}" get pod "${POD}" \
  -o jsonpath='{.status.containerStatuses[?(@.name=="novel-board-runner")].ready}{"\n"}'
kubectl -n "${NS}" logs "${POD}" -c novel-board-runner --since=10m --timestamps
kubectl -n "${NS}" exec "${POD}" -c novel-board-runner -- sh -c \
  'test -x /opt/codex-workspace/novel-board/novel_board_runner.bb && test -f "$CODEX_NOVEL_BOARD_VAULT/Boards/Novel Board.md" && test -d "$CODEX_NOVEL_BOARD_ROOT" && test "$CODEX_NOVEL_BOARD_PI_MODEL" = llama.cpp/gemma4-26b-vision && test -f /etc/pi-agent-config/models.json'
```

root init container は sidecar 起動前に `/home/boxp/.novel-board` を UID/GID 1000、mode `0700` へ正規化します。rollout 後は `Novel Board has no cards` または対象カードの処理 log、`Boards/Novel Board.md` の5レーンと運用ルール、`Templates/Novel Management.md`、`Novels/README.md`、private root の owner/mode、既存 `task-board-runner` と `codex-cron-scheduler` の継続稼働を確認します。

実行中の Novel lock がある状態で `Recreate` rollout した場合、planned-shutdown marker は停止理由を記録しますが、marker だけでは lock を即時回収しません。旧 runner または agent の停止を heartbeat で確認するため、新 Pod は最後の heartbeat が 180 秒を超えるまで待機します。poll は 30 秒なので、受入条件は最後の heartbeat から通常 180〜210 秒以内に `planned owner shutdown with stale heartbeat` として lock が回収され、現在の Novel Board lane から処理を再開することです。この待機中に同一カードの agent を二重起動しないことも確認します。

### Lock diagnosis and recovery

```sh
kubectl -n "${NS}" exec "${POD}" -c novel-board-runner -- \
  find /home/boxp/.novel-board/locks -maxdepth 1 -type f -print
kubectl -n "${NS}" exec "${POD}" -c novel-board-runner -- \
  /opt/codex-workspace/novel-board/novel_board_runner.bb recover
```

fresh lock は planned-shutdown marker と一致していても削除しません。別 Pod UID の marker と一致し、かつ heartbeat が180秒を超えた lock は `planned owner shutdown with stale heartbeat` として回収します。marker がない異常終了でも、heartbeat が180秒を超えれば `stale heartbeat` として回収します。いずれも管理ノートと run summary に `interrupted` を残し、poll 30秒を含めると最後の heartbeat から通常210秒以内に再開します。復旧後も Novel Board lane を優先し、過去 run から状態や完成版を巻き戻しません。

### Rollback

active Novel run がないことを確認して、この `novel-board-runner` sidecar 追加だけを先に revert します。sidecar は `CODEX_WORKSPACE_ROLE=novel-board-runner` を設定し、image entrypoint に runner loop を起動させます。通常 workspace はこの role を設定しないため runner を起動しません。private root と管理ノート、完成版は削除せず、image revert も必須ではありません。再導入時は current lane と既存 `published.edn` から冪等に再開します。

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
