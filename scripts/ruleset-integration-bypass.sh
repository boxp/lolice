#!/usr/bin/env bash
# BOXP-265: lolice main ruleset (509073) の bypass_actors を GitHub App (Integration) 単位へ移行する。
#
# 使い方:
#   scripts/ruleset-integration-bypass.sh show
#   scripts/ruleset-integration-bypass.sh add      <app-slug> [--app-id <id>] [--apply]   # DeployKey bypass を残したまま App の Integration bypass を追加
#   scripts/ruleset-integration-bypass.sh app-only <app-slug> [--app-id <id>] [--apply]   # DeployKey bypass を除去し App の Integration bypass のみにする
#   scripts/ruleset-integration-bypass.sh restore  <backup.json> [--apply] # 直前 GET の JSON で bypass_actors を戻す
#   scripts/ruleset-integration-bypass.sh suites   <app-slug>              # main への push 評価 (rule-suites) のうち App 主体のものを表示
#
# - --apply を付けない限り PUT しない (dry-run で payload と diff を表示する)。
# - rules / conditions / enforcement / name / target は直前 GET の値をそのまま使い、bypass_actors 以外を変更しない。
# - App ID は公開 API GET /apps/<slug> の id (Integration actor_id)。Installation ID と混同しない。
#   private App は GET /apps/<slug> が 404 になるため、その場合は --app-id <id> (App 設定画面冒頭の App ID、非秘密) を渡す。
#   渡された id は GraphQL node(id: base64("03:App<id>")) で slug が一致することを照合してから使う。
#   private App は API で owner を確認できないため、fallback は HTTP 404 のときだけ、かつ slug が
#   PRIVATE_APP_SLUGS (既定: argocd-image-updater-for-lolice、owner 提示) に含まれる場合に限る。
# - --apply 後は GraphQL の rulesets.bypassActors で Integration actor の slug を再確認する。
#   ただし private App は GraphQL node(id) も bypassActors.actor も NOT_FOUND / null になる (owner の OAuth token でも解決不可、2026-10-22 実測)。
#   その場合は rule-suites API (GET /repos/<repo>/rulesets/rule-suites?ref=refs/heads/main) で
#   actor_name=<slug>[bot] の push が result=bypass になっていること (= 現在の Integration actor_id がその App のもの) を照合に使う。
#   - add (3a): GraphQL でも rule-suites でも照合できない初回は ALLOW_UNVERIFIED_APP_ID=1 を付けた場合のみ進める
#     (DeployKey bypass は保持されるので可逆。誤 ID なら App push は fail のまま残り、suites で判別できる)。
#   - app-only (3c): rule-suites の bypass 証拠が無ければ中断する (DeployKey bypass を外す前に必ず App 主体の bypass push を要求)。
# - 秘密値 (private key など) は一切扱わない。
set -euo pipefail

REPO="${REPO:-boxp/lolice}"
RULESET_ID="${RULESET_ID:-509073}"
BACKUP_DIR="${BACKUP_DIR:-/tmp}"

usage() { sed -n '2,24p' "$0"; exit 1; }

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
  local id="$1" node out
  node="$(printf '03:App%s' "$id" | base64 | tr -d '\n')"
  # gh は GraphQL errors があると応答本文を stdout に出して非 0 で終わるため、exit code と形式 ("<数値> <slug> ...") を両方確認する。
  out="$(gh api graphql -f query="{ node(id: \"$node\") { __typename ... on App { databaseId slug name } } }" \
    --jq 'if .data.node.__typename == "App" then "\(.data.node.databaseId) \(.data.node.slug) \(.data.node.name)" else empty end' 2>/dev/null)" || return 0
  [[ "$out" =~ ^[0-9]+\ [A-Za-z0-9._-]+ ]] && printf '%s\n' "$out"
  return 0
}

# rule-suites: main への push 評価のうち App 主体 (actor_name=<slug>[bot]) のものを表示する。
# result=fail は bypass 無しで pull_request / required_status_checks に掛かった push、result=bypass は Integration bypass で通った push。
app_rule_suites() {
  local slug="$1"
  gh api "repos/$REPO/rulesets/rule-suites?ref=refs/heads/main&per_page=100" \
    --jq ".[] | select(.actor_name == \"${slug}[bot]\") | \"\(.pushed_at) actor=\(.actor_name) \(.before_sha[0:7])->\(.after_sha[0:7]) result=\(.result)\""
}

# 現在の ruleset が Integration actor_id=$2 を含み、かつその更新時刻以降に <slug>[bot] の push が result=bypass で記録されていれば 0。
suites_bypass_evidence() {
  local slug="$1" id="$2" since n
  jq -e --argjson id "$id" '.bypass_actors[]? | select(.actor_type=="Integration" and .actor_id==$id)' "$current" >/dev/null || return 1
  since="$(jq -r '.updated_at // "1970-01-01T00:00:00Z"' "$current")"
  n="$(gh api "repos/$REPO/rulesets/rule-suites?ref=refs/heads/main&per_page=100" \
    --jq "[.[] | select(.actor_name == \"${slug}[bot]\" and .result == \"bypass\" and .pushed_at >= \"$since\")] | length")"
  [ "${n:-0}" -gt 0 ] || return 1
  echo "app (rule-suites): ${slug}[bot] の main push が Integration actor_id=$id の下で result=bypass ($n 件, since $since)" >&2
}

resolve_app_id() {
  local slug="$1" json status raw
  # gh api -i で HTTP status を取り、404 のときだけ private App として fallback する (一時障害・認証失敗では fallback しない)。
  # token は gh が内部で扱い、プロセス引数には載せない。
  raw="$(gh api -i "apps/$slug" 2>/dev/null || true)"
  status="$(printf '%s\n' "$raw" | awk 'NR==1 && $1 ~ /^HTTP/ {print $2}')"
  json="$(printf '%s\n' "$raw" | awk 'body {print} /^\r?$/ {body=1}')"
  if [ "$status" = "200" ] && [ -n "$json" ]; then
    echo "$json" | jq -r '"app: slug=\(.slug) id=\(.id) owner=\(.owner.login) permissions=\(.permissions|tostring)"' >&2
    [ "$(echo "$json" | jq -r .owner.login)" = "${APP_OWNER:-boxp}" ] || { echo "App owner が ${APP_OWNER:-boxp} ではない。中断。" >&2; exit 2; }
    local rest_id; rest_id="$(echo "$json" | jq -r .id)"
    if [ -n "$app_id_opt" ] && [ "$app_id_opt" != "$rest_id" ]; then
      echo "--app-id $app_id_opt が GET /apps/$slug の id=$rest_id と一致しない。中断。" >&2; exit 2
    fi
    echo "$rest_id"; return
  fi
  [ "$status" = "404" ] || { echo "GET /apps/$slug が HTTP $status (404 以外)。一時障害や認証問題の可能性があるため private App として扱わず中断。" >&2; exit 2; }
  # private App: GET /apps/<slug> は 404 で owner を API から確認できない。
  # 代わりに owner が提示した slug の許可リスト (PRIVATE_APP_SLUGS、空白区切り) に含まれることを必須にし、
  # --app-id を GraphQL で slug 照合する (slug は GitHub 全体で一意)。
  local allowed=0 a
  for a in ${PRIVATE_APP_SLUGS:-argocd-image-updater-for-lolice}; do [ "$a" = "$slug" ] && allowed=1; done
  [ "$allowed" = 1 ] || { echo "private App '$slug' は PRIVATE_APP_SLUGS (owner 提示の slug) に含まれない。中断。" >&2; exit 2; }
  if [ -z "$app_id_opt" ]; then
    echo "GET /apps/$slug は 404 (private App か slug 誤り)。App 設定画面 https://github.com/settings/apps/$slug 冒頭の App ID (数値・非秘密) を --app-id で渡してください。" >&2
    exit 2
  fi
  [[ "$app_id_opt" =~ ^[0-9]+$ ]] || { echo "--app-id は数値 (App ID)。Installation ID と混同しない。" >&2; exit 2; }
  local resolved
  resolved="$(graphql_app_slug "$app_id_opt")"
  if [ -n "$resolved" ]; then
    echo "app (graphql): id=${resolved%% *} slug=$(echo "$resolved" | awk '{print $2}') name=$(echo "$resolved" | cut -d' ' -f3-)" >&2
    [ "$(echo "$resolved" | awk '{print $2}')" = "$slug" ] || { echo "App id=$app_id_opt の slug が $slug ではない。中断。" >&2; exit 2; }
    echo "$app_id_opt"; return
  fi
  # private App は GraphQL でも解決できない。rule-suites の bypass 証拠 (現在の Integration actor_id の下で <slug>[bot] の push が bypass) で照合する。
  if suites_bypass_evidence "$slug" "$app_id_opt"; then
    echo "$app_id_opt"; return
  fi
  if [ "$cmd" = "add" ] && [ "${ALLOW_UNVERIFIED_APP_ID:-0}" = 1 ]; then
    echo "warn: App id=$app_id_opt は GraphQL / rule-suites のどちらでも slug=$slug と照合できていない (owner 提示値をそのまま使う)。DeployKey bypass は保持されるため可逆。適用後は 'suites $slug' で <slug>[bot] の push が result=bypass になることを確認すること。" >&2
    echo "$app_id_opt"; return
  fi
  echo "App id=$app_id_opt を slug=$slug と照合できない (private App は GraphQL node(id) で NOT_FOUND になる)。" >&2
  echo "  - add (3a) の初回は ALLOW_UNVERIFIED_APP_ID=1 を付けて owner 提示の App ID を使う (DeployKey bypass 保持のため可逆)。" >&2
  echo "  - app-only (3c) は 'suites $slug' で result=bypass の push が記録されてから実行する。" >&2
  exit 2
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
  suites)
    slug="${args[0]:-}"; [ -n "$slug" ] || usage
    app_rule_suites "$slug"
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
