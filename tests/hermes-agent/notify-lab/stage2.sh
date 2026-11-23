#!/bin/sh
# BOXP-554 / BOXP-556 lab: stage 2 (Task Board and cron on boxp_notify) and stage 3 (no core patch: old route
# gone, vault image plugin, cron deliver rewrite) on a Hermes image, laid out by the
# bootstrap init container's own script, with the real dispatcher as the event source and a fake home.
# --network none; the LLM and Telegram are the loopback stub.
# Usage: stage2.sh build <tag> <base-image> <lolice/argoproj/hermes-agent>
#        stage2.sh run <tag> <container-name> <evidence-dir>      (about 12 minutes)
set -eu
here=$(cd "$(dirname "$0")" && pwd)
cmd=$1
shift
if [ "$cmd" = build ]; then
  tag=$1
  base=$2
  src=${3:-"$here/../../../argoproj/hermes-agent"}
  ctx=$(mktemp -d "${LAB_TMP:-/tmp}/stage2-ctx.XXXXXX")
  cp "$here/Dockerfile.stage2" "$ctx/Dockerfile"
  cp "$here/stub.bb" "$here/home.bb" "$here/tick.sh" "$ctx/"
  bb "$here/stage2-context.bb" "$src" "${4:-${ARCH_DIR:?Set ARCH_DIR to the arch checkout}}" "$ctx"
  DOCKER_BUILDKIT=0 docker build -q --build-arg "BASE=$base" -t "$tag" "$ctx"
  rm -rf "$ctx"
  exit 0
fi
tag=$1
name=$2
out=$3
x() { docker exec -u hermes "$name" sh -c "$1"; }
mark() {
  x "printf '{\"at\":\"%s\",\"kind\":\"lab_step\",\"step\":\"$1\"}\n' \"\$(date -u +%FT%TZ)\" >> /opt/data/boxp-notify/journal.jsonl"
  echo "# $1"
}
init() { # the Pod's init container: same image, root, same script
  docker run --rm --network none -u 0 --entrypoint sh -e ALERTMANAGER_WEBHOOK_SECRET=lab-secret \
    -v "$name-data:/opt/data" -v "$name-lab:/lab" "$tag" /lab/init.sh 2>&1 | grep '^\[bootstrap\]' || true
}
start() {
  "$here/lab.sh" stub "$name"
  docker exec -d -u hermes "$name" sh -c 'bb /lab/home.bb >> /lab/logs/home.out 2>&1'
}
tick() { docker exec -u hermes -e LAB_QUIET="${LAB_QUIET:-off}" -e LAB_ROUTE="${LAB_ROUTE:-task-board-channel}" "$name" /lab/tick.sh tick | tail -1; }
n=0
event() { # scenario [to-status] -> records one run-finished event on its own ticket
  n=$((n + 1))
  ticket="BOXP-91$(printf '%02d' "$n")"
  run="20261114T1200$(printf '%02d' "$n")Z-aaaa00$(printf '%02d' "$n")"
  x "printf '# %s: lab ticket %s\n\n## Notes\n\n' $ticket '$1' > /lab/vault/Tickets/$ticket.md"
  docker exec -u hermes "$name" /lab/tick.sh record "$ticket" "$run" in-progress "${2:-done}" "scenario=$1" >/dev/null
  last="$ticket-$run-${2:-done}"
  echo "event $last ($1)"
}
routing() { x "mkdir -p /lab/vault/Projects/task-board-events && printf '# lab\n\ntask-board: %s\n' $1 > /lab/vault/Projects/task-board-events/delivery-routing.md"; }

"$here/lab.sh" down "$name"
docker volume create "$name-data" >/dev/null
docker volume create "$name-lab" >/dev/null
echo "# init (first Pod start)"
init
echo "# init again (a normal Pod re-creation must change nothing)"
init
"$here/lab.sh" up "$tag" "$name"
docker exec -d -u hermes "$name" sh -c 'bb /lab/home.bb >> /lab/logs/home.out 2>&1'
routing home
sleep 60
mark "T1 formal report, home primary -> home inbox only, dispatcher closes it, reminder after 1 minute unread"
event receipt-notify
t1=$last
tick
sleep 20
tick
mark "T2 the same event delivered again (new delivery id) -> nothing more"
x "curl -sS -o /dev/null -w 'redelivery http=%{http_code}\n' -X POST http://127.0.0.1:8644/webhooks/task-board-channel -H 'Content-Type: application/json' -H 'X-Gitlab-Token: lab-secret' -H 'X-Request-ID: $t1-again' -H 'X-GitHub-Event: task-board.run-finished' -d '{\"event_id\":\"$t1\",\"event_type\":\"task-board.run-finished\",\"ticket_id\":\"BOXP-9101\",\"result\":\"scenario=notify\"}'"
sleep 15
mark "T3 silence without the Notes marker -> one notice on Telegram, home copy"
event silent
tick
sleep 20
tick
mark "T4 silence with the Notes marker -> nothing"
event receipt-silent
tick
sleep 20
tick
mark "T5 model error -> one notice"
event error
tick
sleep 30
tick
mark "T6 reply arrives after quiet hours began -> held, then the morning digest carries it"
x 'echo "{\"slow_seconds\":25}" > /lab/logs/control.json'
event slow-notify
tick
sleep 8
LAB_QUIET=0-24 tick
sleep 35
LAB_QUIET=0-24 tick
mark "T6 morning: the dispatcher builds the digest"
tick
sleep 8
tick
sleep 25
tick
mark "T7 home inbox unwritable -> Telegram carries the report; copy and receipt follow"
x 'touch /opt/data/boxp-notify/lab-fail-home'
event receipt-notify
tick
sleep 20
x 'rm -f /opt/data/boxp-notify/lab-fail-home'
sleep 25
tick
mark "T8 routing file switched to telegram (type-level rollback) -> Telegram primary, home copy without reminder"
routing telegram
event receipt-notify
tick
sleep 20
tick
routing home
mark "T9 inbox and Telegram both down, gateway restart -> recovery"
x 'touch /opt/data/boxp-notify/lab-fail-home; echo "{\"telegram_fail\":true}" > /lab/logs/control.json'
event receipt-notify
tick
sleep 25
docker restart "$name" >/dev/null
start
sleep 60
x 'rm -f /opt/data/boxp-notify/lab-fail-home; echo "{}" > /lab/logs/control.json'
sleep 25
tick
if x 'grep -q "\"task-board\":" /opt/data/webhook_subscriptions.json'; then
  mark "T10 the old route task-board still works through the core patch (rollback path), the channel is not involved"
  event receipt-notify
  LAB_ROUTE=task-board tick
  sleep 20
  LAB_ROUTE=task-board tick
else
  mark "T10 (BOXP-556) the old route task-board is gone: a post to it is refused and nothing is delivered"
  x "curl -sS -o /dev/null -w 'old-route http=%{http_code}\n' -X POST http://127.0.0.1:8644/webhooks/task-board -H 'Content-Type: application/json' -H 'X-Gitlab-Token: lab-secret' -H 'X-Request-ID: old-route-1' -H 'X-GitHub-Event: task-board.run-finished' -d '{\"event_id\":\"BOXP-9199-20261114T120099Z-aaaa0099-done\",\"event_type\":\"task-board.run-finished\",\"ticket_id\":\"BOXP-9199\",\"result\":\"scenario=notify\"}'"
  sleep 10
fi
mark "T1 reminder: unread for over a minute"
sleep 5
tick
mark "C1 cron job on the channel -> home and Telegram"
x 'cd /opt/hermes; ./.venv/bin/hermes cron create --name "lab 定期報告" --deliver boxp_notify:cron "every 12h" "cron lab scenario=cron-report" 2>&1 | grep Created
   ./.venv/bin/hermes cron create --name lab-fail --deliver boxp_notify:cron "every 12h" "cron lab scenario=error" 2>&1 | grep Created'
h() { x "cd /opt/hermes; ./.venv/bin/hermes $1 2>&1 | grep -v 'install out of sync'"; }
ids=$(h "cron list" | awk '/\[active\]/{print $1}')
for id in $ids; do h "cron run $id" | tail -1; done
sleep 100
mark "C2 the same jobs run again 100 seconds later (same text) -> a retry of one run; then after the window -> new"
for id in $ids; do h "cron run $id" | tail -1; done
sleep 100
tick
mark "A1 Alertmanager (stage 1) unchanged: firing, the same payload again, resolved"
alert() { printf '{"version":"4","groupKey":"{}:{alertname=\\"Lab\\"}","status":"%s","receiver":"hermes","externalURL":"https://alertmanager.b0xp.io","alerts":[{"status":"%s","labels":{"alertname":"LabStage2Alert","severity":"critical","namespace":"lab","notify_hermes":"true"},"annotations":{"summary":"stage2 lab","description":"x"},"startsAt":"2026-11-14T03:00:00Z","endsAt":"%s","fingerprint":"bbbb000100000000"}]}' "$1" "$1" "$2"; }
"$here/lab.sh" post "$name" alertmanager a1 "$(alert firing 0001-01-01T00:00:00Z)"
sleep 6
"$here/lab.sh" post "$name" alertmanager a2 "$(alert firing 0001-01-01T00:00:00Z)"
sleep 6
"$here/lab.sh" post "$name" alertmanager a3 "$(alert resolved 2026-11-14T03:05:00Z)"
sleep 8
tick
if x 'test -d /opt/data/plugins/kohako-vault-image'; then
  mark "V1 (BOXP-556) vault image route from the plugin: no key 401, image 200, traversal and non-image 404"
  x 'mkdir -p /lab/vault/attachments && printf "\211PNG\r\n\032\nlab" > /lab/vault/attachments/lab.png && printf "secret" > /lab/vault/attachments/note.png'
  v() { x "curl -sS -o /tmp/v.out -w '$1 http=%{http_code} ' $2 'http://127.0.0.1:8642/v1/kohako/vault-image?path=$3'; head -c 60 /tmp/v.out; echo"; }
  v no-key "" attachments/lab.png
  v wrong-key "-H 'Authorization: Bearer nope'" attachments/lab.png
  v image "-H 'Authorization: Bearer lab-api-key-0123456789abcdef0123456789abcdef'" attachments/lab.png
  v traversal "-H 'Authorization: Bearer lab-api-key-0123456789abcdef0123456789abcdef'" ../etc/passwd.png
  v fake-image "-H 'Authorization: Bearer lab-api-key-0123456789abcdef0123456789abcdef'" attachments/note.png
  x 'grep -h "kohako-vault-image" /opt/data/logs/*.log | tail -2' || true
  mark "K1 (BOXP-556) the agent creates cron jobs from the CLI: the home Telegram becomes the channel, no target (origin) and local stay"
  for sc in cron-create cron-create-telegram cron-create-local; do
    x "cd /opt/hermes; timeout 120 ./.venv/bin/hermes chat -Q -t cronjob -q 'lab scenario=$sc' 2>&1 | grep -v 'install out of sync' | tail -1" || echo "chat $sc failed"
  done
  mark "K2 (BOXP-556) jobs stored with a Telegram target (hermes cron create) are moved by the recovery loop"
  x 'cd /opt/hermes; ./.venv/bin/hermes cron create --name lab-cli-telegram --deliver telegram:1000000001 "every 12h" "cron lab scenario=cron-report" 2>&1 | grep Created
     ./.venv/bin/hermes cron create --name lab-cli-fd --deliver boxp_notify:cron --failure-deliver telegram:1000000001 "every 12h" "cron lab scenario=cron-report" 2>&1 | grep Created'
  sleep 30
  x 'grep cron_job_migrat /opt/data/boxp-notify/journal.jsonl' || echo "no migration journal"
  x 'python3 -c "
import json
for j in json.load(open(\"/opt/data/cron/jobs.json\"))[\"jobs\"]:
    print(\"job\", j.get(\"name\"), \"deliver=\" + str(j.get(\"deliver\")), \"failure_deliver=\" + str(j.get(\"failure_deliver\")))"'
  x 'grep cron_deliver_rewritten /opt/data/boxp-notify/journal.jsonl' || echo "no rewrite journal"
  mark "P1 (BOXP-556) plugin validation of every managed plugin on this image"
  for p in kohako-g2-context kohako-g2-realtime kohako-vault-image boxp-notify boxp-ai-ops-observer boxp-langfuse-observer; do
    x "cd /opt/hermes; ./.venv/bin/hermes plugins validate /opt/data/plugins/$p 2>&1 | grep -E '^(✗|Validation)' | tr '\n' ' '; echo ' <- $p'" || true
  done
  # The command is expanded on the remote host.
  # shellcheck disable=SC2016
  x 'cd /opt/hermes; ./.venv/bin/hermes --version 2>&1 | grep -m1 "Hermes Agent"; echo "core files carrying a boxp patch: $(grep -l "boxp_" gateway/*.py gateway/platforms/*.py cron/*.py | wc -l)"'
fi
mark "end"
docker exec "$name" sh -c 'cd /opt/hermes && ./.venv/bin/hermes plugins validate /opt/data/plugins/boxp-notify 2>&1 | grep -v "install out of sync" | tail -15' >"$out.validate.txt" 2>&1 || true
rm -rf "$out"
"$here/lab.sh" collect "$name" "$out"
docker cp -q "$name:/lab/tb" "$out/dispatcher"
docker cp -q "$name:/opt/data/webhook_subscriptions.json" "$out/webhook_subscriptions.json"
docker exec "$name" sh -c 'grep -n -A4 "^display:" /opt/data/config.yaml; ls /opt/data/task-board-delivery-receipts 2>/dev/null | wc -l' >"$out.state.txt" 2>&1 || true

# BOXP-674 baseline: unchanged waits for a paired CI measurement.
