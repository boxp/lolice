#!/bin/sh
# BOXP-546 lab: fire the webhook scenarios against a running lab container. Usage: scenarios.sh <name>
set -eu
here=$(cd "$(dirname "$0")" && pwd)
name=$1
n=0
fire() { # scenario [request-id-suffix] [event-suffix]
  scenario=$1
  n=$((n + 1))
  event="BOXP-9001-20261114T0000$(printf '%02d' "$n")Z-aaaa00$(printf '%02d' "$n")-done"
  [ -n "${3:-}" ] && event=$3
  printf '%s %s -> ' "$scenario" "$event"
  "$here/lab.sh" post "$name" task-board "${event}${2:-}" \
    "{\"event_id\":\"$event\",\"event_type\":\"task-board.run-finished\",\"ticket_id\":\"BOXP-9001\",\"scenario\":\"$scenario\"}"
  last=$event
  sleep "${LAB_WAIT:-12}"
}
fire notify
fire notify "" "$last"          # same delivery id: stopped at the webhook ingress
fire notify "-attempt2" "$last" # redelivery with a new delivery id: a second turn runs, the channel must drop it
fire silent
fire receipt-silent
fire receipt-notify
fire stream
fire empty
fire error
LAB_WAIT=45 fire error500
printf 'direct -> '
"$here/lab.sh" post "$name" direct "direct-0001" '{"name":"reminder","firing":"","message":"lab 直送の通知本文"}'
sleep 5
printf 'direct alertmanager -> '
"$here/lab.sh" post "$name" direct "direct-0002" '{"name":"alertmanager","firing":"2","message":"[FIRING:2] lab alert\n本文"}'
sleep 5
printf 'direct alertmanager redelivery -> '
"$here/lab.sh" post "$name" direct "direct-0003" '{"name":"alertmanager","firing":"2","message":"[FIRING:2] lab alert\n本文"}'
sleep 5
