import json
rows = [json.loads(x) for x in open("/tmp/requests.jsonl")]
assert all(all(s not in r["body"] for s in ("syntheticpassword428", "syntheticgrafanavalue428")) for r in rows)
