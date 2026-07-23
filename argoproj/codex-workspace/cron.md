# Codex Workspace Cron

Codex workspace の prompt 定期実行は、workspace Pod 内の resident scheduler が管理する。
OpenClaw の cron と同じく、複数の job がそれぞれ別の schedule と prompt を持てる。

## 仕組み

- `deployment.yaml`
  - `codex-cron-scheduler` sidecar が `/opt/codex-workspace/cron/scheduler.bb` を常駐実行する。
  - scheduler と workspace container は同じ home PVC を mount する。
- `/home/boxp/Documents/obsidian-headless/BOXP/Infrastructure/Codex Cron/jobs.edn`
  - live job registry。Obsidian vault 上の実体を Codex skill から CRUD する。
- `/home/boxp/Documents/obsidian-headless/BOXP/Infrastructure/Codex Cron/prompts/*.md`
  - job ごとの prompt。
- `/home/boxp/Documents/obsidian-headless/BOXP/Infrastructure/Codex Cron/runs/<job>/<run-id>/`
  - `events.jsonl`, `stderr.log`, `last-message.md`, `summary.edn` を保存する。

## Registry 例

```clojure
{:version 1
 :jobs [{:id "workspace-maintenance"
         :name "Workspace maintenance"
         :enabled false
         :schedule "0 22 * * *"
         :time-zone "Etc/UTC"
         :prompt-file "prompts/workspace-maintenance.md"
         :workdir "/home/boxp"
         :output-root "/home/boxp/Documents/obsidian-headless/BOXP/Infrastructure/Codex Cron/runs"
         :bypass-approvals true}]}
```

## 操作

live job は Codex workspace 内の skill helper で操作する。

```bash
bb ~/.codex/skills/codex-workspace-cron/scripts/codex_cron_jobs.bb list
bb ~/.codex/skills/codex-workspace-cron/scripts/codex_cron_jobs.bb add \
  --id daily-report \
  --name "Daily report" \
  --schedule "0 22 * * *" \
  --prompt "調査して日本語で報告してください。"
bb ~/.codex/skills/codex-workspace-cron/scripts/codex_cron_jobs.bb enable daily-report
```

## OpenClaw との差分

OpenClaw は Gateway 内蔵 scheduler が home 配下の job registry を管理する。
Codex workspace では Gateway ではなく Pod sidecar として scheduler を常駐させ、Kubernetes CronJob は個別 schedule の source of truth にしない。
