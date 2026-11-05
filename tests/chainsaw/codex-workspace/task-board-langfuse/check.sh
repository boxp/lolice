#!/usr/bin/env bash
set -euo pipefail
: "${REPO_ROOT:?}"
ns=task-board-langfuse-fixture
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
"$REPO_ROOT/tests/chainsaw/codex-workspace/task-board-langfuse/render-fixture.sh" > "$tmp"
kubectl apply -f "$tmp" >/dev/null
kubectl -n "$ns" wait --for=condition=Available deploy/sink --timeout=180s || { echo 'FAIL: sink が起動しない' >&2; exit 1; }
kubectl -n "$ns" wait --for=condition=Ready pod/exporter --timeout=180s || { echo 'FAIL: 鍵なしで sidecar が起動しない' >&2; exit 1; }
identity() { kubectl -n "$ns" get pod exporter -o json | jq -r '.metadata.uid + ":" + (.status.containerStatuses[] | select(.name == "task-board-langfuse-exporter") | (.restartCount | tostring) + ":" + (if .state.running then "Running" else "not-running" end))'; }
original=$(identity)
[[ $original == *:0:Running ]] || { echo 'FAIL: 鍵なしの sidecar が Running で restart 0 ではない' >&2; exit 1; }
# Observe more than one shortened poll interval before supplying a key.
deadline=$(( $(date +%s) + 5 ))
while (( $(date +%s) < deadline )); do
  kubectl -n "$ns" exec deploy/sink -- python -c 'import os; assert not os.path.exists("/tmp/requests.jsonl")' || { echo 'FAIL: 鍵なしで sink に送信した' >&2; exit 1; }
  [[ $(identity) == "$original" ]] || { echo 'FAIL: 鍵なしで sidecar が再起動した' >&2; exit 1; }
  sleep 1
done
pk=pk-lf-synthetic-428
sk=sk-lf-synthetic-428
auth="Basic $(printf '%s:%s' "$pk" "$sk" | base64 | tr -d '\n')"
kubectl -n "$ns" create secret generic langfuse-ingest-key --from-literal=LANGFUSE_PUBLIC_KEY="$pk" --from-literal=LANGFUSE_SECRET_KEY="$sk" >/dev/null
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
wait_for '鍵追加後に exporter の cutoff が初期化されない' state_exists
# The run id is after cutoff; finished-at is already settled. Only synthetic values are used.
seed_run() {
  kubectl -n "$ns" exec exporter -c helper -- python -c '
import datetime, pathlib, sys
now = datetime.datetime.now(datetime.timezone.utc)
run = (now + datetime.timedelta(seconds=3)).strftime("%Y%m%dT%H%M%SZ") + "-" + sys.argv[1]
root = pathlib.Path("/home/.codex-task-board")
d = root / "runs" / "BOXP-428" / run
d.mkdir(parents=True)
finished = (now - datetime.timedelta(seconds=120)).isoformat().replace("+00:00", "Z")
(d / "summary.edn").write_text("{:status :done :agent \"claude\" :finished-at \"" + finished + "\"}")
# The exporter masks a line that names a secret and the nearest non-blank lines, so the
# clean marker sits two lines away: it proves the body arrives while the secret does not.
(d / "prompt.md").write_text("cleanbodymarker-" + sys.argv[1] + "\nplain line one\nplain line two\npassword=syntheticpassword428\nplain line three\nsyntheticgeminivalue428\n")
(d / "last-message.md").write_text("synthetic completion")
(d / "events.jsonl").write_text("{}\n")
project = "-home-boxp--codex-task-board-workspaces-BOXP-428-" + run
p = pathlib.Path("/home/.claude/projects") / project
p.mkdir(parents=True, exist_ok=True)
(p / "session.jsonl").write_text("{}\n")
' "$1"
}
[[ $(dir_owner /home/.codex-task-board /home/.claude /home/.claude/projects /home/.codex /home/.codex/sessions | sort -u) == 1000 ]] || { echo "FAIL: exporter の subPath が UID 1000 所有ではない" >&2; exit 1; }
seed_run first
received() { kubectl -n "$ns" exec deploy/sink -- python -c 'import json,sys; p="/tmp/requests.jsonl"; rows=[json.loads(x) for x in open(p)]; assert any(r["authorization"] == sys.argv[1] and sys.argv[2] in r["body"] for r in rows)' "$auth" "$1" 2>/dev/null; }
wait_for '後発の鍵による Basic 認証付き trace が届かない' received cleanbodymarker-first
[[ $(identity) == "$original" ]] || { echo 'FAIL: 鍵追加で sidecar が再起動した' >&2; exit 1; }
kubectl -n "$ns" exec deploy/sink -- python -c 'import json; rows=[json.loads(x) for x in open("/tmp/requests.jsonl")]; assert all(all(s not in r["body"] for s in ("syntheticpassword428", "syntheticgeminivalue428")) for r in rows)' || { echo 'FAIL: 合成秘密値が sink に届いた' >&2; exit 1; }
kubectl -n "$ns" exec exporter -c task-board-langfuse-exporter -- bash -c 'test ! -e /home/boxp/.ssh/marker && ! touch /home/boxp/.claude/projects/test && ! touch /home/boxp/.codex/sessions/test' || { echo 'FAIL: home の隔離または readOnly mount が機能しない' >&2; exit 1; }
kubectl -n "$ns" scale deploy/sink --replicas=0 >/dev/null
kubectl -n "$ns" wait --for=delete pod -l app=sink --timeout=60s || { echo 'FAIL: sink が停止しない' >&2; exit 1; }
seed_run second
spooled() { kubectl -n "$ns" exec exporter -c helper -- sh -c 'find /home/.codex-task-board/langfuse-exporter/spool -name "*.json" | grep -q .'; }
wait_for 'sink 停止中の run が spool に保留されない' spooled
[[ $(identity) == "$original" ]] || { echo 'FAIL: sink 停止で sidecar が再起動した' >&2; exit 1; }
kubectl -n "$ns" scale deploy/sink --replicas=1 >/dev/null
kubectl -n "$ns" wait --for=condition=Available deploy/sink --timeout=180s || { echo 'FAIL: sink が復帰しない' >&2; exit 1; }
wait_for 'sink 復帰後に spool の trace が届かない' received cleanbodymarker-second
[[ $(identity) == "$original" ]] || { echo 'FAIL: sink 復帰で sidecar が再起動した' >&2; exit 1; }
