"""OTLP sink の受信記録 (/tmp/requests.jsonl) を判定する。kubectl exec -i ... python - <mode> [args]。

判定結果は exit code で返す (0: 成立 / 非 0: 不成立)。
"""
import json
import os
import sys

REQUESTS = "/tmp/requests.jsonl"
SECRET_FRAGMENTS = (
    "plainwordvalue", "plainkubevalue", "plainenvvalue", "plainnestedvalue",
    "plainflagvalue", "plainentryvalue", "plainarrayvalue", "plainresumevalue",
    "plainxmlvalue", "plainprosevalue", "plainformatvalue", "plainnamedvalue",
    "plaintablevalue",
)
# Owner の契約: 無害な tool 入出力、その後の応答、次ターン、委譲の系譜は読めること。
POSITIVE_MARKERS = (
    "harmlessrequestmarker", "harmlesstooloutputmarker", "harmlessreplymarker",
    "harmlessnextturnmarker", "harmlessnextreplymarker", "ls -la /tmp", "/tmp/notes.txt",
    "a_tool_added_later", "harmlessfilemarker", "harmlesschildmarker",
    "harmlesschildtoolmarker", "harmlesschildreplymarker", "harmlesssummarymarker",
    "harmlessparentreplymarker", "keptdistantmarker", "nameonlyreplymarker",
    "nameonlynextmarker", "td12grepfilterreplymarker", "td12grepfilternextmarker",
    "td12prosewritereplymarker", "td12prosewritenextmarker", "td12oneenvreplymarker",
    "td12oneenvnextmarker",
)


def rows():
    with open(REQUESTS) as requests:
        return [json.loads(line) for line in requests]


def body():
    return "".join(row["body"] for row in rows())


def no_requests():
    assert not os.path.exists(REQUESTS) or os.path.getsize(REQUESTS) == 0


def authenticated_marker_received(authorization):
    if not os.path.exists(REQUESTS):
        sys.exit(1)
    sys.exit(not any(r["authorization"] == authorization and "cleanbodymarker" in r["body"] for r in rows()))


def secrets_withheld(public_key, secret_key):
    received = rows()
    assert any("cleanbodymarker" in r["body"] for r in received)
    forbidden = (*SECRET_FRAGMENTS, public_key, secret_key)
    assert all(all(value not in r["body"] for value in forbidden) for r in received)


def positive_bodies_received():
    text = body()
    missing = [marker for marker in POSITIVE_MARKERS if marker not in text]
    print(missing)
    sys.exit(bool(missing))


def body_policy():
    text = body()
    assert text.count('"stringValue": "sent"') >= 10, "no body_policy=sent"
    assert "withheld_after_known_format_in_tool_output" in text
    assert "withheld_known_source" in text


def child_trace_received():
    assert any("childbodymarker" in r["body"] for r in rows())


def delegated_goal_received():
    assert any("delegatedgoalmarker" in r["body"] for r in rows())


{
    "no-requests": no_requests,
    "authenticated-marker": authenticated_marker_received,
    "secrets-withheld": secrets_withheld,
    "positive-bodies": positive_bodies_received,
    "body-policy": body_policy,
    "child-trace": child_trace_received,
    "delegated-goal": delegated_goal_received,
}[sys.argv[1]](*sys.argv[2:])
