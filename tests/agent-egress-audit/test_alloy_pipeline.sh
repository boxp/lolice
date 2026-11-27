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
if [ -z "$DNS_BLOCK" ]; then fail "config.alloy に 'BEGIN BOXP-721 dns-redaction' の stage がある"; fi
if [ -z "$CONN_BLOCK" ]; then fail "config.alloy に 'BEGIN BOXP-721 conn-filter' の stage がある"; fi
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

echo "RESULT: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
