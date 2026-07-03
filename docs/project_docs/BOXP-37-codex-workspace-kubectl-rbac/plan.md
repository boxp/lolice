# BOXP-37 codex-workspace kubectl RBAC

## 目的

Codex workspace から Kubernetes manifest の事前確認と cluster 状態の読み取り調査をできるようにする。
`kubectl` / `kustomize` は workspace image 側で利用可能な前提とし、この変更では `boxp/lolice` の GitOps manifest に in-cluster 接続用の ServiceAccount、kubeconfig、RBAC、mount を追加する。

## 実装計画

- `codex-workspace` namespace に専用 ServiceAccount `codex-workspace` を追加する。
- Pod の `automountServiceAccountToken: false` は維持し、明示的な projected volume で短期 ServiceAccount token、`kube-root-ca.crt`、namespace を mount する。
- ConfigMap `codex-workspace-kubeconfig` を追加し、`tokenFile` と cluster CA を参照する in-cluster kubeconfig を提供する。
- `workspace` container、`codex-cron-scheduler` container、`task-board-runner` container に `KUBECONFIG=/var/run/secrets/codex-workspace/kubeconfig/config` と read-only mount を追加する。
- `secrets` を除く core resource と、リポジトリで利用している主要 API group / Kubernetes 標準 API group に `get`, `list`, `watch` のみを付与する ClusterRole / ClusterRoleBinding を追加する。

## 権限範囲

許可する操作:

- core API group の `pods`, `services`, `configmaps`, `events`, `nodes`, `namespaces`, `persistentvolumes`, `persistentvolumeclaims`, `serviceaccounts` などの `get`, `list`, `watch`
- `apps`, `batch`, `networking.k8s.io`, `storage.k8s.io`, `apiextensions.k8s.io`, `argoproj.io`, `monitoring.coreos.com`, `external-secrets.io`, `projectcalico.org`, `longhorn.io`, `pingcap.com` などの `get`, `list`, `watch`

明示的に許可しない操作:

- core `secrets` の `get`, `list`, `watch`
- `create`, `update`, `patch`, `delete` などの write 系 verbs
- `pods/log`, `pods/exec`, `pods/portforward` などの pod subresource
- `tokenreviews`, `subjectaccessreviews` など、通常 `create` を必要とする認証・認可レビュー系操作

## 検証

2026-07-03 の conflict 解消 run では、前回の BOXP-37 差分を最新 `origin/main` に載せ直し、追加済みの `task-board-runner` sidecar にも同じ kubeconfig / token mount を適用した。
この Task Board run の実行環境には `kubectl` が PATH 上になかったため、前回 run で取得済みの `/tmp/kubectl-v1.36.1` を使って manifest 検証を行った。

実行済み:

```bash
/tmp/kubectl-v1.36.1 version --client=true --output=yaml
/tmp/kubectl-v1.36.1 kustomize argoproj/codex-workspace >/tmp/boxp-37-kustomize.yaml
python3 - <<'PY'
from pathlib import Path
text = Path('argoproj/codex-workspace/rbac.yaml').read_text().splitlines()
core_resources = []
in_core_resources = False
verbs_lines = []
for line in text:
    stripped = line.strip()
    if stripped == 'resources:':
        in_core_resources = True
        continue
    if in_core_resources:
        if stripped.startswith('- '):
            core_resources.append(stripped[2:])
            continue
        if stripped.startswith('verbs:'):
            verbs_lines.append(stripped)
            in_core_resources = False
            continue
    if stripped.startswith('verbs:'):
        verbs_lines.append(stripped)
if 'secrets' in core_resources:
    raise SystemExit('core resources include secrets')
for verbs in verbs_lines:
    if verbs != 'verbs: ["get", "list", "watch"]':
        raise SystemExit(f'unexpected verbs line: {verbs}')
print('rbac check ok: core resources exclude secrets; all rules are get/list/watch only')
PY
```

結果:

- `kubectl` client は `v1.36.1`
- bundled kustomize は `v5.8.1`
- `kubectl kustomize argoproj/codex-workspace` は成功し、781 lines の manifest を生成
- RBAC 静的確認で core `secrets` は含まれず、全 rule の verb は `get`, `list`, `watch` のみ

デプロイ後に workspace Pod 内で確認するコマンド:

```bash
kubectl version --client=true --output=yaml
kubectl kustomize --help
kubectl get nodes
kubectl get namespaces
kubectl get pods -A
kubectl get deploy -A
kubectl get services -A
kubectl get configmaps -A
kubectl get events -A
kubectl get crd
kubectl auth can-i get secrets -A
kubectl auth can-i create pod -A
kubectl auth can-i patch deployment -A
```

期待値:

- read 系の代表コマンドは成功する
- `kubectl auth can-i get secrets -A` は `no`
- `kubectl auth can-i create pod -A` は `no`
- `kubectl auth can-i patch deployment -A` は `no`

## 制限事項

- この run から対象 cluster への kubeconfig がないため、実 cluster に対する `kubectl auth can-i` と read 操作は未実施。
- `kubectl apply --dry-run=client --validate=false -f /tmp/boxp-37-kustomize.yaml` は kubeconfig なしの discovery で `localhost:8080` に接続しようとして失敗したため、検証結果には含めない。
- custom resource の read 権限は、リポジトリで利用している API group と主要 Kubernetes 標準 API group を列挙している。新しい CRD group を追加した場合、その group の read 権限追加が必要になる可能性がある。
- `kubectl logs`, `exec`, `port-forward` は今回の範囲外。
