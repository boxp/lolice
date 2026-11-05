"""Run inside a probe Pod: send synthetic OTLP traces and read them back through the public API."""
import base64, hashlib, json, os, sys, time, urllib.error, urllib.request

BASE = "http://langfuse-web.langfuse.svc.cluster.local:3000"
AUTH = "Basic " + base64.b64encode(f"{os.environ['LF_PK']}:{os.environ['LF_SK']}".encode()).decode()


def call(path, body=None, auth=AUTH, method=None):
    headers = {"Content-Type": "application/json", "x-langfuse-ingestion-version": "4"}
    if auth:
        headers["Authorization"] = auth
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return error.code, {}


def attr(key, value):
    return {"key": key, "value": {"stringValue": value}}


def send(trace, age_days, public, marker):
    start = int((time.time() - float(age_days) * 86400) * 1e9)
    common = [attr("langfuse.session.id", "chainsaw"), attr("langfuse.trace.name", "chainsaw")]
    if public == "1":
        common.append({"key": "langfuse.trace.public", "value": {"boolValue": True}})
    root, child = (hashlib.sha256(f"{trace}:{name}".encode()).hexdigest()[:16] for name in ("root", "child"))
    spans = [
        {"traceId": trace, "spanId": root, "name": "root", "kind": 1, "startTimeUnixNano": str(start),
         "endTimeUnixNano": str(start + 10**9), "attributes": common + [attr("langfuse.observation.input", marker)]},
        {"traceId": trace, "spanId": child, "parentSpanId": root, "name": "child", "kind": 1,
         "startTimeUnixNano": str(start), "endTimeUnixNano": str(start + 10**8), "attributes": common + [
             attr("langfuse.observation.type", "generation"), attr("langfuse.observation.output", marker)]},
    ]
    payload = {"resourceSpans": [{"resource": {"attributes": [attr("service.name", "chainsaw")]},
                                  "scopeSpans": [{"scope": {"name": "chainsaw"}, "spans": spans}]}]}
    status, _ = call("/api/public/otel/v1/traces", payload)
    assert 200 <= status < 300, f"ingest returned {status}"


def rows(trace):
    status, body = call(f"/api/public/v2/observations?traceId={trace}&fromStartTime=2020-01-01T00:00:00Z&fields=core,basic,io")
    assert status == 200, f"read returned {status}"
    return body.get("data", [])


def wait(trace, want_present, seconds):
    deadline = time.time() + float(seconds)
    while time.time() < deadline:
        found = rows(trace)
        if bool(found) == want_present and (not want_present or len(found) >= 2):
            return found
        time.sleep(2)
    raise SystemExit(f"trace {trace} present={bool(rows(trace))}, wanted present={want_present}")


def check_auth():
    bad = "Basic " + base64.b64encode(b"pk-lf-wrong:sk-lf-wrong").decode()
    for auth in (None, bad):
        status, _ = call("/api/public/otel/v1/traces", {"resourceSpans": []}, auth=auth)
        assert status == 401, f"unauthenticated ingest returned {status}"
        status, _ = call("/api/public/v2/observations", auth=auth)
        assert status == 401, f"unauthenticated read returned {status}"
    status, _ = call("/api/auth/signup", {"name": "x", "email": "chainsaw@example.invalid", "password": "Chainsaw-pass-1"}, auth=None)
    assert status >= 400, f"signup returned {status} with AUTH_DISABLE_SIGNUP"


def main():
    command, *args = sys.argv[1:]
    if command == "send":
        send(*args)
    elif command == "present":
        trace, marker, seconds = args
        found = wait(trace, True, seconds)
        by_name = {row["name"]: row for row in found}
        assert by_name["child"]["parentObservationId"] == by_name["root"]["id"], "parent link lost"
        assert marker in json.dumps(found), "body marker missing"
    elif command == "gone":
        wait(args[0], False, args[1])
    elif command == "auth":
        check_auth()


main()
