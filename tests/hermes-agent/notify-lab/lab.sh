#!/bin/sh
# BOXP-546 lab driver. Usage: lab.sh build <tag> <base-image> | up <tag> <name> [docker run args] | down <name>
#                              | post <name> <route> <delivery-id> <json> | logs <name>
set -eu
here=$(cd "$(dirname "$0")" && pwd)
cmd=$1
shift
case "$cmd" in
  build)
    tag=$1
    base=$2
    ctx=$(mktemp -d "${LAB_TMP:-/tmp}/lab-ctx.XXXXXX")
    cp "$here/Dockerfile" "$here/stub.bb" "$here/config.yaml" "$here/webhook_subscriptions.json" "$here/ticket.md" "$ctx/"
    cp "$(command -v bb)" "$ctx/bb"
    cp -r "$here/../../../argoproj/hermes-agent/plugins" "$ctx/plugin"
    DOCKER_BUILDKIT=0 docker build -q --build-arg "BASE=$base" -t "$tag" "$ctx"
    rm -rf "$ctx"
    ;;
  up)
    tag=$1
    name=$2
    shift 2
    docker run -d --name "$name" --network none -v "$name-data:/opt/data" -v "$name-lab:/lab" "$@" "$tag" gateway run >/dev/null
    docker exec -d "$name" sh -c 'bb /lab/stub.bb >> /lab/logs/stub.out 2>&1'
    ;;
  stub) docker exec -d "$1" sh -c 'bb /lab/stub.bb >> /lab/logs/stub.out 2>&1' ;;
  down)
    docker rm -f "$1" >/dev/null 2>&1 || true
    docker volume rm "$1-data" "$1-lab" >/dev/null 2>&1 || true
    ;;
  post)
    name=$1
    route=$2
    id=$3
    body=$4
    docker exec "$name" curl -sS -o /dev/null -w '%{http_code}\n' -X POST "http://127.0.0.1:8644/webhooks/$route" \
      -H 'Content-Type: application/json' -H 'X-Gitlab-Token: lab-secret' -H "X-Request-ID: $id" \
      -H "X-GitHub-Event: ${LAB_EVENT_TYPE:-task-board.run-finished}" -d "$body"
    ;;
  collect) # collect <name> <dir>
    mkdir -p "$2"
    docker cp -q "$1:/opt/data/boxp-notify" "$2/boxp-notify"
    docker cp -q "$1:/lab/logs" "$2/logs"
    docker cp -q "$1:/lab/vault" "$2/vault"
    docker cp -q "$1:/opt/data/logs/gateway.log" "$2/gateway.log"
    ;;
  logs)
    docker exec "$1" sh -c 'for f in /opt/data/boxp-notify/journal.jsonl /lab/logs/llm.jsonl /lab/logs/telegram.jsonl; do echo "## $f"; cat "$f" 2>/dev/null || true; done'
    ;;
  *)
    echo "unknown command: $cmd" >&2
    exit 2
    ;;
esac
