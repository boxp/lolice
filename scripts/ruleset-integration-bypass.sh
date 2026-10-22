#!/usr/bin/env bash
# BOXP-265: lolice main ruleset (509073) の bypass_actors を GitHub App (Integration) 単位へ移行する。
#
# 使い方:
#   scripts/ruleset-integration-bypass.sh show
#   scripts/ruleset-integration-bypass.sh add      <app-slug> [--apply]   # DeployKey bypass を残したまま App の Integration bypass を追加
#   scripts/ruleset-integration-bypass.sh app-only <app-slug> [--apply]   # DeployKey bypass を除去し App の Integration bypass のみにする
#   scripts/ruleset-integration-bypass.sh restore  <backup.json> [--apply] # 直前 GET の JSON で bypass_actors を戻す
#
# - --apply を付けない限り PUT しない (dry-run で payload と diff を表示する)。
# - rules / conditions / enforcement / name / target は直前 GET の値をそのまま使い、bypass_actors 以外を変更しない。
# - App ID は公開 API GET /apps/<slug> の id (Integration actor_id)。Installation ID と混同しない。
# - 秘密値 (private key など) は一切扱わない。
set -euo pipefail

REPO="${REPO:-boxp/lolice}"
RULESET_ID="${RULESET_ID:-509073}"
BACKUP_DIR="${BACKUP_DIR:-/tmp}"

usage() { sed -n '2,13p' "$0"; exit 1; }

cmd="${1:-}"; shift || true
[ -n "$cmd" ] || usage
apply=0
args=()
for a in "$@"; do
  case "$a" in
    --apply) apply=1 ;;
    *) args+=("$a") ;;
  esac
done

fetch_current() {
  local ts
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  current="$BACKUP_DIR/ruleset-$RULESET_ID-$ts.json"
  gh api "repos/$REPO/rulesets/$RULESET_ID" > "$current"
  echo "current ruleset saved: $current" >&2
}

resolve_app_id() {
  local slug="$1" json
  json="$(gh api "apps/$slug")" || { echo "GET /apps/$slug failed (slug が誤り or App が存在しない)" >&2; exit 2; }
  echo "$json" | jq -r '"app: slug=\(.slug) id=\(.id) owner=\(.owner.login) permissions=\(.permissions|tostring)"' >&2
  [ "$(echo "$json" | jq -r .owner.login)" = "${APP_OWNER:-boxp}" ] || { echo "App owner が ${APP_OWNER:-boxp} ではない。中断。" >&2; exit 2; }
  echo "$json" | jq -r .id
}

put_or_show() {
  local payload="$1"
  echo "--- bypass_actors diff (current -> new) ---" >&2
  diff <(jq -S .bypass_actors "$current") <(echo "$payload" | jq -S .bypass_actors) >&2 || true
  echo "--- other fields unchanged? ---" >&2
  diff <(jq -S 'del(.bypass_actors)|{name,target,enforcement,conditions,rules}' "$current") \
       <(echo "$payload" | jq -S '{name,target,enforcement,conditions,rules}') >&2 && echo "ok: name/target/enforcement/conditions/rules は不変" >&2
  if [ "$apply" = 1 ]; then
    echo "$payload" | gh api -X PUT "repos/$REPO/rulesets/$RULESET_ID" --input - | jq '{id,name,enforcement,bypass_actors,rules:[.rules[].type]}'
  else
    echo "dry-run: --apply を付けると PUT します" >&2
    echo "$payload" | jq '{bypass_actors}'
  fi
}

build_payload() {
  # $1: jq filter で新しい bypass_actors を生成
  jq --argjson app_id "$app_id" "{name,target,enforcement,conditions,rules,bypass_actors: ($1)}" "$current"
}

case "$cmd" in
  show)
    fetch_current
    jq '{id,name,enforcement,target,conditions,bypass_actors,rules:[.rules[].type]}' "$current"
    gh api "repos/$REPO/keys" --jq '.[] | "deploy key: id=\(.id) title=\(.title) read_only=\(.read_only) last_used=\(.last_used)"'
    ;;
  add)
    slug="${args[0]:-}"; [ -n "$slug" ] || usage
    fetch_current
    app_id="$(resolve_app_id "$slug")"
    # 既存の bypass_actors を保持し、同じ Integration が無ければ追加
    payload="$(build_payload '(.bypass_actors // []) as $b | if ($b | any(.actor_type=="Integration" and .actor_id==$app_id)) then $b else $b + [{actor_id:$app_id, actor_type:"Integration", bypass_mode:"always"}] end')"
    put_or_show "$payload"
    ;;
  app-only)
    slug="${args[0]:-}"; [ -n "$slug" ] || usage
    fetch_current
    app_id="$(resolve_app_id "$slug")"
    payload="$(build_payload '[{actor_id:$app_id, actor_type:"Integration", bypass_mode:"always"}]')"
    put_or_show "$payload"
    ;;
  restore)
    backup="${args[0]:-}"; [ -f "$backup" ] || usage
    fetch_current
    app_id=0
    payload="$(jq --slurpfile b "$backup" '{name,target,enforcement,conditions,rules,bypass_actors: $b[0].bypass_actors}' "$current")"
    put_or_show "$payload"
    ;;
  *) usage ;;
esac
