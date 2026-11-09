#!/usr/bin/env bash
set -euo pipefail
: "${REPO_ROOT:?}"
ns=task-board-langfuse-fixture
suite="$REPO_ROOT/tests/chainsaw/codex-workspace/task-board-langfuse"
identity() { kubectl -n "$ns" get pod exporter -o json | jq -r '.metadata.uid + ":" + (.status.containerStatuses[] | select(.name == "task-board-langfuse-exporter") | (.restartCount | tostring) + ":" + (if .state.running then "Running" else "not-running" end))'; }
baseline() { kubectl -n "$ns" get configmap scenario-state -o jsonpath='{.data.identity}'; }
record_identity() {
  local original
  original=$(identity)
  [[ $original == *:0:Running ]] || { echo 'FAIL: 鍵なしの sidecar が Running で restart 0 ではない' >&2; exit 1; }
  kubectl -n "$ns" create configmap scenario-state --from-literal=identity="$original" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
}
assert_identity() { [[ $(identity) == "$(baseline)" ]] || { echo "FAIL: $1" >&2; exit 1; }; }
wait_for() {
  local message=$1; shift
  local deadline=$(( $(date +%s) + 180 ))
  until "$@"; do
    (( $(date +%s) < deadline )) || { echo "FAIL: $message" >&2; exit 1; }
    sleep 2
  done
}
dir_owner() { kubectl -n "$ns" exec exporter -c helper -- stat -c %u "$@"; }
state_exists() { kubectl -n "$ns" exec exporter -c helper -- test -f /home/.codex-task-board/langfuse-exporter/state.edn; }
seed_run() { kubectl -n "$ns" exec -i exporter -c helper -- python - "$1" < "$suite/seed-run.py"; }
received() { kubectl -n "$ns" exec -i deploy/sink -- python - "$auth" "$1" < "$suite/received.py" 2>/dev/null; }
spooled() { kubectl -n "$ns" exec exporter -c helper -- sh -c 'find /home/.codex-task-board/langfuse-exporter/spool -name "*.json" | grep -q .'; }
pk=pk-lf-synthetic-428
sk=sk-lf-synthetic-428
auth="Basic $(printf '%s:%s' "$pk" "$sk" | base64 | tr -d '\n')"
case "${1:?section required}" in
 deploy) tmp=$(mktemp); trap 'rm -f "$tmp"' EXIT; "$suite/render-fixture.sh" > "$tmp"; kubectl apply -f "$tmp" >/dev/null ;;
 record_identity) record_identity ;;
 observe)
# Observe more than one shortened poll interval before supplying a key.
deadline=$(( $(date +%s) + 5 ))
while (( $(date +%s) < deadline )); do
  kubectl -n "$ns" exec deploy/sink -- python -c 'import os; assert not os.path.exists("/tmp/requests.jsonl")' || { echo 'FAIL: 鍵なしで sink に送信した' >&2; exit 1; }
  [[ $(identity) == "$(baseline)" ]] || { echo 'FAIL: 鍵なしで sidecar が再起動した' >&2; exit 1; }
  sleep 1
done
 ;;
 state) wait_for '鍵追加後に exporter の cutoff が初期化されない' state_exists ;;
 owner) [[ $(dir_owner /home/.codex-task-board /home/.claude /home/.claude/projects /home/.codex /home/.codex/sessions | sort -u) == 1000 ]] || { echo "FAIL: exporter の subPath が UID 1000 所有ではない" >&2; exit 1; } ;;
 seed_first) seed_run first ;;
 received_first) wait_for '後発の鍵による Basic 認証付き trace が届かない' received cleanbodymarker-first ;;
 identity_key) assert_identity '鍵追加で sidecar が再起動した' ;;
 secrets) kubectl -n "$ns" exec -i deploy/sink -- python - < "$suite/no-secret-values.py" || { echo 'FAIL: 合成秘密値が sink に届いた' >&2; exit 1; } ;;
 home) kubectl -n "$ns" exec exporter -c task-board-langfuse-exporter -- bash -c 'test ! -e /home/boxp/.ssh/marker && ! touch /home/boxp/.claude/projects/test && ! touch /home/boxp/.codex/sessions/test' || { echo 'FAIL: home の隔離または readOnly mount が機能しない' >&2; exit 1; } ;;
 seed_second) seed_run second ;;
 spool) wait_for 'sink 停止中の run が spool に保留されない' spooled ;;
 identity_stopped) assert_identity 'sink 停止で sidecar が再起動した' ;;
 delivered) wait_for 'sink 復帰後に spool の trace が届かない' received cleanbodymarker-second ;;
 identity_restored) assert_identity 'sink 復帰で sidecar が再起動した' ;;
 *) echo "unknown section: $1" >&2; exit 1 ;;
esac
