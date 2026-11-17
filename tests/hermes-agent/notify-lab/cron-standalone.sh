#!/bin/sh
# BOXP-546 lab: cron delivery with no gateway running (`hermes cron tick` in its own process), i.e. the
# plugin's standalone sender. Usage: cron-standalone.sh <image-tag> <container-name> <evidence-dir>
set -eu
here=$(cd "$(dirname "$0")" && pwd)
tag=$1
name=$2
out=$3
"$here/lab.sh" down "$name"
docker run -d --name "$name" --network none -v "$name-data:/opt/data" -v "$name-lab:/lab" --entrypoint sleep "$tag" 3600 >/dev/null
docker exec -u root "$name" sh -c 'chown -R hermes /opt/data /lab 2>/dev/null || true'
docker exec -d -u hermes "$name" sh -c 'bb /lab/stub.bb >> /lab/logs/stub.out 2>&1'
sleep 5
h() { docker exec -u hermes "$name" sh -c "cd /opt/hermes; HOME=/opt/data ./.venv/bin/hermes $1 2>&1 | grep -v 'install out of sync'"; }
h 'cron create --name lab-agent --deliver boxp_notify:cron-lab-agent "every 12h" "cron lab scenario=default"' | grep Created
id=$(h "cron list" | awk '/\[active\]/{print $1}')
for _ in 1 2; do
  h "cron run $id" | tail -1
  h "cron tick" | tail -3
done
rm -rf "$out"
mkdir -p "$out"
docker cp -q "$name:/opt/data/boxp-notify" "$out/boxp-notify"
docker cp -q "$name:/lab/logs" "$out/logs"
docker cp -q "$name:/lab/vault" "$out/vault"
"$here/lab.sh" down "$name"
