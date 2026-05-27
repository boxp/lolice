# Control-plane node alert

## Goal

Fire a node-level alert when any lolice control-plane node becomes NotReady or Unknown.

## Plan

1. Add a `PrometheusRule` under `argoproj/prometheus-operator`.
2. Match control-plane nodes via `kube_node_role{role=~"control-plane|master"}` so both current and legacy Kubernetes role labels are covered.
3. Match non-ready node state via `kube_node_status_condition{condition="Ready",status!="true"} == 1`, then aggregate it with `max by (node)` so the alert `for` duration survives transitions between `Ready=False` and `Ready=Unknown`.
4. Register the new rule manifest in the prometheus-operator kustomization.
5. Validate the manifest syntax and rendered kustomize output where local tooling allows it.

## Verification

- `npx --yes prettier --check argoproj/prometheus-operator/control-plane-node-rules.yaml docs/project_docs/control-plane-node-alert/plan.md`

## Local tooling notes

- `kubectl`, `kustomize`, `yq`, `yamllint`, `kubeconform`, and `ruby` were not available in the local environment.
- `npx --yes prettier --check argoproj/prometheus-operator/kustomization.yaml` parsed the file but reported existing formatting style differences, so the existing kustomization style was left unchanged.
