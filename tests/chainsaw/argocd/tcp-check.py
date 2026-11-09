"""Pod 内で DNS と Service port の TCP 到達性を判定する (kubectl exec -i ... python - ...)。

usage: tcp-check.py <host> <port> <reachable|blocked|resolve>
exit 0: 期待どおり / 3: DNS 失敗 / 4: 期待に反して到達状況が逆 / 5: timeout 以外の TCP 失敗
"""
import socket
import sys

host, port, expected = sys.argv[1], int(sys.argv[2]), sys.argv[3]

try:
    socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
except Exception as exc:
    print(f"DNS failure: {exc}", file=sys.stderr)
    sys.exit(3)
if expected == "resolve":
    sys.exit(0)
try:
    conn = socket.create_connection((host, port), timeout=2)
    conn.close()
except TimeoutError:
    sys.exit(0 if expected == "blocked" else 4)
except OSError as exc:
    print(f"TCP failed without policy-drop timeout: {exc}", file=sys.stderr)
    sys.exit(5)
else:
    sys.exit(4 if expected == "blocked" else 0)
