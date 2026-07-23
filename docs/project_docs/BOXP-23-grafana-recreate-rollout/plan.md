# B0XP-23 Grafana Recreate rollout

## Goal

Fix Grafana rollouts with the existing `grafana-pvc` RWO volume.

## Problem

Grafana uses `monitoring/grafana-pvc`. With the default Deployment RollingUpdate strategy, dashboard/config changes can create a new Grafana Pod on another node while the old Pod still owns the PVC. This produces a Longhorn multi-attach failure and leaves the rollout stuck.

Observed during local LLM / GPU dashboard rollout:

```text
Multi-Attach error for volume "pvc-9d4507a5-ad0f-47d4-9317-87b8a0c489f9"
Volume is already used by pod(s) grafana-5c44c4fc5-lng9z
```

## Change

Set Grafana Deployment strategy to `Recreate` in the existing prometheus-operator overlay.

## Validation

- `kubectl kustomize argoproj/prometheus-operator`
- rendered Deployment contains:
  - `strategy.type: Recreate`
