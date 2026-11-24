#!/usr/bin/env bash
# BOXP-699: onitiku Discord (contact point "onitiku server") への Grafana 通知停止の回帰テスト。
#
# 本番では対象ルール 2 件と contact point が UI 作成 (provenance なし) で DB にあり、
# argoproj/prometheus-operator/grafana-alerting/onitiku-discord-stop.yaml の file provisioning で
# 同じ uid のルールを取り込み、常時有効な mute timing を付ける。期待 (仕様) は次のとおり。
#   1. UI 作成のルールを持つ既存 DB に provisioning を足して再起動しても Grafana は起動する
#   2. 対象ルールは同じ uid のまま provenance=file になり、評価は続く (状態は Alerting / Normal に変わる)
#   3. FIRING と RESOLVED のどちらも "onitiku server" へ送らない
#   4. default policy の receiver ("boxlab alert") へも流れない (fallback しない)
#   5. contact point "onitiku server" と default policy は変更・削除されない
#   6. 対照: provisioning 前 (UI 作成のまま) は同じ fixture で "onitiku server" に FIRING / RESOLVED が届く
#
#   static   : provisioning ファイルの対象・mute の付与を照合する (kind 不要)
#   prepare  : 本番イメージの Grafana と偽 Prometheus / webhook 受信口を起動し、UI 作成相当の状態を作る
#   control  : provisioning 前に FIRING / RESOLVED が "onitiku server" に届くことを確かめる (6)
#   upgrade  : 本番 render の grafana-alerting-provisioning を mount して同じ PVC で再起動し 1,2,5 を確かめる
#   muted    : 発火→解消させても通知がどこにも届かないことを確かめる (2,3,4)
#   restart  : provisioning 済みからもう一度再起動しても起動し、状態が保たれることを確かめる
#   diagnose : 失敗時の状態とログを出す
#
# fixture は本番と次の点が違う: contact point は Discord ではなく webhook (受信口に記録するだけ)、
# default policy の group_wait / group_interval を短くしている (mute の判定には影響しない)。
# Discord への実送信、本番 PVC (Longhorn)、mute timing の時刻境界は扱わない。
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(git rev-parse --show-toplevel)}"
HERE="$REPO_ROOT/tests/chainsaw/prometheus-operator/onitiku-discord-mute"
NS="${ONITIKU_MUTE_TEST_NAMESPACE:-chainsaw-grafana-onitiku}"
WORK="${ONITIKU_MUTE_TEST_WORKDIR:-${TMPDIR:-/tmp}/grafana-onitiku-mute-test}"
SRC="$REPO_ROOT/argoproj/prometheus-operator/grafana-alerting/onitiku-discord-stop.yaml"
ADMIN_PASSWORD="chainsaw-admin"
FIXTURE_IMAGE="babashka/babashka:1.12.215"

# 本番の UI 作成オブジェクトの uid / 名前 (2026-11-24 時点の読戻し)
FOLDER_UID="be64nsy52jda8d"
PROM_DS_UID="P1809F7CD0C75ACF3"
ONITIKU_CP_UID="de96123xro5c0c"
BOXLAB_CP_UID="de936r1ia7jepd"
PAL_RULE_UID="be92yxv4y2mf4b"
ARK_RULE_UID="fenkaqm8slj40d"
MUTE_NAME="onitiku-discord-stopped"

mkdir -p "$WORK"

render() {
  kustomize build "$REPO_ROOT/argoproj/prometheus-operator" > "$WORK/rendered.yaml"
  yq -r 'select(.kind == "Deployment" and .metadata.name == "grafana") | .spec.template.spec.containers[] | select(.name == "grafana") | .image' \
    "$WORK/rendered.yaml" > "$WORK/image"
  yq 'select(.kind == "ConfigMap" and .metadata.name == "grafana-alerting-provisioning")' "$WORK/rendered.yaml" > "$WORK/alerting-cm.yaml"
  yq -r 'select(.kind == "Deployment" and .metadata.name == "grafana") | .spec.template.spec.containers[] | select(.name == "grafana") | .volumeMounts[] | select(.name == "grafana-alerting-provisioning") | .mountPath' \
    "$WORK/rendered.yaml" > "$WORK/mount-path"
  test -s "$WORK/image" || { echo "grafana Deployment のイメージを render から取り出せません" >&2; exit 1; }
  test -s "$WORK/alerting-cm.yaml" || { echo "ConfigMap grafana-alerting-provisioning が render にありません" >&2; exit 1; }
  test "$(cat "$WORK/mount-path")" = /etc/grafana/provisioning/alerting \
    || { echo "grafana-alerting-provisioning が /etc/grafana/provisioning/alerting に mount されていません" >&2; exit 1; }
}

static_check() {
  render
  local f="$SRC" fail=0
  # 対象は "onitiku server" を receiver にするルールだけで、全ルールに mute が付く
  [ "$(yq '[.groups[].rules[]] | length' "$f")" = 2 ] || { echo "NG: 対象ルールは 2 件のはず" >&2; fail=1; }
  [ "$(yq '[.groups[].rules[] | select(.notification_settings.receiver != "onitiku server")] | length' "$f")" = 0 ] \
    || { echo "NG: onitiku server 以外を receiver にするルールが含まれています" >&2; fail=1; }
  [ "$(yq "[.groups[].rules[] | select((.notification_settings.mute_time_intervals // []) | contains([\"$MUTE_NAME\"]) | not)] | length" "$f")" = 0 ] \
    || { echo "NG: mute_time_intervals が付いていないルールがあります" >&2; fail=1; }
  [ "$(yq -r '[.groups[].rules[].uid] | sort | join(",")' "$f")" = "$(printf '%s\n' "$PAL_RULE_UID" "$ARK_RULE_UID" | sort | paste -sd,)" ] \
    || { echo "NG: 本番の UI 作成ルールと uid が一致しません (別ルールとして重複作成される)" >&2; fail=1; }
  # mute timing は常時有効 (全曜日、時刻・日付・月の制限なし)
  [ "$(yq -r ".muteTimes[] | select(.name == \"$MUTE_NAME\") | .time_intervals | length" "$f")" = 1 ] \
    && [ "$(yq -r ".muteTimes[] | select(.name == \"$MUTE_NAME\") | .time_intervals[0] | keys | join(\",\")" "$f")" = weekdays ] \
    && [ "$(yq -r ".muteTimes[] | select(.name == \"$MUTE_NAME\") | .time_intervals[0].weekdays | join(\",\")" "$f")" = "sunday:saturday" ] \
    || { echo "NG: mute timing $MUTE_NAME が常時有効になっていません" >&2; fail=1; }
  # contact point / policy は provisioning しない (UI 管理のまま、削除・上書きしない)
  [ "$(yq '(.contactPoints // []) + (.policies // []) + (.deleteContactPoints // []) + (.resetPolicies // []) | length' "$f")" = 0 ] \
    || { echo "NG: contact point / policy を provisioning しています" >&2; fail=1; }
  [ "$fail" = 0 ] && echo "ok: 対象 2 ルールに常時 mute を付け、contact point / policy は触らない"
  return "$fail"
}

grafana_exec() { kubectl -n "$NS" exec deploy/grafana-fixture -c grafana -- "$@"; }

api() {
  # api METHOD PATH [JSON]  (UI と同じく provenance を付けない)
  local method="$1" path="$2" body="${3:-}"
  if [ -n "$body" ]; then
    grafana_exec curl -fsS -u "admin:$ADMIN_PASSWORD" -X "$method" -H 'Content-Type: application/json' \
      -H 'X-Disable-Provenance: true' --data "$body" "http://127.0.0.1:3000$path"
  else
    grafana_exec curl -fsS -u "admin:$ADMIN_PASSWORD" -X "$method" "http://127.0.0.1:3000$path"
  fi
}

fixture() {
  # fixture METHOD PATH : 偽 Prometheus / 受信口を Grafana Pod から呼ぶ
  grafana_exec curl -fsS -X "$1" "http://fixture-server:8080$2"
}

wait_rollout() {
  local deadline=$((SECONDS + ${1:-240})) restarts
  until kubectl -n "$NS" rollout status deploy/grafana-fixture --timeout=10s > /dev/null 2>&1; do
    restarts="$(kubectl -n "$NS" get pod -l app=grafana-fixture -o jsonpath='{range .items[*]}{.status.containerStatuses[0].restartCount}{"\n"}{end}' 2>/dev/null | sort -n | tail -1)"
    if [ "${restarts:-0}" -ge 2 ] || [ "$SECONDS" -ge "$deadline" ]; then
      echo "Grafana が Ready になりません (restartCount=${restarts:-0})" >&2
      diagnose >&2
      return 1
    fi
  done
  local i
  for i in $(seq 1 30); do
    if api GET /api/health 2>/dev/null | jq -e '.database == "ok"' > /dev/null; then return 0; fi
    sleep 2
  done
  echo "Grafana の /api/health が ok になりません" >&2
  return 1
}

apply_fixture() {
  # $1: with-provisioning なら本番 render の grafana-alerting-provisioning を mount する
  local image; image="$(cat "$WORK/image")"
  kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f - > /dev/null
  kubectl -n "$NS" create configmap fixture-server --from-file=server.clj="$HERE/fixture-server.clj" \
    --dry-run=client -o yaml | kubectl apply -f - > /dev/null
  yq ".metadata.namespace = \"$NS\"" "$WORK/alerting-cm.yaml" | kubectl apply -f - > /dev/null
  local mounts="" volumes=""
  if [ "${1:-}" = with-provisioning ]; then
    mounts='
        - name: grafana-alerting-provisioning
          mountPath: /etc/grafana/provisioning/alerting
          readOnly: true'
    volumes='
      - name: grafana-alerting-provisioning
        configMap:
          name: grafana-alerting-provisioning'
  fi
  kubectl apply -f - > /dev/null <<YAML
apiVersion: apps/v1
kind: Deployment
metadata:
  name: fixture-server
  namespace: $NS
spec:
  replicas: 1
  selector:
    matchLabels:
      app: fixture-server
  template:
    metadata:
      labels:
        app: fixture-server
    spec:
      containers:
      - name: server
        image: $FIXTURE_IMAGE
        command: [bb, /fixture/server.clj]
        ports:
        - containerPort: 8080
        readinessProbe:
          httpGet:
            path: /healthz
            port: 8080
          periodSeconds: 3
        volumeMounts:
        - name: fixture
          mountPath: /fixture
      volumes:
      - name: fixture
        configMap:
          name: fixture-server
---
apiVersion: v1
kind: Service
metadata:
  name: fixture-server
  namespace: $NS
spec:
  selector:
    app: fixture-server
  ports:
  - port: 8080
    targetPort: 8080
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: grafana-fixture
  namespace: $NS
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 1Gi
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: grafana-fixture
  namespace: $NS
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: grafana-fixture
  template:
    metadata:
      labels:
        app: grafana-fixture
    spec:
      securityContext:
        fsGroup: 472
      containers:
      - name: grafana
        image: $image
        env:
        - name: GF_SECURITY_ADMIN_PASSWORD
          value: $ADMIN_PASSWORD
        - name: GF_ANALYTICS_REPORTING_ENABLED
          value: "false"
        - name: GF_ANALYTICS_CHECK_FOR_UPDATES
          value: "false"
        - name: GF_PLUGINS_PREINSTALL_DISABLED
          value: "true"
        readinessProbe:
          httpGet:
            path: /api/health
            port: 3000
          periodSeconds: 5
        volumeMounts:
        - name: storage
          mountPath: /var/lib/grafana$mounts
      volumes:
      - name: storage
        persistentVolumeClaim:
          claimName: grafana-fixture$volumes
YAML
  kubectl -n "$NS" rollout status deploy/fixture-server --timeout=180s > /dev/null
}

rule_group_json() {
  # provisioning ファイルの group を UI (API) 作成用の JSON にする。mute は UI 作成時点には無いので外す。
  local group="$1"
  yq -o=json "
    .groups[] | select(.name == \"$group\") | {
      \"title\": .name,
      \"folderUid\": \"$FOLDER_UID\",
      \"interval\": 60,
      \"rules\": [.rules[] | {
        \"uid\": .uid, \"title\": .title, \"condition\": .condition, \"data\": .data,
        \"noDataState\": .noDataState, \"execErrState\": .execErrState, \"for\": .for,
        \"annotations\": .annotations, \"isPaused\": .isPaused,
        \"folderUID\": \"$FOLDER_UID\", \"ruleGroup\": \"$group\", \"orgID\": 1,
        \"notification_settings\": {\"receiver\": .notification_settings.receiver}
      }]
    }" "$SRC" | jq -c .
}

prepare() {
  render
  apply_fixture
  wait_rollout 300
  api POST /api/folders "{\"uid\":\"$FOLDER_UID\",\"title\":\"Default\"}" > /dev/null
  api POST /api/datasources "{\"name\":\"prometheus\",\"type\":\"prometheus\",\"uid\":\"$PROM_DS_UID\",\"access\":\"proxy\",\"url\":\"http://fixture-server:8080\",\"jsonData\":{\"httpMethod\":\"POST\"}}" > /dev/null
  api POST /api/v1/provisioning/contact-points \
    "{\"uid\":\"$ONITIKU_CP_UID\",\"name\":\"onitiku server\",\"type\":\"webhook\",\"settings\":{\"url\":\"http://fixture-server:8080/hook/onitiku\"}}" > /dev/null
  api POST /api/v1/provisioning/contact-points \
    "{\"uid\":\"$BOXLAB_CP_UID\",\"name\":\"boxlab alert\",\"type\":\"webhook\",\"settings\":{\"url\":\"http://fixture-server:8080/hook/boxlab\"}}" > /dev/null
  api PUT /api/v1/provisioning/policies \
    '{"receiver":"boxlab alert","group_by":["grafana_folder","alertname"],"group_wait":"1s","group_interval":"10s","repeat_interval":"1h"}' > /dev/null
  local g
  for g in onitiku-palserver ark-asa; do
    api PUT "/api/v1/provisioning/folder/$FOLDER_UID/rule-groups/$g" "$(rule_group_json "$g")" > /dev/null
  done
  api GET /api/v1/provisioning/alert-rules > "$WORK/rules-before.json"
  jq -e "[.[] | select(.uid == \"$PAL_RULE_UID\" or .uid == \"$ARK_RULE_UID\") | select((.provenance // \"\") == \"\")] | length == 2" \
    "$WORK/rules-before.json" > /dev/null || { echo "UI 作成相当のルールを用意できません" >&2; cat "$WORK/rules-before.json" >&2; exit 1; }
  echo "UI 作成相当の状態を用意: $(jq -c '[.[] | {uid, title, isPaused, provenance, notification_settings}]' "$WORK/rules-before.json")"
}

rule_state() {
  # Grafana の Prometheus 互換 API で palworld ルールの state (inactive/pending/firing) を返す
  api GET /api/prometheus/grafana/api/v1/rules \
    | jq -r --arg t "onitiku palworldが稼働していません" '.data.groups[].rules[] | select(.name == $t) | .state'
}

wait_for() {
  # wait_for SECONDS DESCRIPTION COMMAND...
  local deadline=$((SECONDS + $1)) desc="$2"; shift 2
  until "$@"; do
    if [ "$SECONDS" -ge "$deadline" ]; then echo "待機タイムアウト: $desc" >&2; return 1; fi
    sleep 5
  done
}

hook_has() { fixture GET /log | jq -e --arg n "$1" --arg s "$2" 'any(.[]; .name == $n and .status == $s)' > /dev/null; }
state_is() { [ "$(rule_state)" = "$1" ]; }

control() {
  fixture POST /reset > /dev/null
  fixture POST /mode/down > /dev/null
  wait_for 300 "provisioning 前の FIRING が onitiku に届く" hook_has onitiku firing
  fixture POST /mode/ok > /dev/null
  wait_for 300 "provisioning 前の RESOLVED が onitiku に届く" hook_has onitiku resolved
  fixture GET /log > "$WORK/control-log.json"
  echo "対照 (UI 作成のまま): $(jq -c . "$WORK/control-log.json")"
  jq -e 'all(.[]; .name == "onitiku")' "$WORK/control-log.json" > /dev/null \
    || { echo "対照で boxlab に届いています (fixture の routing が本番と違う)" >&2; exit 1; }
  wait_for 180 "palworld ルールが Normal に戻る" state_is inactive
  fixture POST /reset > /dev/null
}

verify_provisioned() {
  api GET /api/v1/provisioning/alert-rules > "$WORK/rules-after.json"
  echo "provisioning 後: $(jq -c '[.[] | {uid, title, isPaused, provenance, notification_settings}]' "$WORK/rules-after.json")"
  jq -e --arg mute "$MUTE_NAME" --arg pal "$PAL_RULE_UID" --arg ark "$ARK_RULE_UID" '
    length == 2
    and all(.[]; (.uid == $pal or .uid == $ark) and .provenance == "file"
                 and .notification_settings.receiver == "onitiku server"
                 and .notification_settings.mute_time_intervals == [$mute]
                 and .folderUID == "'"$FOLDER_UID"'")
    and (map(select(.uid == $pal))[0].isPaused == false)
    and (map(select(.uid == $ark))[0].isPaused == true)' "$WORK/rules-after.json" > /dev/null \
    || { echo "対象ルールが同じ uid のまま mute 付きで provisioning されていません" >&2; exit 1; }
  api GET /api/v1/provisioning/mute-timings | jq -e --arg m "$MUTE_NAME" \
    'any(.[]; .name == $m and .provenance == "file")' > /dev/null || { echo "mute timing $MUTE_NAME がありません" >&2; exit 1; }
  api GET /api/v1/provisioning/contact-points | jq -e --arg u "$ONITIKU_CP_UID" \
    'any(.[]; .uid == $u and .name == "onitiku server" and ((.provenance // "") == ""))' > /dev/null \
    || { echo "contact point onitiku server が UI 管理のまま残っていません" >&2; exit 1; }
  api GET /api/v1/provisioning/policies | jq -e '.receiver == "boxlab alert" and ((.provenance // "") == "")' > /dev/null \
    || { echo "default policy が変わっています" >&2; exit 1; }
  api GET /api/folders | jq -e '[.[] | select(.title == "Default")] | length == 1' > /dev/null \
    || { echo "folder Default が重複して作られています" >&2; exit 1; }
}

upgrade() {
  render
  apply_fixture with-provisioning
  wait_rollout 240
  verify_provisioned
}

muted() {
  fixture POST /reset > /dev/null
  fixture POST /mode/down > /dev/null
  wait_for 300 "palworld ルールが発火 (firing) する" state_is firing
  # 対照では firing から数秒で届いた。group_wait (1s) / group_interval (10s) より十分長く待つ。
  sleep 45
  fixture GET /log > "$WORK/muted-firing-log.json"
  jq -e 'length == 0' "$WORK/muted-firing-log.json" > /dev/null \
    || { echo "mute 中に FIRING が送られました: $(cat "$WORK/muted-firing-log.json")" >&2; exit 1; }
  echo "ok: firing 中も通知なし (評価は続き state=firing)"
  fixture POST /mode/ok > /dev/null
  wait_for 300 "palworld ルールが Normal に戻る" state_is inactive
  sleep 45
  fixture GET /log > "$WORK/muted-resolved-log.json"
  jq -e 'length == 0' "$WORK/muted-resolved-log.json" > /dev/null \
    || { echo "mute 中に RESOLVED が送られました: $(cat "$WORK/muted-resolved-log.json")" >&2; exit 1; }
  echo "ok: 解消後も通知なし (onitiku / boxlab とも 0 件)"
}

restart() {
  render
  kubectl -n "$NS" rollout restart deploy/grafana-fixture > /dev/null
  sleep 5
  wait_rollout 240
  verify_provisioned
}

diagnose() {
  set +e
  kubectl -n "$NS" get deploy,rs,pod,pvc -o wide
  kubectl -n "$NS" describe pod -l app=grafana-fixture | tail -30
  local pod
  for pod in $(kubectl -n "$NS" get pod -l app=grafana-fixture -o name); do
    echo "--- $pod (current)"; kubectl -n "$NS" logs "$pod" --tail=300 | grep -E 'provision|level=crit|level=error' | tail -10
    echo "--- $pod (previous)"; kubectl -n "$NS" logs "$pod" --previous --tail=300 2>/dev/null | grep -E 'provision|level=crit|level=error' | tail -10
  done
  fixture GET /log 2>/dev/null
  set -e
}

guard() { "$REPO_ROOT/tests/workflows/require-isolated-kind.sh"; }

case "${1:-}" in
  static) static_check ;;
  prepare) guard; prepare ;;
  control) guard; control ;;
  upgrade) guard; upgrade ;;
  muted) guard; muted ;;
  restart) guard; restart ;;
  diagnose) diagnose ;;
  *) echo "usage: $0 static|prepare|control|upgrade|muted|restart|diagnose" >&2; exit 2 ;;
esac
