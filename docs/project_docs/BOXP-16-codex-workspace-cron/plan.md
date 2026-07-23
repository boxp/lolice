# BOXP-16: Codex workspace にプロンプト定期実行環境を作る

## Context

OpenClaw 公式リポジトリ (`https://github.com/openclaw/openclaw`) の cron 実装を確認した。

- CLI は `openclaw cron create <schedule> <prompt>` / `openclaw cron add` を提供する。
- cron は Gateway 内蔵 scheduler として動く。
- ジョブ定義は `~/.openclaw/cron/jobs.json` に永続化される。
- 実行状態は `jobs-state.json`、履歴は `runs/<jobId>.jsonl` に分離される。
- isolated 実行ではジョブごとに新しい session を使い、delivery は announce/webhook/none を持つ。

Codex workspace には OpenClaw Gateway 相当の常駐 scheduler がないため、Kubernetes CronJob を scheduler とし、Codex CLI の非対話実行 (`codex exec`) を runner として使う。ただし OpenClaw の `jobs.json` に近い登録体験を残すため、複数 job の定義は ConfigMap の `jobs.yaml` に集約する。

## Design

- `argoproj/codex-workspace` に reusable runner ConfigMap を追加する。
- 複数 job は ConfigMap の `jobs.yaml` に `id/name/enabled/schedule/timeZone/session/promptFile/workdir/model` として登録する。
- prompt は ConfigMap の `prompt-*.md` として GitOps 管理する。
- スケジュール実行は Kubernetes `CronJob.spec.schedule` として管理し、CronJob は `CODEX_CRON_JOB_ID` で `jobs.yaml` の entry を選ぶ。
- Runner の job selector は workspace image に含まれる Babashka (`bb`) で実行する。
- CronJob は `ghcr.io/boxp/arch/codex-workspace:latest` を使い、既存 workspace と同じ home PVC を mount する。
- RWO PVC を既存 workspace Pod と同じ node で mount できるよう、workspace Pod への pod affinity を設定する。
- 実行履歴は `/home/boxp/.codex-cron/runs/<job>/<run-id>/` に保存する。
- 実行中の多重起動は `concurrencyPolicy: Forbid` と runner lock で抑止する。
- CronJob 用の Calico NetworkPolicy を追加し、DNS、Grafana、外部 22/80/443 のみ許可する。

## Acceptance Criteria

- [x] Codex workspace の manifests に prompt 定期実行用 CronJob を追加する。
- [x] 複数 job を `jobs.yaml` に登録し、それぞれ別 schedule/prompt で実行できる構造にする。
- [x] prompt と schedule を GitOps で変更できる。
- [x] Codex 実行ログ、stderr、最後の応答、summary を PVC 上に保存する。
- [x] 同一ジョブの多重実行を避ける。
- [x] 設定・有効化・手動実行手順を manifest 近くに文書化する。
- [x] `kustomize build argoproj/codex-workspace` が成功する。

## Notes

- 初期 CronJob は誤課金や意図しない自動作業を避けるため `suspend: true` にしておく。
- 有効化する場合は対象 CronJob の `suspend: false` と prompt 内容を明示的に変更する。
