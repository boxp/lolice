# Codex Workspace Cron

Codex workspace の prompt 定期実行は `jobs.yaml` と Kubernetes CronJob で管理する。
OpenClaw の `jobs.json` と同じく、複数のジョブがそれぞれ schedule と prompt を持つ。

## 仕組み

- `cron-configmap.yaml`
  - `jobs.yaml`: 登録済み cron job の一覧。
  - `run-codex-cron.sh`: `codex exec` を非対話で起動する runner。
  - `prompt-*.md`: 定期実行したい prompt。
- `cronjob.yaml`
  - `spec.schedule`: Kubernetes が実行する schedule。`jobs.yaml` の同名 entry と合わせる。
  - `spec.suspend`: 誤実行防止。初期値は `true`。
  - `CODEX_CRON_JOB_ID`: `jobs.yaml` の job id。
- 実行ログ
  - `/home/boxp/.codex-cron/runs/<job>/<run-id>/events.jsonl`
  - `/home/boxp/.codex-cron/runs/<job>/<run-id>/stderr.log`
  - `/home/boxp/.codex-cron/runs/<job>/<run-id>/last-message.md`
  - `/home/boxp/.codex-cron/runs/<job>/<run-id>/summary.json`

## 有効化

prompt と schedule を確認してから、`jobs.yaml` の対象 job を `enabled: true` にし、対応する CronJob の `suspend` を `false` にする。

```yaml
jobs:
  - id: workspace-maintenance
    enabled: true
    schedule: "0 22 * * *"
```

```yaml
spec:
  suspend: false
  schedule: "0 22 * * *"
```

## 手動実行

```bash
kubectl create job \
  --from=cronjob/codex-cron-workspace-maintenance \
  codex-cron-workspace-maintenance-manual \
  -n codex-workspace
```

## ジョブ追加

1. `cron-configmap.yaml` の `jobs.yaml` に job entry を追加する。
2. `cron-configmap.yaml` に対応する `prompt-<name>.md` を追加する。
3. `cronjob.yaml` をコピーして CronJob 名、`schedule`、`CODEX_CRON_JOB_ID` を変更する。
4. `jobs.yaml` の `id/schedule/timeZone` と CronJob の `CODEX_CRON_JOB_ID/schedule/timeZone` を揃える。
5. 多重実行を避けるため `concurrencyPolicy: Forbid` は維持する。
6. workspace home PVC を mount するため、workspace Pod と同じ node へ寄せる `podAffinity` は維持する。

## OpenClaw との差分

OpenClaw は Gateway 内蔵 scheduler が `jobs.json` と `jobs-state.json` を管理する。Codex workspace では Gateway 相当の常駐 scheduler を持たないため、scheduler は Kubernetes CronJob に委譲し、ジョブ登録情報は ConfigMap の `jobs.yaml` に集約する。Runner 補助スクリプトは workspace image に入っている Babashka (`bb`) で動かす。
