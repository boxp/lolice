#!/usr/bin/env bash
# BOXP-599: ナビログの夜の cron job を Hermes に登録（同名があれば prompt 等を更新）する。
# 配備（Argo CD 同期で kohako_navilog_source.py が /opt/data/scripts に置かれた）後に一度だけ実行する。
#   argoproj/hermes-agent/scripts/kohako-navilog/register-job.sh [--dry-run]
# 道具はファイル操作だけ（enabled_toolsets=file）。配送は local（通知しない）。失敗の通知も local。
set -euo pipefail
cd "$(dirname "$0")"
NS=hermes-agent
POD=$(kubectl get pods -n "$NS" -l app=hermes-agent -o jsonpath='{.items[0].metadata.name}')
DRY=${1:-}
kubectl exec -i -n "$NS" "$POD" -c hermes-agent -- env DRY="$DRY" sh -c 'cd /opt/hermes && exec python3 -I -c "$0"' '
import os, sys
sys.path.insert(0, "/opt/hermes")
from cron.jobs import create_job, list_jobs, update_job
NAME = "Kohako ナビログ（夜のまとめ・通知なし）"
prompt = sys.stdin.read()
fields = dict(prompt=prompt, schedule="30 23 * * *", deliver="local", failure_deliver="local",
              script="kohako_navilog_source.py", enabled_toolsets=["file"])
same = [j for j in list_jobs(include_disabled=True) if j.get("name") == NAME]
if os.environ.get("DRY"):
    print("dry-run:", "update" if same else "create", NAME, {k: v for k, v in fields.items() if k != "prompt"})
    sys.exit(0)
if same:
    # 時刻は本人が変えていることがあるので触らない（prompt・道具・配送・前処理だけ揃える）。
    job = update_job(same[0]["id"], {k: v for k, v in fields.items() if k != "schedule"})
else:
    job = create_job(name=NAME, timezone="Asia/Tokyo", **fields)
print(job["id"], job["name"], job.get("schedule_display") or job.get("schedule"), job.get("deliver"),
      job.get("enabled_toolsets"), job.get("script"), job.get("next_run_at"))
' < kohako_navilog_prompt.md
