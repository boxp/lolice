#!/usr/bin/env bash
# BOXP-735: @b0xp2 の新着公開投稿を毎日 18:00 JST に確かめる cron job を Hermes に登録する（同名があれば更新）。
# 配備（Argo CD 同期で kohako_x_daily.py が /opt/data/scripts に置かれた）後に一度だけ実行する。
#   argoproj/hermes-agent/scripts/kohako-x-daily/register-job.sh [--dry-run]
# LLM は起動しない（no_agent、script だけ）。配送は local・失敗の配送も local（通知しない）。
# 同じ script を使う job が別名で残っていたら登録を止める（重複ジョブを作らない）。
set -euo pipefail
NS=hermes-agent
POD=$(kubectl get pods -n "$NS" -l app=hermes-agent -o jsonpath='{.items[0].metadata.name}')
DRY=${1:-}
kubectl exec -i -n "$NS" "$POD" -c hermes-agent -- env DRY="$DRY" /command/s6-setuidgid hermes sh -c 'cd /opt/hermes && exec python3 -I -c "$0"' '
import os, sys
sys.path.insert(0, "/opt/hermes")
from cron.jobs import create_job, list_jobs, update_job
NAME = "Kohako X 新着確認（@b0xp2・毎日18時・通知なし）"
SCRIPT = "kohako_x_daily.py"
fields = dict(prompt="", schedule="0 18 * * *", deliver="local", failure_deliver="local",
              script=SCRIPT, no_agent=True)
jobs = list_jobs(include_disabled=True)
same = [j for j in jobs if j.get("name") == NAME]
others = [j["id"] for j in jobs if j.get("script") == SCRIPT and j.get("name") != NAME]
if others or len(same) > 1:
    sys.exit("refuse: duplicate jobs for %s: same-name=%s other=%s" % (SCRIPT, [j.get("id") for j in same], others))
if os.environ.get("DRY"):
    print("dry-run:", "update" if same else "create", NAME, {k: v for k, v in fields.items() if k != "prompt"})
    sys.exit(0)
if same:
    # 時刻は本人が変えていることがあるので触らない。
    job = update_job(same[0]["id"], {k: v for k, v in fields.items() if k != "schedule"})
else:
    job = create_job(name=NAME, timezone="Asia/Tokyo", **fields)
print(job["id"], job["name"], job.get("schedule_display"), job.get("deliver"), job.get("failure_deliver"),
      job.get("no_agent"), job.get("script"), job.get("timezone"), job.get("next_run_at"))
'
