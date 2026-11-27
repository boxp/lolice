#!/bin/bash
# BOXP-721: agent 通信監査の Alloy パイプライン（DNS 名の伏字・接続ログの抽出）を
# 本物の Alloy で動かして確かめる。期待値は Obsidian の現行仕様
# Projects/agent-egress-audit/spec.md の例から取っている（実装の出力から写していない）。
#
# 使い方: tests/agent-egress-audit/test_alloy_pipeline.sh
# 前提: docker が使えること。ALLOY_IMAGE で image を差し替えられる。
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
ALLOY_IMAGE="${ALLOY_IMAGE:-docker.io/grafana/alloy:v1.7.0}"
CONFIGMAP="$REPO_DIR/argoproj/grafana-alloy/configmap.yaml"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/alloy-audit-test.XXXXXX")"
CONTAINER=""
cleanup() {
  if [ -n "$CONTAINER" ]; then docker rm -f "$CONTAINER" >/dev/null 2>&1 || true; fi
  rm -rf "$WORK"
}
trap cleanup EXIT

PASS=0
FAIL=0
pass() { PASS=$((PASS + 1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL + 1)); echo "FAIL: $1"; }

# ConfigMap から config.alloy を取り出し、印の間の stage だけを抜く。
sed -n '/^  config.alloy: |$/,$ { /^  config.alloy: |$/d; s/^    //; p; }' "$CONFIGMAP" > "$WORK/config.alloy"
extract() {
  sed -n "/\/\/ BEGIN BOXP-721 $1/,/\/\/ END BOXP-721 $1/p" "$WORK/config.alloy"
}
DNS_BLOCK="$(extract dns-redaction)"
CONN_BLOCK="$(extract conn-filter)"
CONN_SOURCE="$(extract conn-source)"
if [ -z "$DNS_BLOCK" ]; then fail "config.alloy に 'BEGIN BOXP-721 dns-redaction' の stage がある"; fi
if [ -z "$CONN_BLOCK" ]; then fail "config.alloy に 'BEGIN BOXP-721 conn-filter' の stage がある"; fi
if [ -z "$CONN_SOURCE" ]; then fail "config.alloy に 'BEGIN BOXP-721 conn-source' の source がある"; fi
if [ "$FAIL" -ne 0 ]; then echo "RESULT: $PASS passed, $FAIL failed"; exit 1; fi

mkdir -p "$WORK/fixtures"
# 仕様の例（resolver の出力 = CRI の中身）
cat > "$WORK/fixtures/dns.log" <<'EOF'
[INFO] 192.178.60.152 udp A api.anthropic.com. NOERROR 120 0.012s
[INFO] 192.178.60.152 udp A Ym94cDYzMS1jYW5hcnktc2VjcmV0LXZhbHVl.canary.b0xp.io. NXDOMAIN 110 0.020s
[INFO] 192.178.60.152 udp A abcdefghijklmnopqrstuvw.example.com. NXDOMAIN 110 0.020s
[INFO] 192.178.60.152 udp TXT aaaaaaaaaaaaaaaaaaaaaaaa.bbbbbbbbbbbbbbbbbbbbbbbbbbbb.example. NXDOMAIN 110 0.020s
EOF
# 他の Pod のログは伏字にしない
cat > "$WORK/fixtures/other.log" <<'EOF'
other-app Ym94cDYzMS1jYW5hcnktc2VjcmV0LXZhbHVl unchanged
EOF
# kernel log: calico-packet の行だけ残す
cat > "$WORK/fixtures/kern.log" <<'EOF'
calico-packet: IN=cali1234 OUT=enp1s0 MAC=aa:bb SRC=192.178.60.152 DST=203.0.113.10 LEN=60 TOS=0x00 PREC=0x00 TTL=63 ID=1 DF PROTO=TCP SPT=43210 DPT=443 WINDOW=64240 RES=0x00 SYN URGP=0
Nov 27 02:00:00 golyat-1 kernel: [123456.789012] calico-packet: IN=cali1234 OUT=enp1s0 SRC=192.178.60.152 DST=198.51.100.7 LEN=60 PROTO=UDP SPT=40000 DPT=53 LEN=40
usb 1-1: new high-speed USB device number 3 using xhci_hcd
EOF

{
  cat <<'EOF'
loki.source.file "dns" {
  targets = [
    {"__path__" = "/work/fixtures/dns.log", "namespace" = "agent-egress-audit", "container" = "coredns", "case" = "dns"},
    {"__path__" = "/work/fixtures/other.log", "namespace" = "other", "container" = "app", "case" = "other"},
  ]
  forward_to = [loki.process.dns.receiver]
  tail_from_end = false
}
loki.process "dns" {
  forward_to = [loki.echo.out.receiver]
EOF
  echo "$DNS_BLOCK"
  cat <<'EOF'
}
loki.source.file "conn" {
  targets = [
    {"__path__" = "/work/fixtures/kern.log", "job" = "agent-egress-audit/conn", "case" = "conn"},
  ]
  forward_to = [loki.process.conn.receiver]
  tail_from_end = false
}
loki.process "conn" {
  forward_to = [loki.echo.out.receiver]
EOF
  echo "$CONN_BLOCK"
  cat <<'EOF'
}
EOF
  # 本番の source 節（/var/log/kern.log。コンテナ内には無い）をそのまま入れ、
  # ファイルが無くても設定の評価に失敗しないことを確かめる。
  echo "$CONN_SOURCE"
  cat <<'EOF'
loki.process "agent_egress_conn" {
  forward_to = [loki.echo.out.receiver]
}
loki.echo "out" {}
EOF
} > "$WORK/harness.alloy"

CONTAINER="$(docker create -e HOSTNAME=test-node "$ALLOY_IMAGE" run /work/harness.alloy --storage.path=/tmp/alloy)"
docker cp "$WORK/." "$CONTAINER:/work" >/dev/null
docker start "$CONTAINER" >/dev/null
sleep 8
docker logs "$CONTAINER" > "$WORK/out.log" 2>&1 || true

entries() { grep 'component_id=loki.echo.out' "$WORK/out.log" | grep -o 'entry="[^"]*"' | sed 's/^entry="//; s/"$//'; }
has_entry() { entries | grep -qxF -- "$1"; }

if grep -q 'level=error' "$WORK/out.log"; then
  fail "Alloy が設定を読み込める"; grep 'level=error' "$WORK/out.log" | head -5
fi

check() {
  if has_entry "$2"; then pass "$1"; else fail "$1 (expected: $2)"; fi
}
check "通常のドメイン名は変えない" \
  '[INFO] 192.178.60.152 udp A api.anthropic.com. NOERROR 120 0.012s'
check "24文字以上のラベルは長さだけ残して伏字" \
  '[INFO] 192.178.60.152 udp A <redacted-label-len=36>.canary.b0xp.io. NXDOMAIN 110 0.020s'
check "23文字のラベルは残る" \
  '[INFO] 192.178.60.152 udp A abcdefghijklmnopqrstuvw.example.com. NXDOMAIN 110 0.020s'
check "複数の長いラベルはそれぞれ伏字" \
  '[INFO] 192.178.60.152 udp TXT <redacted-label-len=24>.<redacted-label-len=28>.example. NXDOMAIN 110 0.020s'
check "他の Pod のログは伏字にしない" \
  'other-app Ym94cDYzMS1jYW5hcnktc2VjcmV0LXZhbHVl unchanged'
check "rsyslog 形式（時刻・host 付き）の calico-packet 行も残す" \
  'Nov 27 02:00:00 golyat-1 kernel: [123456.789012] calico-packet: IN=cali1234 OUT=enp1s0 SRC=192.178.60.152 DST=198.51.100.7 LEN=60 PROTO=UDP SPT=40000 DPT=53 LEN=40'
if grep -q 'loki.source.file.agent_egress_conn' "$WORK/out.log" && ! grep -q 'level=error' "$WORK/out.log"; then
  pass "kern.log が無いノードでも設定の評価に失敗しない"
else
  fail "kern.log が無いノードでも設定の評価に失敗しない"
fi
if entries | grep -q 'Ym94cDYzMS1jYW5hcnktc2VjcmV0LXZhbHVl.canary'; then
  fail "伏字前の canary ラベルが DNS の出力に残らない"
else
  pass "伏字前の canary ラベルが DNS の出力に残らない"
fi
check "calico-packet の行は残す" \
  'calico-packet: IN=cali1234 OUT=enp1s0 MAC=aa:bb SRC=192.178.60.152 DST=203.0.113.10 LEN=60 TOS=0x00 PREC=0x00 TTL=63 ID=1 DF PROTO=TCP SPT=43210 DPT=443 WINDOW=64240 RES=0x00 SYN URGP=0'
if entries | grep -q 'usb 1-1'; then
  fail "calico-packet 以外の kernel log は捨てる"
else
  pass "calico-packet 以外の kernel log は捨てる"
fi

# ---------------------------------------------------------------------------
# BOXP-726: golyat-1 は kern.log が無いので journal から接続ログを読む。
# 期待値は現行仕様「接続の読み元」の例から取っている。
JOURNAL_BLOCK="$(sed -n '/\/\/ BEGIN BOXP-726 conn-journal/,/\/\/ END BOXP-726 conn-journal/p' "$WORK/config.alloy")"
if [ -z "$JOURNAL_BLOCK" ]; then
  fail "config.alloy に 'BEGIN BOXP-726 conn-journal' の source がある"
  echo "RESULT: $PASS passed, $FAIL failed"; exit 1
fi

# 試験用の journal を systemd-journal-remote で作る（machine-id は Alloy の image と違う値。
# 本番でもホストの machine-id は Alloy コンテナのものと違う）。
JOURNAL_IMAGE="${JOURNAL_IMAGE:-docker.io/library/ubuntu:24.04}"
MID=22222222222222222222222222222222
NOW_US="$(date +%s)000000"
mkdir -p "$WORK/journal-in"
{
  i=0
  emit() { # transport hostname message
    i=$((i + 1))
    printf '__REALTIME_TIMESTAMP=%s\n__MONOTONIC_TIMESTAMP=%s\n_BOOT_ID=11111111111111111111111111111111\n_MACHINE_ID=%s\n_TRANSPORT=%s\n_HOSTNAME=%s\nSYSLOG_IDENTIFIER=kernel\n_COMM=secret-comm-name\nMESSAGE=%s\n\n' \
      "$((NOW_US - 60000000 + i * 1000))" "$((i * 1000))" "$MID" "$1" "$2" "$3"
  }
  emit kernel golyat-1 'calico-packet: IN=cali1234 OUT=enp1s0 SRC=192.178.60.187 DST=203.0.113.20 LEN=60 PROTO=TCP SPT=43211 DPT=443 SYN URGP=0'
  emit kernel golyat-1 'usb 1-1: new high-speed USB device number 3 using xhci_hcd'
  emit syslog golyat-1 'calico-packet: forged-from-userspace SRC=192.0.2.1 DST=192.0.2.2'
} > "$WORK/journal-in/in.export"
JB="$(docker create "$JOURNAL_IMAGE" sh -c "apt-get update -qq >/dev/null && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq systemd-journal-remote >/dev/null 2>&1 && mkdir -p /out/journal/$MID && /lib/systemd/systemd-journal-remote --output=/out/journal/$MID/system.journal /in/in.export")"
docker cp "$WORK/journal-in" "$JB:/in" >/dev/null
if docker start -a "$JB" >/dev/null 2>&1 && docker cp "$JB:/out/journal" "$WORK/journal" >/dev/null 2>&1; then
  docker rm -f "$JB" >/dev/null 2>&1 || true
else
  docker rm -f "$JB" >/dev/null 2>&1 || true
  fail "試験用の journal を作れる（$JOURNAL_IMAGE と apt が必要）"
  echo "RESULT: $PASS passed, $FAIL failed"; exit 1
fi

{
  # 本番の kern.log の source と journal の source をそのまま入れ、抽出は本番の conn-filter。
  echo "$CONN_SOURCE"
  echo "$JOURNAL_BLOCK"
  echo 'loki.process "agent_egress_conn" {'
  echo '  forward_to = [loki.echo.out.receiver]'
  echo "$CONN_BLOCK"
  echo '}'
  echo 'loki.echo "out" {}'
} > "$WORK/journal-harness.alloy"

# run_journal_case NODE WITH_JOURNAL -> $WORK/journal-NODE-WITH.log
run_journal_case() {
  local c out="$WORK/journal-$1-$2.log"
  c="$(docker create -e HOSTNAME="$1" "$ALLOY_IMAGE" run /work/journal-harness.alloy --storage.path=/tmp/alloy)"
  docker cp "$WORK/journal-harness.alloy" "$c:/work/journal-harness.alloy" >/dev/null 2>&1 || {
    docker cp "$WORK/." "$c:/work" >/dev/null; }
  if [ "$2" = yes ]; then docker cp "$WORK/journal" "$c:/var/log/journal" >/dev/null; fi
  docker start "$c" >/dev/null
  sleep 8
  echo "running=$(docker inspect -f '{{.State.Running}}' "$c")" > "$out"
  docker logs "$c" >> "$out" 2>&1 || true
  docker rm -f "$c" >/dev/null 2>&1 || true
}
jentries() { grep 'component_id=loki.echo.out' "$1" | grep -o 'entry=.*'; }

run_journal_case golyat-1 yes
G1="$WORK/journal-golyat-1-yes.log"
if grep -q '^running=true' "$G1" && ! grep -q 'level=error' "$G1"; then
  pass "golyat-1: journal がある時は設定の読込に成功する"
else
  fail "golyat-1: journal がある時は設定の読込に成功する"; grep -E 'level=error|Error' "$G1" | head -3
fi
if jentries "$G1" | grep -qF 'entry="calico-packet: IN=cali1234 OUT=enp1s0 SRC=192.178.60.187 DST=203.0.113.20 LEN=60 PROTO=TCP SPT=43211 DPT=443 SYN URGP=0"'; then
  pass "golyat-1: journal の kernel の calico-packet 行が入る"
else
  fail "golyat-1: journal の kernel の calico-packet 行が入る"
fi
if jentries "$G1" | grep -F 'DST=203.0.113.20' | grep -q 'node=\\"golyat-1\\"'; then
  pass "golyat-1: journal の行に node=golyat-1 が付く"
else
  fail "golyat-1: journal の行に node=golyat-1 が付く"; jentries "$G1" | head -3
fi
if jentries "$G1" | grep -q 'usb 1-1'; then fail "golyat-1: calico-packet 以外の kernel 行は入らない"; else pass "golyat-1: calico-packet 以外の kernel 行は入らない"; fi
if jentries "$G1" | grep -q 'forged-from-userspace'; then fail "golyat-1: kernel 以外（syslog）の calico-packet 行は入らない"; else pass "golyat-1: kernel 以外（syslog）の calico-packet 行は入らない"; fi
if jentries "$G1" | grep -q 'secret-comm-name\|_COMM\|comm='; then fail "golyat-1: journal の他の項目は label に付かない"; else pass "golyat-1: journal の他の項目は label に付かない"; fi

for J in yes no; do
  run_journal_case golyat-4 "$J"
  G4="$WORK/journal-golyat-4-$J.log"
  if grep -q '^running=true' "$G4" && ! grep -q 'level=error' "$G4"; then
    pass "golyat-4 (journal=$J): 設定の読込に成功する"
  else
    fail "golyat-4 (journal=$J): 設定の読込に成功する"; grep -E 'level=error|Error' "$G4" | head -3
  fi
  if jentries "$G4" | grep -q 'calico-packet'; then
    fail "golyat-4 (journal=$J): journal からは何も入らない"
  else
    pass "golyat-4 (journal=$J): journal からは何も入らない"
  fi
done

echo "RESULT: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
