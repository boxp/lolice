# BOXP-16 Codex Cron Home Scheduler Plan

## Goal

OpenClaw の cron に近い形で、Codex workspace の home PVC 配下に複数 job の schedule と prompt を保持し、workspace Pod 内の常駐 scheduler がそれを実行する。

## Changes

1. Kubernetes CronJob と ConfigMap job registry を source of truth から外す。
2. `codex-workspace` Deployment に `codex-cron-scheduler` sidecar を追加する。
3. scheduler は `/home/boxp/.codex-cron/jobs.edn` を読み、同じ PVC 上の prompt と run log を使う。
4. `cron.md` を home registry と resident scheduler 前提の運用説明に更新する。

## Validation

- `kustomize build argoproj/codex-workspace`
- `git diff --check`
