"""Plays the browser against Langfuse and the fixture issuer. Prints one JSON line with what happened."""
import http.client, json, os, sys, urllib.parse

WEB = "http://langfuse-web.langfuse.svc.cluster.local:3000"
GATEWAY = "http://langfuse-sign-in-gateway.langfuse.svc.cluster.local:3001"
ISSUER = os.environ["ISSUER"]
jar = {}


def call(method, url, body=None, headers=None):
    part = urllib.parse.urlsplit(url)
    conn = http.client.HTTPConnection(part.hostname, part.port, timeout=30)
    head = dict(headers or {})
    if jar:
        head["Cookie"] = "; ".join(f"{k}={v}" for k, v in jar.items())
    if body is not None:
        body = urllib.parse.urlencode(body)
        head["Content-Type"] = "application/x-www-form-urlencoded"
    conn.request(method, part.path + ("?" + part.query if part.query else ""), body, head)
    res = conn.getresponse()
    data = res.read()
    for name, value in res.getheaders():
        if name.lower() == "set-cookie":
            key, _, val = value.split(";")[0].partition("=")
            jar[key] = val
    conn.close()
    return res.status, res.getheader("Location") or "", data


def session(headers=None):
    return (json.loads(call("GET", WEB + "/api/auth/session", headers=headers)[2]).get("user") or {}).get("email")


def finish(location):
    """Follows the issuer and the callback the way a browser would, stopping at the first page outside them."""
    if not location.startswith(ISSUER + "/"):
        return location
    status, callback, _ = call("GET", location)
    if status != 302:
        return f"issuer answered {status}"
    return urllib.parse.urljoin(WEB, call("GET", callback)[1])


def csrf():
    return json.loads(call("GET", WEB + "/api/auth/csrf")[2])["csrfToken"]


command = sys.argv[1]
result = {}
if command == "mode":
    call("POST", ISSUER + "/mode", {"mode": sys.argv[2]})
elif command == "direct":
    location = call("POST", WEB + "/api/auth/signin/custom", {"csrfToken": csrf(), "callbackUrl": WEB + "/"})[1]
    result = {"final": finish(location), "email": session()}
elif command == "gateway":
    query = urllib.parse.urlencode({"targetPath": sys.argv[2]}) if len(sys.argv) > 2 else ""
    status, location, _ = call("GET", GATEWAY + "/auth/sign-in?" + query)
    result = {"status": status, "starts_at_issuer": location.startswith(ISSUER + "/"), "final": finish(location), "email": session()}
elif command == "gateway-status":
    status, location, data = call(sys.argv[2], GATEWAY + sys.argv[3])
    result = {"status": status, "location": location, "html": b"<html" in data[:2000].lower()}
elif command == "spoof":
    forged = {"Cf-Access-Jwt-Assertion": "forged.jwt.value", "Cf-Access-Authenticated-User-Email": os.environ["OWNER_EMAIL"],
              "X-Forwarded-Email": os.environ["OWNER_EMAIL"], "X-Forwarded-User": os.environ["OWNER_EMAIL"]}
    result = {"email": session(forged)}
elif command == "password":
    status, location, _ = call("POST", WEB + "/api/auth/callback/credentials",
                               {"csrfToken": csrf(), "email": os.environ["OWNER_EMAIL"], "password": os.environ["OWNER_PASSWORD"],
                                "callbackUrl": WEB + "/", "json": "true"})
    result = {"status": status, "email": session()}
print(json.dumps(result))
