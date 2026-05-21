# BOXP-6: OpenClaw を停止・撤去し、Longhorn backup を保持する

## Summary

OpenClaw が不要になったため、`boxp/lolice` から OpenClaw runtime resources を撤去して Kubernetes / Longhorn の占有リソースを解放する。
Longhorn backup は保持し、`argoproj/longhorn/` と backup target は削除対象にしない。

## Current State

2026-05-22 時点の非破壊確認結果:

- Namespace: `openclaw`
- Running Pods:
  - `cloudflared-66d5df7d7d-hw5xz` (`1/1 Running`)
  - `litellm-b457b9fb9-fpccl` (`1/1 Running`)
  - `openclaw-7bc7d99467-h726k` (`5/5 Running`)
- Services:
  - `openclaw` (`18789/TCP`, `8080/TCP`)
  - `litellm` (`4000/TCP`)
- PVC:
  - `openclaw/openclaw-data` -> PV `pvc-24dd53a6-9edf-4daf-ae23-ee101dd8616c`
  - size: `10Gi`
  - storageClass: `longhorn`
  - reclaimPolicy: `Delete`
- Longhorn volume:
  - `pvc-24dd53a6-9edf-4daf-ae23-ee101dd8616c`
  - state: `attached`
  - robustness: `healthy`
  - node: `golyat-2`
- Longhorn backup volume:
  - `pvc-24dd53a6-9edf-4daf-ae23-ee101dd8616c-55f03aa6`
  - latest backup: `backup-2ccefb59484f48aa`
  - latest backup time: `2026-05-21T00:00:57Z`
  - latest backup state: `Completed`
- observed usage:
  - `litellm`: about 942Mi
  - `openclaw`: about 588Mi
  - `cloudflared`: about 19Mi
  - other OpenClaw sidecars: about 72Mi

## Keep

- `argoproj/longhorn/`
- Longhorn backup target and existing backups
- Other application PVCs and backup volumes

## Remove Or Disable

- `argoproj/openclaw/`
  - `Application/openclaw`
  - `Namespace/openclaw`
  - `Deployment/openclaw`
  - `Deployment/litellm`
  - `Deployment/cloudflared`
  - `PVC/openclaw-data` after backup/retain decision
  - OpenClaw/LiteLLM ExternalSecrets, Services, ConfigMaps, NetworkPolicies
- `argoproj/argocd-image-updater/imageupdaters/openclaw.yaml`
- OpenClaw dashboards and mounts:
  - `argoproj/prometheus-operator/grafana-dashboard-openclaw.yaml`
  - `argoproj/prometheus-operator/grafana-dashboard-openclaw-app.yaml`
  - OpenClaw dashboard volumes/mounts in `argoproj/prometheus-operator/overlays/grafana.yaml`
- OpenClaw cross-namespace network policy exceptions:
  - Prometheus/Grafana policies that reference namespace `openclaw`
- OpenClaw-specific docs that are no longer useful after removal

## Acceptance Criteria

- [x] `openclaw-data` Longhorn backup の存在を確認する。
- [x] `openclaw-data` の PVC/PV は Retain せず、Longhorn backup のみを保持して manifests から削除する。
- [x] OpenClaw ArgoCD Application と manifests を削除する。
- [x] ArgoCD Image Updater の OpenClaw entry を削除する。
- [x] OpenClaw Grafana dashboards と dashboard mounts を削除する。
- [x] OpenClaw 用 NetworkPolicy 例外を削除する。
- [x] `argoproj/longhorn/` と Longhorn backup target に変更を入れない。
- [ ] 適用後、`openclaw` namespace の Pod/PVC が残っていない、または意図した Retain 状態だけが残っている。
- [ ] 適用後、Longhorn backup volume `pvc-24dd53a6-9edf-4daf-ae23-ee101dd8616c-55f03aa6` を参照できる。

## Plan

- [x] PVC/PV retain 方針を決める。
  - 現在の PV reclaimPolicy は `Delete`。
  - backup のみを保持し、PVC/PV は GitOps prune で削除して占有 storage を解放する。
- [x] manifests を削除する。
- [x] kustomize/ArgoCD の参照が残らないことを確認する。
- [x] monitoring resources から OpenClaw 参照を削る。
- [x] `kubectl kustomize argoproj` で生成確認する。
- [ ] arch 側 PR と適用順序を合わせる。

## Notes

- チケット: [[Tickets/BOXP-6]]
- arch 側 plan: `boxp/arch:docs/project_docs/BOXP-6-retire-openclaw-keep-longhorn-backup/plan.md`
- `board.b0xp.io` は OpenClaw の `board-server` sidecar と arch 側 Cloudflare tunnel に同居しているため、必要なら別途移行が必要。
- 2026-05-22 の実装結果:
  - `argoproj/openclaw/` を削除。
  - `argoproj/argocd-image-updater/imageupdaters/openclaw.yaml` と参照を削除。
  - OpenClaw Grafana dashboards と dashboard mounts を削除。
  - Prometheus/Grafana の OpenClaw namespace 向け NetworkPolicy 例外を削除。
  - OpenClaw 運用 docs と Renovate の OpenClaw packageRule を削除。
- 検証:
  - `kubectl kustomize argoproj`
  - `kubectl kustomize argoproj/argocd-image-updater`
  - `kubectl kustomize argoproj/prometheus-operator`
  - `jq . renovate.json`
