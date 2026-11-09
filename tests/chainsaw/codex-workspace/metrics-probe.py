"""ai-ops-metrics container 内で /healthz と /metrics の応答を確認する (kubectl exec -i ... python -)。"""
import urllib.request as u

BASE = "http://127.0.0.1:9464"

health = u.urlopen(f"{BASE}/healthz").read().strip()
assert health == b"ok", f"/healthz returned {health!r}"

metrics = u.urlopen(f"{BASE}/metrics").read()
for needle in (
    b"ai_ops_stub_metric 1",
    b"ai_ops_metrics_file_present 1",
    b'ai_prepaid_estimate_state{provider="gemini_api",state="ok"} 1',
    b"ai_prepaid_balance_actual_state",
):
    assert needle in metrics, f"/metrics に {needle!r} がありません"
