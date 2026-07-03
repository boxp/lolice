# BOXP-37: Codex workspace kubectl readonly access

## 目的

Codex workspace Pod 内で `kubectl` / `kustomize` を確実に実行できるようにし、Kubernetes cluster に対して secrets を除く read-only 調査だけを許可する。

## 実装内容

- `argoproj/codex-workspace/rbac.yaml` を追加する。
  - `ServiceAccount/codex-workspace`
  - `ClusterRole/codex-workspace-readonly`
  - `ClusterRoleBinding/codex-workspace-readonly`
- `argoproj/codex-workspace/deployment.yaml` を更新する。
  - Pod に `serviceAccountName: codex-workspace` を設定する。
  - `automountServiceAccountToken: false` は維持する。
  - workspace container だけに `/var/run/secrets/kubernetes.io/serviceaccount` を projected volume で mount する。
  - `kubectl v1.36.1` と `kustomize v5.8.1` を init container で `/usr/local/codex-tools` に配置し、SHA-256 を検証した上で workspace container の `PATH` に追加する。
- `argoproj/codex-workspace/kustomization.yaml` に `rbac.yaml` を追加する。

## 権限範囲

許可する verb は `get`, `list`, `watch` のみ。

主な許可 resource:

- core: `pods`, `services`, `configmaps`, `events`, `nodes`, `namespaces`, `persistentvolumeclaims`, `persistentvolumes`, `endpoints`, `serviceaccounts`
- apps: `deployments`, `replicasets`, `daemonsets`, `statefulsets`
- batch: `jobs`, `cronjobs`
- networking: `ingresses`, `networkpolicies`
- storage: `storageclasses`, `csidrivers`, `csinodes`, `volumeattachments`
- CRD: `customresourcedefinitions`
- events.k8s.io: `events`

明示的に許可しないもの:

- `secrets` の `get/list/watch`
- `create`, `update`, `patch`, `delete`, `deletecollection` などの write verb
- `pods/log`, `pods/exec`, `pods/portforward`
- `tokenreviews`, `subjectaccessreviews` などの認証・認可レビュー API

## 検証

この Task Board worker 環境には最初から `kubectl` / `kustomize` が入っていなかったため、一時領域へ manifest と同じ `kubectl v1.36.1` を取得して検証した。

実行済み:

```bash
mkdir -p /tmp/boxp-37-tools
curl -fsSL https://dl.k8s.io/release/v1.36.1/bin/linux/amd64/kubectl -o /tmp/boxp-37-tools/kubectl
chmod 755 /tmp/boxp-37-tools/kubectl
/tmp/boxp-37-tools/kubectl version --client
/tmp/boxp-37-tools/kubectl kustomize argoproj/codex-workspace >/tmp/boxp-37-codex-workspace.yaml
curl -fsSL https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize/v5.8.1/kustomize_v5.8.1_linux_amd64.tar.gz -o /tmp/boxp-37-tools/kustomize.tar.gz
tar -tzf /tmp/boxp-37-tools/kustomize.tar.gz
curl -fsSL https://dl.k8s.io/release/v1.36.1/bin/linux/amd64/kubectl.sha256
curl -fsSL https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize/v5.8.1/checksums.txt | grep 'kustomize_v5.8.1_linux_amd64.tar.gz'
git diff --check
```

結果:

- `kubectl version --client`: `Client Version: v1.36.1`, `Kustomize Version: v5.8.1`
- `kubectl kustomize argoproj/codex-workspace`: 成功、651 行を render
- kustomize archive: `kustomize` binary を含むことを確認
- kubectl checksum: `629d3f410e09bf49b64ae7079f7f0bda1191efed311f7d37fdbab0ad5b0ec2b7`
- kustomize checksum: `029a7f0f4e1932c52a0476cf02a0fd855c0bb85694b82c338fc648dcb53a819d`
- `git diff --check`: 成功

`kubectl apply --dry-run=client --validate=false` は runner の in-cluster API endpoint `https://kubernetes.default.svc` へ接続しようとして timeout したため、今回の worker では未完了。

## 反映後の確認コマンド

Argo CD 反映後、codex-workspace の workspace container で以下を確認する。

```bash
kubectl version --client
kustomize version
kubectl get nodes
kubectl get namespaces
kubectl get pods -A
kubectl get deploy -A
kubectl get svc -A
kubectl get configmaps -A
kubectl get events -A
kubectl get crd
kubectl auth can-i get secrets -A
kubectl auth can-i create pod -A
kubectl auth can-i patch deployment -A
```

期待値:

- read 系の `get` は成功する。
- `kubectl auth can-i get secrets -A` は `no`。
- `kubectl auth can-i create pod -A` は `no`。
- `kubectl auth can-i patch deployment -A` は `no`。

## 制限事項

- CLI は Pod 起動時に public endpoint から取得するため、`dl.k8s.io` と GitHub release への HTTPS egress に依存する。
- `kubectl` の cluster 接続は in-cluster ServiceAccount token に依存する。ローカル kubeconfig は配布しない。
- secret read、write 操作、logs/exec/port-forward はこのチケットの対象外。
