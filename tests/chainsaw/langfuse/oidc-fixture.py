"""Synthetic OIDC issuer standing in for Cloudflare Access. /mode selects how the next ID token is wrong."""
import base64, hashlib, json, os, secrets, time, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ISSUER = os.environ["ISSUER"]
CLIENT_ID = os.environ["CLIENT_ID"]
CLIENT_SECRET = os.environ["CLIENT_SECRET"]
OWNER_EMAIL = os.environ["OWNER_EMAIL"]
state = {"mode": "ok"}
codes = {}


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def new_key():
    """Returns (n, e, d) from a 2048 bit key built with the stdlib only, so no key material is stored."""
    def prime():
        while True:
            c = secrets.randbits(1024) | (1 << 1023) | 1
            if all(pow(a, c - 1, c) == 1 for a in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29)):
                return c
    e = 65537
    while True:
        p, q = prime(), prime()
        phi = (p - 1) * (q - 1)
        try:
            return p * q, e, pow(e, -1, phi)
        except ValueError:
            continue


KEY = new_key()
OTHER_KEY = new_key()
PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


def sign(payload, key):
    n, _, d = key
    size = (n.bit_length() + 7) // 8
    digest = PREFIX + hashlib.sha256(payload).digest()
    block = b"\x00\x01" + b"\xff" * (size - len(digest) - 3) + b"\x00" + digest
    return pow(int.from_bytes(block, "big"), d, n).to_bytes(size, "big")


def id_token(nonce):
    mode, now = state["mode"], int(time.time())
    claims = {"iss": ISSUER, "aud": CLIENT_ID, "sub": "synthetic-owner", "email": OWNER_EMAIL, "name": "synthetic",
              "iat": now, "exp": now + 300}
    if nonce:
        claims["nonce"] = nonce
    if mode == "wrong_aud":
        claims["aud"] = "another-access-application"
    elif mode == "wrong_iss":
        claims["iss"] = ISSUER + "/another"
    elif mode == "expired":
        claims.update(iat=now - 7200, exp=now - 3600)
    elif mode == "unknown_email":
        claims.update(email="intruder@example.invalid", sub="synthetic-intruder")
    alg = "none" if mode == "alg_none" else "RS256"
    head = b64url(json.dumps({"alg": alg, "kid": "k1", "typ": "JWT"}).encode())
    body = b64url(json.dumps(claims).encode())
    signing = f"{head}.{body}".encode()
    if mode == "alg_none":
        return f"{head}.{body}."
    return f"{head}.{body}.{b64url(sign(signing, OTHER_KEY if mode == 'bad_sig' else KEY))}"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, body=None, location=None):
        data = json.dumps(body).encode() if body is not None else b""
        self.send_response(code)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        query = dict(urllib.parse.parse_qsl(url.query))
        if url.path == "/.well-known/openid-configuration":
            self.reply(200, {"issuer": ISSUER, "authorization_endpoint": ISSUER + "/authorize", "token_endpoint": ISSUER + "/token",
                             "jwks_uri": ISSUER + "/jwks", "userinfo_endpoint": ISSUER + "/userinfo",
                             "response_types_supported": ["code"], "subject_types_supported": ["public"],
                             "id_token_signing_alg_values_supported": ["RS256"], "code_challenge_methods_supported": ["S256"]})
        elif url.path == "/jwks":
            n, e, _ = KEY
            self.reply(200, {"keys": [{"kty": "RSA", "kid": "k1", "use": "sig", "alg": "RS256",
                                       "n": b64url(n.to_bytes((n.bit_length() + 7) // 8, "big")), "e": b64url(e.to_bytes(3, "big"))}]})
        elif url.path == "/authorize":
            if query.get("client_id") != CLIENT_ID or query.get("code_challenge_method") != "S256" or not query.get("state"):
                self.reply(400, {"error": "PKCE and state are required"})
                return
            code = secrets.token_urlsafe(16)
            codes[code] = query
            self.reply(302, location=f"{query['redirect_uri']}?{urllib.parse.urlencode({'code': code, 'state': query['state']})}")
        elif url.path == "/healthz":
            self.reply(200, {"ok": True})
        else:
            self.reply(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        form = dict(urllib.parse.parse_qsl(self.rfile.read(length).decode()))
        path = urllib.parse.urlsplit(self.path).path
        if path == "/mode":
            state["mode"] = form["mode"]
            self.reply(200, state)
        elif path == "/token":
            basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
            given = (form.get("client_id"), form.get("client_secret"))
            if self.headers.get("Authorization") != "Basic " + basic and given != (CLIENT_ID, CLIENT_SECRET):
                self.reply(401, {"error": "invalid_client"})
                return
            auth = codes.pop(form.get("code"), None)
            verifier = (form.get("code_verifier") or "").encode()
            if not auth or b64url(hashlib.sha256(verifier).digest()) != auth["code_challenge"]:
                self.reply(400, {"error": "invalid_grant"})
                return
            self.reply(200, {"access_token": secrets.token_urlsafe(16), "token_type": "Bearer", "expires_in": 300,
                             "id_token": id_token(auth.get("nonce"))})
        else:
            self.reply(404)


ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
