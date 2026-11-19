#!/usr/bin/env bash
# BOXP-623: Grafana のデータソース provisioning が、UI で作られた既存データソースを持つ
# DB に対して起動できることを確かめる (#1303 の CrashLoop の回帰テスト)。
#
#   static   : 本番 render の provisioning と ui-datasources.json を照合する (kind 不要)
#   prepare  : 本番と同じ Grafana イメージを PVC 付きで provisioning なしに起動し、
#              ui-datasources.json のデータソースを API (UI と同じ経路) で作る
#   upgrade  : 本番 render の grafana-datasources を mount して同じ PVC のまま再起動し、
#              起動・既存行の引き継ぎ・url などの上書きを確かめる
#   restart  : provisioning 済みの状態からもう一度再起動しても起動できることを確かめる
#   diagnose : 失敗時の状態とログを出す
#
# 本番 PVC やノード固有の事情 (Longhorn / multipath など) は扱わない。kind の local-path PVC で
# 「既存 DB を持ったまま設定を変えて再起動する」流れだけを再現する。
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(git rev-parse --show-toplevel)}"
HERE="$REPO_ROOT/tests/chainsaw/prometheus-operator"
NS="${GRAFANA_DS_TEST_NAMESPACE:-chainsaw-grafana-ds}"
WORK="${GRAFANA_DS_TEST_WORKDIR:-${TMPDIR:-/tmp}/grafana-ds-test}"
FIXTURE="$HERE/ui-datasources.json"
ADMIN_PASSWORD="chainsaw-admin"

mkdir -p "$WORK"

render() {
  # 本番と同じ kustomize render から Grafana のイメージと datasources.yaml を取り出す。
  # 前回の render を使い回すと、変更後の定義を検証しないまま通ってしまうので毎回作り直す。
  kustomize build "$REPO_ROOT/argoproj/prometheus-operator" > "$WORK/rendered.yaml"
  yq -r 'select(.kind == "Deployment" and .metadata.name == "grafana") | .spec.template.spec.containers[] | select(.name == "grafana") | .image' \
    "$WORK/rendered.yaml" > "$WORK/image"
  yq -r 'select(.kind == "Secret" and .metadata.name == "grafana-datasources") | .stringData["datasources.yaml"]' \
    "$WORK/rendered.yaml" > "$WORK/datasources.yaml"
  test -s "$WORK/image" || { echo "grafana Deployment のイメージを render から取り出せません" >&2; exit 1; }
  grep -q '^apiVersion: 1' "$WORK/datasources.yaml" || { echo "grafana-datasources の datasources.yaml を取り出せません" >&2; exit 1; }
}

static_check() {
  render
  local fail=0 name uid want
  while IFS=$'\t' read -r name uid; do
    want="$(yq -r ".datasources[] | select(.name == \"$name\") | .uid // \"\"" "$WORK/datasources.yaml")"
    if [ -z "$want" ]; then
      # provisioning 側が uid を省略した場合も、Grafana は名前で既存行を見つけて更新する。
      continue
    fi
    if [ "$want" != "$uid" ]; then
      echo "NG: provisioning の '$name' が uid=$want を宣言しているが、本番 DB の既存 (UI 作成) は uid=$uid。" >&2
      echo "    Grafana は起動時に 'Datasource provisioning error: data source not found' で終了する。uid を $uid に合わせること。" >&2
      fail=1
    else
      echo "ok: '$name' は既存 uid=$uid を引き継ぐ"
    fi
  done < <(jq -r '.datasources[] | [.name, .uid] | @tsv' "$FIXTURE")
  return "$fail"
}

grafana_exec() {
  kubectl -n "$NS" exec deploy/grafana-fixture -c grafana -- "$@"
}

api() {
  # api METHOD PATH [JSON]
  local method="$1" path="$2" body="${3:-}"
  if [ -n "$body" ]; then
    grafana_exec curl -fsS -u "admin:$ADMIN_PASSWORD" -X "$method" -H 'Content-Type: application/json' \
      --data "$body" "http://127.0.0.1:3000$path"
  else
    grafana_exec curl -fsS -u "admin:$ADMIN_PASSWORD" -X "$method" "http://127.0.0.1:3000$path"
  fi
}

wait_rollout() {
  # 起動直後に終了して再起動を繰り返す (#1303 の CrashLoop) なら timeout を待たずに失敗させる。
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
    if api GET /api/health > "$WORK/health.json" 2>/dev/null && jq -e '.database == "ok"' "$WORK/health.json" > /dev/null; then
      return 0
    fi
    sleep 2
  done
  echo "Grafana の /api/health が ok になりません" >&2
  diagnose
  return 1
}

apply_fixture() {
  # $1: with-provisioning なら grafana-datasources を mount する
  local image; image="$(cat "$WORK/image")"
  kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f - > /dev/null
  kubectl -n "$NS" create secret generic grafana-datasources \
    --from-file=datasources.yaml="$WORK/datasources.yaml" --dry-run=client -o yaml | kubectl apply -f - > /dev/null
  local mounts="" volumes=""
  if [ "${1:-}" = with-provisioning ]; then
    mounts='
        - name: grafana-datasources
          mountPath: /etc/grafana/provisioning/datasources
          readOnly: true'
    volumes='
      - name: grafana-datasources
        secret:
          secretName: grafana-datasources'
  fi
  kubectl apply -f - > /dev/null <<YAML
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
  # 本番と同じく RWO の PVC を持つので、旧 Pod を止めてから新 Pod を起動する。
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
        - name: CLICKHOUSE_GRAFANA_PASSWORD
          value: chainsaw-dummy
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
}

prepare() {
  render
  apply_fixture
  wait_rollout 300
  local ds
  while IFS= read -r ds; do
    api POST /api/datasources "$ds" > /dev/null
  done < <(jq -c '.datasources[]' "$FIXTURE")
  api GET /api/datasources > "$WORK/before.json"
  jq -e --slurpfile f "$FIXTURE" \
    '[$f[0].datasources[] as $w | any(.[]; .name == $w.name and .uid == $w.uid and .readOnly == false)] | all' \
    "$WORK/before.json" > /dev/null || { echo "UI 作成相当のデータソースを用意できません" >&2; cat "$WORK/before.json" >&2; exit 1; }
  echo "既存 DB を用意: $(jq -c '[.[] | {id, name, uid, url, readOnly}]' "$WORK/before.json")"
}

verify_after() {
  api GET /api/datasources > "$WORK/after.json"
  echo "provisioning 後: $(jq -c '[.[] | {id, name, uid, url, readOnly}]' "$WORK/after.json")"
  # UI 作成分は同じ行 (id / uid) のまま provisioning の内容で上書きされる。
  jq -e --slurpfile b "$WORK/before.json" --slurpfile f "$FIXTURE" '
    . as $after
    | [$f[0].datasources[] as $w
       | ($b[0][] | select(.name == $w.name)) as $old
       | ($after[] | select(.name == $w.name)) as $new
       | $new.id == $old.id and $new.uid == $old.uid and $new.readOnly == true]
    | length > 0 and all' "$WORK/after.json" > /dev/null \
    || { echo "UI 作成の既存データソースが同じ行のまま provisioning に引き継がれていません" >&2; exit 1; }
  # provisioning の全データソースが同名 1 件ずつあり、url と uid (宣言時) が一致する。
  local name url uid
  while IFS='|' read -r name url uid; do
    jq -e --arg n "$name" --arg u "$url" --arg id "$uid" \
      '[.[] | select(.name == $n)] | length == 1 and (.[0].url == $u) and ($id == "" or .[0].uid == $id)' \
      "$WORK/after.json" > /dev/null || { echo "provisioning の '$name' が反映されていません" >&2; exit 1; }
  done < <(yq -r '.datasources[] | [.name, (.url // ""), (.uid // "")] | join("|")' "$WORK/datasources.yaml")
  # alertmanager は Grafana 管理ルールを受けない (二重通知をしない)
  jq -e '[.[] | select(.type == "alertmanager")] | all(.jsonData.handleGrafanaManagedAlerts == false)' \
    "$WORK/after.json" > /dev/null || { echo "alertmanager データソースが Grafana 管理ルールを受ける設定です" >&2; exit 1; }
}

upgrade() {
  render
  apply_fixture with-provisioning
  wait_rollout 240
  verify_after
}

restart() {
  render
  kubectl -n "$NS" rollout restart deploy/grafana-fixture > /dev/null
  wait_rollout 240
  verify_after
}

diagnose() {
  set +e
  kubectl -n "$NS" get deploy,rs,pod,pvc -o wide
  kubectl -n "$NS" describe pod -l app=grafana-fixture | tail -30
  local pod
  for pod in $(kubectl -n "$NS" get pod -l app=grafana-fixture -o name); do
    echo "--- $pod (current)"; kubectl -n "$NS" logs "$pod" --tail=200 | grep -E 'Failed to provision|provisioning error|level=crit' | head -5
    echo "--- $pod (previous)"; kubectl -n "$NS" logs "$pod" --previous --tail=200 2>/dev/null | grep -E 'Failed to provision|provisioning error|level=crit' | head -5
  done
  set -e
}

guard() { "$REPO_ROOT/tests/workflows/require-isolated-kind.sh"; }

case "${1:-}" in
  static) static_check ;;
  prepare) guard; prepare ;;
  upgrade) guard; upgrade ;;
  restart) guard; restart ;;
  diagnose) diagnose ;;
  *) echo "usage: $0 static|prepare|upgrade|restart|diagnose" >&2; exit 2 ;;
esac
