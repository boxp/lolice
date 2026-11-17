#!/bin/sh
# BOXP-546 lab: cron -> channel. Usage: cron.sh <name>   (jobs lab-agent / lab-fail must exist)
set -eu
name=$1
h() { docker exec -u hermes "$name" sh -c "cd /opt/hermes; HOME=/opt/data ./.venv/bin/hermes $1 2>&1 | grep -v 'install out of sync'"; }
ids=$(h "cron list" | awk '/\[active\]/{print $1}')
for _ in 1 2; do
  for id in $ids; do h "cron run $id" | tail -1; done
  sleep 100
done
