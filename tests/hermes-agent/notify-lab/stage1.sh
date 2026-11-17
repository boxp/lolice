#!/bin/sh
# BOXP-553 lab: the stage 1 path (Alertmanager -> relay script -> boxp_notify -> home inbox + Telegram) on the
# production image, with the lolice manifests as they will be deployed. --network none; Telegram is the stub.
# Usage: stage1.sh build <tag> <base-image> <lolice/argoproj/hermes-agent>
#        stage1.sh run <tag> <container-name> <evidence-dir>      (about 6 minutes)
set -eu
here=$(cd "$(dirname "$0")" && pwd)
cmd=$1
shift
if [ "$cmd" = build ]; then
  tag=$1
  base=$2
  src=${3:-"$here/../../../argoproj/hermes-agent"}
  ctx=$(mktemp -d "${LAB_TMP:-/tmp}/stage1-ctx.XXXXXX")
  cp "$here/Dockerfile.stage1" "$ctx/Dockerfile"
  cp "$here/stub.bb" "$ctx/"
  bb "$here/stage1-context.bb" "$src" "$ctx"
  DOCKER_BUILDKIT=0 docker build -q --build-arg "BASE=$base" -t "$tag" "$ctx"
  rm -rf "$ctx"
  exit 0
fi
tag=$1
name=$2
out=$3
x() { docker exec "$name" sh -c "$1"; }
mark() {
  x "printf '{\"at\":\"%s\",\"kind\":\"lab_step\",\"step\":\"$1\"}\n' \"\$(date -u +%FT%TZ)\" >> /opt/data/boxp-notify/journal.jsonl"
  echo "# $1"
}
alert() { # fingerprint status severity
  end=0001-01-01T00:00:00Z
  [ "$2" = firing ] || end=2026-11-14T03:05:00Z
  printf '{"version":"4","groupKey":"{}:{alertname=\\"Lab%s\\"}","status":"%s","receiver":"hermes","externalURL":"https://alertmanager.b0xp.io","alerts":[{"status":"%s","labels":{"alertname":"LabStage1Alert","severity":"%s","namespace":"lab","notify_hermes":"true"},"annotations":{"summary":"stage1 lab %s","description":"本番 image 上の実測 *markdown* <b>x</b>","dashboard":"https://grafana.b0xp.io/d/lab"},"startsAt":"2026-11-14T03:00:00Z","endsAt":"%s","fingerprint":"%s00000000"}]}' \
    "$1" "$2" "$2" "$3" "$1" "$end" "$1"
}
post() {
  printf '%s -> ' "$2"
  "$here/lab.sh" post "$name" "$1" "$2" "$3"
}
"$here/lab.sh" down "$name"
"$here/lab.sh" up "$tag" "$name"
sleep 5
# The production image ships root-owned directories under /opt/data that the PVC hides in production;
# a fresh lab volume inherits them, so hand them to the runtime user once and start again.
docker exec -u root "$name" chown -R 1000:10000 /opt/data
docker restart "$name" >/dev/null
"$here/lab.sh" stub "$name"
sleep 60
mark "S1 critical firing"
post alertmanager s1 "$(alert aaaa0001 firing critical)"
sleep 6
mark "S2 the same payload delivered again (Alertmanager retry)"
post alertmanager s2 "$(alert aaaa0001 firing critical)"
sleep 6
mark "S3 resolved"
post alertmanager s3 "$(alert aaaa0001 resolved critical)"
sleep 6
mark "S3b the alert fires again with the same start time (same text as S1) -> a new notification"
post alertmanager s3b "$(alert aaaa0001 firing critical)"
sleep 6
mark "S4 relay output [SILENT] (quiet hours)"
post alertmanager-quiet s4 "$(alert aaaa0004 firing warning)"
sleep 6
mark "S5 not an Alertmanager payload"
post alertmanager s5 '{"event_type":"x"}'
sleep 6
mark "S6 Telegram down -> home only, Telegram pending"
x 'echo "{\"telegram_fail\":true}" > /lab/logs/control.json'
post alertmanager s6 "$(alert aaaa0006 firing critical)"
sleep 8
mark "S6b half delivered (home only), then resolved and firing again with the same text -> home gets all three"
post alertmanager s6b-resolved "$(alert aaaa0006 resolved critical)"
sleep 6
post alertmanager s6b-refire "$(alert aaaa0006 firing critical)"
sleep 8
mark "S6 Telegram back -> recovery"
x 'echo "{}" > /lab/logs/control.json'
sleep 25
mark "S7 vault unwritable -> Telegram only, home pending"
x 'touch /opt/data/boxp-notify/lab-fail-home'
post alertmanager s7 "$(alert aaaa0007 firing critical)"
sleep 8
mark "S7 vault back -> recovery"
x 'rm -f /opt/data/boxp-notify/lab-fail-home'
sleep 25
mark "S8 vault and Telegram both down, then gateway restart"
x 'touch /opt/data/boxp-notify/lab-fail-home; echo "{\"telegram_fail\":true}" > /lab/logs/control.json'
post alertmanager s8 "$(alert aaaa0008 firing critical)"
sleep 6
mark "S8b reached nothing, then resolved and firing again with the same text -> after recovery: resolve, then the firing once"
post alertmanager s8b-resolved "$(alert aaaa0008 resolved critical)"
sleep 6
post alertmanager s8b-refire "$(alert aaaa0008 firing critical)"
sleep 30
docker restart "$name" >/dev/null
"$here/lab.sh" stub "$name"
sleep 60
mark "S8 both back after the restart -> recovery"
x 'rm -f /opt/data/boxp-notify/lab-fail-home; echo "{}" > /lab/logs/control.json'
sleep 30
mark "end"
docker exec "$name" sh -c 'cd /opt/hermes && ./.venv/bin/hermes plugins validate /opt/data/plugins/boxp-notify 2>&1 | grep -v "install out of sync" | tail -15' >"$out.validate.txt" 2>&1 || true
rm -rf "$out"
"$here/lab.sh" collect "$name" "$out"
docker exec "$name" sh -c 'ls /opt/data/boxp-notify/receipts; grep -c . /opt/data/webhook_subscriptions.json >/dev/null; id hermes' >"$out.state.txt" 2>&1 || true
