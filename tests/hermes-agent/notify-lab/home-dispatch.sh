#!/bin/sh
# BOXP-546 lab: feed the inbox files a lab run produced to the real Task Board dispatcher
# (docker/codex-workspace/task-board/task_board_events.bb, unmodified) with a fake home on loopback.
# Usage: home-dispatch.sh <evidence-dir> <work-dir>   -> prints what the fake home stored
set -eu
repo=${ARCH_DIR:?Set ARCH_DIR to the full arch checkout}
evidence=$1
work=$2
port=${LAB_HOME_PORT:-18190}
rm -rf "$work"
mkdir -p "$work/state/outbox/pending" "$work/vault"
cp -r "$evidence/vault/." "$work/vault/"
mkdir -p "$work/vault/Boards"
: >"$work/vault/Boards/Task Board.md"
now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
# One outbox event per Task Board event the lab posted (the dispatcher only reads copies of known events).
# The matched IDs contain no whitespace.
# shellcheck disable=SC2013
for id in $(grep -oE 'BOXP-9001-[0-9TZ]+-[a-f0-9]+-done' "$evidence/boxp-notify/journal.jsonl" | sort -u); do
  printf '{:event-id "%s" :ticket-id "BOXP-9001" :event-type "task-board.run-finished" :status :accepted :created-at "%s" :accepted-at "%s"}\n' \
    "$id" "$now" "$now" >"$work/state/outbox/pending/$id.edn"
done
sed -n '/^write_fake_home() {/,/^CLJ$/p' "$repo/tests/codex-workspace/task-board-events-test.sh" | sed '1,2d;$d' >"$work/home.clj"
printf 'home-token\n' >"$work/token"
: >"$work/home.log"
FAKE_PORT=$port FAKE_LOG="$work/home.log" FAKE_MODE_FILE="$work/home-mode" bb "$work/home.clj" >"$work/home.out" 2>&1 &
pid=$!
trap 'kill $pid 2>/dev/null || true' EXIT
for _ in $(seq 1 50); do
  grep -q listening "$work/home.out" 2>/dev/null && break
  sleep 0.2
done
for round in 1 2; do
  CODEX_TASK_BOARD_VAULT="$work/vault" CODEX_TASK_BOARD_ROOT="$work/state" CODEX_TASK_BOARD_EVENT_QUIET_HOURS=off \
    CODEX_TASK_BOARD_EVENT_HOME_URL="http://127.0.0.1:$port/kohako/api/v1/notifications/ingest" \
    CODEX_TASK_BOARD_EVENT_HOME_TOKEN_FILE="$work/token" \
    bb "$repo/docker/codex-workspace/task-board/task_board_events.bb" tick >"$work/tick$round.out" 2>&1 || true
done
cat "$work/home.log"
