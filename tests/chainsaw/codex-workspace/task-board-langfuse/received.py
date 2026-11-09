import json, sys
p = "/tmp/requests.jsonl"
rows = [json.loads(x) for x in open(p)]
assert any(r["authorization"] == sys.argv[1] and sys.argv[2] in r["body"] for r in rows)
