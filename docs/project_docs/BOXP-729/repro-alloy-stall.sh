#!/usr/bin/env bash
# BOXP-729: Loki 停止中に収集対象ファイルが消えると Alloy の loki.source.file が
# 止まったままになるか、docker だけで再現・比較する（本番クラスタには触れない）。
#
# 使い方:
#   docs/project_docs/BOXP-729/repro-alloy-stall.sh <alloy-image-tag> <remove|keep>
#   例) ... v1.7.0 remove   # 本番で止まった版 + 対象削除あり
#       ... v1.7.0 keep     # 対照: 対象削除なし
#       ... v1.22.0 remove  # 更新後の版
#
# 手順: Loki と Alloy を起動 → 3 ファイルへ書き続ける → Loki を止める →
#       (remove のとき) 1 ファイルを消す → Loki を戻す → 残りのファイルの新着が
#       Loki に届くか、Alloy の送信件数が増えるか、Alloy の API が応答するかを見る。
set -euo pipefail

TAG="${1:?alloy image tag}"
MODE="${2:?remove|keep}"
LOKI_IMAGE="${LOKI_IMAGE:-grafana/loki:3.4.2}"
ID="boxp729-$$"
NET="$ID-net"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/$ID.XXXX")"

cleanup() {
  docker rm -f "$ID-loki" "$ID-alloy" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

cat >"$WORK/config.alloy" <<'EOF'
local.file_match "pods" {
  path_targets = [{"__path__" = "/logs/*.log", "job" = "repro"}]
  sync_period  = "2s"
}

loki.source.file "pods" {
  targets    = local.file_match.pods.targets
  forward_to = [loki.process.pods.receiver]
}

loki.process "pods" {
  forward_to = [loki.write.default.receiver]

  stage.drop {
    drop_counter_reason = "empty_line"
    expression          = "^\\s*$"
  }
}

loki.write "default" {
  endpoint {
    url = "http://loki:3100/loki/api/v1/push"
  }
}
EOF

docker network create "$NET" >/dev/null
start_loki() {
  docker run -d --name "$ID-loki" --network "$NET" --network-alias loki \
    "$LOKI_IMAGE" -config.file=/etc/loki/local-config.yaml >/dev/null
  for _ in $(seq 1 60); do
    if docker run --rm --network "$NET" curlimages/curl:8.10.1 -sf http://loki:3100/ready >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  echo "Loki が ready になりません" >&2
  return 1
}
start_loki

# docker デーモンが別ホストでも動くよう bind mount を使わず docker cp で設定を入れ、
# ログの書き込みも Alloy コンテナ内で行う。
docker create --name "$ID-alloy" --network "$NET" \
  "grafana/alloy:$TAG" run /etc/alloy/config.alloy \
  --server.http.listen-addr=0.0.0.0:12345 --storage.path=/tmp/alloy >/dev/null
docker cp "$WORK/config.alloy" "$ID-alloy:/etc/alloy/config.alloy"
docker start "$ID-alloy" >/dev/null
docker exec "$ID-alloy" mkdir -p /logs

# 1 行 ~200 byte を 1 ファイルあたり約 200 行/秒。Loki 停止中に loki.write が詰まる量。
# /logs/stop-<name> を作るとその writer は止まる。
start_writer() {
  docker exec -d "$ID-alloy" bash -c '
    f=/logs/$1.log; i=0; pad=$(printf "x%.0s" $(seq 1 180))
    while [ ! -e /logs/stop-$1 ]; do
      for _ in $(seq 1 20); do echo "$(date +%s%N) $1 $i $pad" >>"$f"; i=$((i + 1)); done
      sleep 0.1
    done' _ "$1"
}
for n in a b c; do start_writer "$n"; done

acurl() { docker run --rm --network "$NET" curlimages/curl:8.10.1 -s --max-time "${2:-5}" "http://$ID-alloy:12345$1"; }
sent() { acurl /metrics | awk '/^loki_write_sent_entries_total/ {s += $2} END {print s + 0}'; }
lcount() {
  # $1: file name。Loki に入っている行数（Loki は作り直すので再開後の分だけ）。
  docker run --rm --network "$NET" curlimages/curl:8.10.1 -s -G http://loki:3100/loki/api/v1/query \
    --data-urlencode "query=sum(count_over_time({job=\"repro\", filename=\"/logs/$1\"}[10m]))" \
    --data-urlencode "time=$(date +%s)" | sed -n 's/.*"value":\[[^,]*,"\([0-9]*\)".*/\1/p'
}

sleep 20
echo "[$TAG/$MODE] 起動後 sent=$(sent)"

echo "[$TAG/$MODE] Loki を停止"
docker stop "$ID-loki" >/dev/null
docker rm "$ID-loki" >/dev/null
sleep 25

if [ "$MODE" = remove ]; then
  echo "[$TAG/$MODE] Loki 停止中に c.log を削除（収集対象の削除）"
  docker exec "$ID-alloy" bash -c "touch /logs/stop-c; sleep 1; rm -f /logs/c.log"
fi
sleep 20

echo "[$TAG/$MODE] Loki を再開"
start_loki
# 再開直後は詰まっていた分が一度だけ流れることがあるので、30 秒後から 30 秒間の
# 増え方で「流れ続けているか」を判定する。a.log は書き続けている。
sleep 30
s0="$(sent)"; a0="$(lcount a.log)"
sleep 30
s1="$(sent)"; a1="$(lcount a.log)"
a_lines="$((${a1:-0} - ${a0:-0}))"
if acurl /api/v0/web/components 5 >/dev/null; then api=ok; else api=timeout; fi
echo "[$TAG/$MODE] 再開30秒後 sent 累計=$s0 再開後30〜60秒: sent 増分=$((s1 - s0)) Loki a.log 増分=${a_lines:-0} components API=$api"

# a.log には 30 秒で約 6,000 行書くので、その 1/3 未満しか届かなければ詰まりとみなす。
if [ "$((s1 - s0))" -gt 0 ] && [ "$a_lines" -ge 2000 ]; then
  echo "RESULT $TAG $MODE recovered"
else
  echo "RESULT $TAG $MODE stalled"
  docker logs --tail 15 "$ID-alloy" 2>&1 | sed 's/^/  alloy: /'
fi
