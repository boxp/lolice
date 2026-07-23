# BOXP-64 codex-workspace kubectl exec/debug RBAC

## Scope

codex-workspace の task-board runner から Kubernetes API を使って限定的な Pod デバッグを行えるようにする。

対象範囲:

- Cluster: lolice Kubernetes cluster
- Namespace: `codex-workspace`, `hermes-agent`
- ServiceAccount: `system:serviceaccount:codex-workspace:codex-workspace`
- 実行主体: `argoproj/codex-workspace` Deployment 内の `task-board-runner` container。`workspace` と `codex-cron-scheduler` も同じ projected token/kubeconfig を mount しているため、同じ ServiceAccount 権限を持つ。
- Pod 条件: 初期スコープは `codex-workspace` と `hermes-agent` namespace 内の Pod。現状これらの namespace は各 workload 用として管理し、対象外 workload を同居させない運用境界にする。

Kubernetes RBAC は label selector による Pod 単位制限を持たないため、`app=codex-workspace` や `app=hermes-agent` のような label 条件は今回の RBAC では強制できない。許可 namespace 内に別 Pod を追加すると debug 対象になり得るため、別 workload を同居させる場合はこの RoleBinding のリスクを再評価する。

## RBAC

既存の `codex-workspace-read` ClusterRole は読み取り専用のまま維持する。今回追加する write/connect 系権限は `codex-workspace` と `hermes-agent` namespace の Role に限定する。

| Resource / subresource | Verbs | 付与 | 理由 |
| --- | --- | --- | --- |
| `pods/exec` | `create` | する | `kubectl exec` は Pod subresource への create として認可される。`codex-workspace` / `hermes-agent` 内で診断コマンドを実行するために必要。 |
| `pods/attach` | `create` | する | `kubectl debug -it` は ephemeral container 追加後に attach で接続するため必要。namespace-local に限定し、対象外 namespace では拒否する。 |
| `pods/log` | `get` | する | exec/debug 前後の状態確認に必要。既存 ClusterRole は core `pods` の read は持つが `pods/log` subresource は含まないため namespace-local に追加する。 |
| `pods/ephemeralcontainers` | `patch`, `update` | する | `kubectl debug` が既存 Pod に ephemeral container を追加するために必要。namespace-local に限定し、`create pods` は付与しない。 |
| `pods` | `create`, `patch`, `update`, `delete` | しない | `kubectl debug --copy-to` や Pod 作成・変更・削除を防ぐ。既存の read 権限のみ維持する。 |
| `secrets` | なし | しない | Secret の直接 read は不要。ただし exec/debug 先コンテナに mount 済み、または env として注入済みの Secret へは到達し得る。 |

## Risk

`pods/exec` は対象コンテナ内で任意コマンドを実行できるため、対象コンテナの Linux 権限、mount 済み Secret、ServiceAccount token、PVC、クラスタ内ネットワーク到達性を runner に委譲する。`pods/ephemeralcontainers` は既存 Pod の namespace に診断用コンテナを追加できるため、同じ Pod 内の process/network/filesystem 観測範囲が広がる。

今回の緩和策:

- write/connect 権限は `codex-workspace` と `hermes-agent` namespace の Role に閉じる。
- `pods` create/update/delete、`secrets` read は付与しない。
- `pods/attach` は `kubectl debug -it` に必要なため付与するが、namespace-local に限定する。
- Pod の `automountServiceAccountToken: false` は維持し、短期 projected token だけを明示 mount する。
- `codex-workspace` と `hermes-agent` namespace には debug 対象外 workload を同居させない。

明示的に受け入れるリスク:

- `hermes-agent` Pod には `cloudflared` sidecar が同居し、`hermes-agent-cloudflared-secret` が env として注入されている。`codex-workspace` ServiceAccount に `hermes-agent` namespace の `pods/exec` を付与すると、同 ServiceAccount から `cloudflared` container に exec して tunnel token を読める可能性がある。
- このチケットでは `hermes-agent` namespace の実デバッグ容易性を優先し、`cloudflared` の別 namespace / 別 Pod 分離は行わない。token 露出を許容できなくなった場合は、Cloudflare tunnel Pod の分離、または exec/debug 用 API proxy / admission policy を別途導入する。

後続課題:

- `kubectl debug` の image allowlist を admission policy で強制する。
- `pods/exec` の Pod label 単位制御が必要な場合は namespace 分離または API proxy / wrapper による allowlist を導入する。

## Validation Plan

Manifest validation:

```sh
kubectl kustomize argoproj/codex-workspace
kubectl kustomize argoproj/codex-workspace | kubectl --dry-run=client apply -f -
```

After Argo CD sync, verify as the runner ServiceAccount:

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

Expected:

- `yes`: `create pods/exec`, `create pods/attach`, `get pods/log`, `patch/update pods/ephemeralcontainers` in `codex-workspace` and `hermes-agent`
- `no`: `create pods`, `get secrets`
- `no`: `create pods/exec`, `create pods/attach`, and `patch pods/ephemeralcontainers` outside `codex-workspace` / `hermes-agent`

Operation check:

```sh
POD=$(kubectl -n codex-workspace get pod -l app=codex-workspace -o jsonpath='{.items[0].metadata.name}')
kubectl -n codex-workspace exec "${POD}" -c workspace -- id
kubectl -n codex-workspace debug "${POD}" -it --image=docker.io/library/busybox:1.37 --target=workspace -- /bin/sh
HERMES_POD=$(kubectl -n hermes-agent get pod -l app=hermes-agent -o jsonpath='{.items[0].metadata.name}')
kubectl -n hermes-agent exec "${HERMES_POD}" -c hermes-agent -- id
kubectl -n hermes-agent debug "${HERMES_POD}" -it --image=docker.io/library/busybox:1.37 --target=hermes-agent -- /bin/sh
kubectl -n default exec deploy/some-target-outside-debug-scope -- id
```

Expected:

- `codex-workspace` と `hermes-agent` namespace の exec/debug は成功する。
- 対象外 namespace の exec/debug は RBAC denied になる。

## Audit

Kubernetes API audit log で `user.username=system:serviceaccount:codex-workspace:codex-workspace`、`objectRef.resource=pods`、`objectRef.subresource in (exec, attach, log, ephemeralcontainers)` を確認する。監査ログの保存先はクラスタ構成に依存するため、control-plane node の kube-apiserver audit log、または導入済みログ基盤に集約された apiserver audit event を確認する。

runner 側では `/home/boxp/.codex-task-board/runs/{ticket}/{run}/events.jsonl`、`stderr.log`、`last-message.md` が ticket/run 単位の作業履歴になる。audit log の user/serviceAccount と Task Board の run id/ticket を突き合わせることで、どの runner run がどの Pod に対して操作したかを追跡する。

## Rollback

`argoproj/codex-workspace/rbac.yaml` と `argoproj/hermes-agent/rbac.yaml` から `Role/codex-workspace-debug` と `RoleBinding/codex-workspace-debug` を削除し、Argo CD sync する。既存の read-only `ClusterRole` は今回変更しない。

## Run Notes

2026-07-08 task-board run:

- `kubectl kustomize argoproj/codex-workspace` は成功し、`Role/codex-workspace-debug` と `RoleBinding/codex-workspace-debug` が生成されることを確認した。
- `kubectl apply --dry-run=client -f argoproj/codex-workspace/rbac.yaml` は成功した。
- `kubectl kustomize argoproj/codex-workspace | kubectl --dry-run=client apply -f -` は既存の Calico `NetworkPolicy` の client-side patch で `expected a struct, but received a nil` となり失敗した。Role/RoleBinding までは dry-run で正常に処理されている。
- 現行クラスタの `codex-workspace` ServiceAccount は `create pods/exec -n codex-workspace` と `patch/update pods/ephemeralcontainers -n codex-workspace` が `no`、対象外 namespace の `create pods/exec` も `no` だった。
- この runner には `create roles.rbac.authorization.k8s.io -n codex-workspace` と `create rolebindings.rbac.authorization.k8s.io -n codex-workspace` がないため、実クラスタへ RBAC を適用して `kubectl exec` / `kubectl debug` の実操作確認までは未実施。PR merge 後の Argo CD sync 後に Validation Plan の操作確認を実施する。
- 2026-07-08 follow-up: `hermes-agent` namespace でも同じ debug 操作を許可するため、`argoproj/hermes-agent/rbac.yaml` に `Role/codex-workspace-debug` / `RoleBinding` を追加した。許可主体は引き続き `system:serviceaccount:codex-workspace:codex-workspace` に限定し、`pods create/update/delete` と `secrets get/list/watch` は付与しない。
- 2026-07-08 follow-up validation: `kubectl kustomize argoproj/hermes-agent` で `Role/codex-workspace-debug` / `RoleBinding` が生成されることを確認した。`kubectl apply --dry-run=client -f argoproj/codex-workspace/rbac.yaml -f argoproj/hermes-agent/rbac.yaml` と `git diff --check` は成功した。`kubectl kustomize argoproj/hermes-agent | kubectl apply --dry-run=client -f -` は既存 Calico `NetworkPolicy` の client-side patch で `expected a struct, but received a nil` となり失敗したが、追加した Role/RoleBinding までは dry-run で正常に処理されている。現行 runner では `kubectl auth can-i ... --as=system:serviceaccount:codex-workspace:codex-workspace` が impersonation 権限不足で失敗するため、実 SA としての確認は PR merge + Argo CD sync 後に実施する。`--as` なしの現行 runner 権限では `create pods/exec -n hermes-agent`、`patch pods/ephemeralcontainers -n hermes-agent`、`create pods/exec -n default` はすべて `no`。
- 2026-07-08 retry: codex review 指摘を受けて一度は `cloudflared` を `hermes-agent-cloudflared` namespace に分離したが、ユーザー判断により tunnel token 露出リスクを受け入れて最小変更へ戻した。最終方針は `codex-workspace` ServiceAccount に `hermes-agent` namespace の debug RoleBinding を追加し、`hermes-agent` Pod 内の `cloudflared` sidecar は維持する。
