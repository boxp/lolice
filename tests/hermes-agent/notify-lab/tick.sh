#!/bin/sh
# BOXP-554 lab: run the real Task Board dispatcher (unmodified) inside the lab container.
# Usage: tick.sh <tick|record ...|list>   LAB_ROUTE picks the webhook route, LAB_QUIET the quiet hours.
export CODEX_TASK_BOARD_ROOT=/lab/tb CODEX_TASK_BOARD_VAULT=/lab/vault \
  CODEX_TASK_BOARD_EVENT_WEBHOOK_URL="http://127.0.0.1:8644/webhooks/${LAB_ROUTE:-task-board-channel}" \
  CODEX_TASK_BOARD_EVENT_WEBHOOK_SECRET=lab-secret \
  CODEX_TASK_BOARD_HOME_REMINDER_URL=http://127.0.0.1:8644/webhooks/kohako-home-reminder \
  CODEX_TASK_BOARD_EVENT_HOME_URL=http://127.0.0.1:18190/kohako/api/v1/notifications/ingest \
  CODEX_TASK_BOARD_EVENT_HOME_TOKEN_FILE=/lab/token \
  CODEX_TASK_BOARD_EVENT_QUIET_HOURS="${LAB_QUIET:-off}" \
  CODEX_TASK_BOARD_HOME_REMINDER_MINUTES=1 CODEX_TASK_BOARD_EVENT_DIGEST_CLAIM_GRACE_SECONDS=5 \
  CODEX_TASK_BOARD_EVENT_HANDLE_TIMEOUT_SECONDS="${LAB_HANDLE_TIMEOUT:-1800}"
exec bb /lab/task_board_events.bb "$@"
