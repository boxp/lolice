#!/usr/bin/env bash
# BOXP-265: lolice main ruleset (509073) の bypass_actors を GitHub App (Integration) 単位へ移行する。
#
# 使い方:
#   scripts/ruleset-integration-bypass.sh show
#   scripts/ruleset-integration-bypass.sh add      <app-slug> [--app-id <id>] [--apply]   # DeployKey bypass を残したまま App の Integration bypass を追加
#   scripts/ruleset-integration-bypass.sh app-only <app-slug> [--app-id <id>] [--apply]   # DeployKey bypass を除去し App の Integration bypass のみにする
#   scripts/ruleset-integration-bypass.sh restore  <backup.json> [--apply] # 直前 GET の JSON で bypass_actors を戻す
#
# - --apply を付けない限り PUT しない (dry-run で payload と diff を表示する)。
# - rules / conditions / enforcement / name / target は直前 GET の値をそのまま使い、bypass_actors 以外を変更しない。
# - App ID は公開 API GET /apps/<slug> の id (Integration actor_id)。Installation ID と混同しない。
#   private App は GET /apps/<slug> が 404 になるため、その場合は --app-id <id> (App 設定画面冒頭の App ID、非秘密) を渡す。
#   渡された id は GraphQL node(id: base64("03:App<id>")) で slug が一致することを照合してから使う。
# - --apply 後は GraphQL の rulesets.bypassActors で Integration actor の slug を再確認する。
# - 秘密値 (private key など) は一切扱わない。
set -euo pipefail

REPO="${REPO:-boxp/lolice}"
RULESET_ID="${RULESET_ID:-509073}"
BACKUP_DIR="${BACKUP_DIR:-/tmp}"

usage() { sed -n '2,16p' "$0"; exit 1; }

cmd="${1:-}"; shift || true
[ -n "$cmd" ] || usage
apply=0
app_id_opt=""
args=()
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) apply=1 ;;
    --app-id) shift; app_id_opt="${1:-}" ;;
    --app-id=*) app_id_opt="${1#--app-id=}" ;;
    *) args+=("$1") ;;
  esac
  shift
done

fetch_current() {
  local ts
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  current="$BACKUP_DIR/ruleset-$RULESET_ID-$ts.json"
  gh api "repos/$REPO/rulesets/$RULESET_ID" > "$current"
  echo "current ruleset saved: $current" >&2
}

# GraphQL で App ID → slug を照合する (private App でも owner の token なら解決できる)。
# App の legacy global node ID は base64("03:App<id>")。
graphql_app_slug() {
  local id="$1" node
  node="$(printf '03:App%s' "$id" | base64 | tr -d '\n')"
  gh api graphql -f query="{ node(id: \"$node\") { __typename ... on App { databaseId slug name } } }" \
    --jq 'if .data.node.__typename == "App" then "\(.data.node.databaseId) \(.data.node.slug) \(.data.node.name)" else empty end' 2>/dev/null
}

resolve_app_id() {
  local slug="$1" json
  if json="$(gh api "apps/$slug" 2>/dev/null)"; then
    echo "$json" | jq -r '"app: slug=\(.slug) id=\(.id) owner=\(.owner.login) permissions=\(.permissions|tostring)"' >&2
    [ "$(echo "$json" | jq -r .owner.login)" = "${APP_OWNER:-boxp}" ] || { echo "App owner が ${APP_OWNER:-boxp} ではない。中断。" >&2; exit 2; }
    local rest_id; rest_id="$(echo "$json" | jq -r .id)"
    if [ -n "$app_id_opt" ] && [ "$app_id_opt" != "$rest_id" ]; then
      echo "--app-id $app_id_opt が GET /apps/$slug の id=$rest_id と一致しない。中断。" >&2; exit 2
    fi
    echo "$rest_id"; return
  fi
  # private App: GET /apps/<slug> は 404。--app-id を GraphQL で slug 照合する。
  if [ -z "$app_id_opt" ]; then
    echo "GET /apps/$slug は 404 (private App か slug 誤り)。App 設定画面 https://github.com/settings/apps/$slug 冒頭の App ID (数値・非秘密) を --app-id で渡してください。" >&2
    exit 2
  fi
  [[ "$app_id_opt" =~ ^[0-9]+$ ]] || { echo "--app-id は数値 (App ID)。Installation ID と混同しない。" >&2; exit 2; }
  local resolved
  resolved="$(graphql_app_slug "$app_id_opt")"
  [ -n "$resolved" ] || { echo "GraphQL で App id=$app_id_opt を解決できない (id 誤り、または owner 以外の token)。中断。" >&2; exit 2; }
  echo "app (graphql): id=${resolved%% *} slug=$(echo "$resolved" | awk '{print $2}') name=$(echo "$resolved" | cut -d' ' -f3-)" >&2
  [ "$(echo "$resolved" | awk '{print $2}')" = "$slug" ] || { echo "App id=$app_id_opt の slug が $slug ではない。中断。" >&2; exit 2; }
  echo "$app_id_opt"
}

# GraphQL で ruleset の bypass actors を slug 付きで表示する (REST は actor_id しか返さない)。
show_bypass_actors_graphql() {
  gh api graphql -f query="{ repository(owner: \"${REPO%%/*}\", name: \"${REPO#*/}\") { rulesets(first: 20) { nodes { databaseId name bypassActors(first: 20) { nodes { bypassMode deployKey actor { __typename ... on App { databaseId slug } } } } } } } }" \
    --jq ".data.repository.rulesets.nodes[] | select(.databaseId == $RULESET_ID) | .bypassActors.nodes[] | \"bypass (graphql): mode=\(.bypassMode) deployKey=\(.deployKey) actor=\(.actor // {} | tostring)\"" 2>/dev/null || true
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
    show_bypass_actors_graphql >&2
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
    show_bypass_actors_graphql
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
