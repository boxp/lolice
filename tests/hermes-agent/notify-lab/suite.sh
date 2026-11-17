#!/bin/sh
# BOXP-546 lab: full measurement for one image. Usage: suite.sh <image-tag> <container-name> <evidence-dir>
set -eu
here=$(cd "$(dirname "$0")" && pwd)
tag=$1
name=$2
out=$3
"$here/lab.sh" down "$name"
# LAB_RUN_ARGS is a space-separated argument list that must be split into words.
# shellcheck disable=SC2086
"$here/lab.sh" up "$tag" "$name" ${LAB_RUN_ARGS:-}
sleep 45
[ -n "${LAB_CRON_ONLY:-}" ] || {
  "$here/scenarios.sh" "$name"
  "$here/recovery.sh" "$name"
}
docker exec -u hermes "$name" sh -c 'cd /opt/hermes; export HOME=/opt/data
  ./.venv/bin/hermes cron create --name lab-agent --deliver boxp_notify:cron-lab-agent "every 12h" "cron lab scenario=default" 2>&1 | grep Created
  ./.venv/bin/hermes cron create --name lab-fail --deliver boxp_notify:cron-lab-fail "every 12h" "cron lab scenario=error" 2>&1 | grep Created'
"$here/cron.sh" "$name"
docker exec "$name" sh -c 'cd /opt/hermes && ./.venv/bin/hermes plugins validate /opt/data/plugins/boxp-notify 2>&1 | grep -v "install out of sync" | tail -15' >"$out.validate.txt" 2>&1 || true
rm -rf "$out"
"$here/lab.sh" collect "$name" "$out"
