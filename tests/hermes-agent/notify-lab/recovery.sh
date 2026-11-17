#!/bin/sh
# BOXP-546 lab: fallback / failure / restart scenarios. Usage: recovery.sh <name>
set -eu
here=$(cd "$(dirname "$0")" && pwd)
name=$1
x() { docker exec "$name" sh -c "$1"; }
fire() { # scenario event
  printf '%s %s -> ' "$1" "$2"
  "$here/lab.sh" post "$name" task-board "$2" \
    "{\"event_id\":\"$2\",\"event_type\":\"task-board.run-finished\",\"ticket_id\":\"BOXP-9001\",\"scenario\":\"$1\"}"
}
mark() {
  x "printf '{\"at\":\"%s\",\"kind\":\"lab_step\",\"step\":\"$1\"}\n' \"\$(date -u +%FT%TZ)\" >> /opt/data/boxp-notify/journal.jsonl"
  echo "# $1"
}
ev() { echo "BOXP-9001-20261114T0001$1Z-bbbb00$1-done"; }

mark "R1 home unwritable -> telegram fallback"
x 'touch /opt/data/boxp-notify/lab-fail-home'
fire notify "$(ev 01)"
sleep 12

mark "R2 home and telegram both fail -> pending; does upstream call send again?"
x 'echo "{\"telegram_fail\":true}" > /lab/logs/control.json'
fire notify "$(ev 02)"
sleep 45
mark "R2 home restored -> channel recovery"
x 'rm -f /opt/data/boxp-notify/lab-fail-home'
sleep 15

mark "R3 both fail, channel recovery off, then restart: does upstream redeliver?"
x 'touch /opt/data/boxp-notify/lab-fail-home /opt/data/boxp-notify/lab-no-recovery'
fire notify "$(ev 03)"
sleep 20
docker restart "$name" >/dev/null
"$here/lab.sh" stub "$name"
sleep 75
mark "R3 failures cleared, channel recovery on"
x 'rm -f /opt/data/boxp-notify/lab-fail-home /opt/data/boxp-notify/lab-no-recovery; echo "{}" > /lab/logs/control.json'
sleep 15

mark "R4 hard kill in the middle of a turn, then start: is the turn resumed?"
x 'echo "{\"slow_seconds\":25}" > /lab/logs/control.json'
fire slow "$(ev 04)"
sleep 8
docker kill -s KILL "$name" >/dev/null
docker start "$name" >/dev/null
"$here/lab.sh" stub "$name"
sleep 100
mark "end"
