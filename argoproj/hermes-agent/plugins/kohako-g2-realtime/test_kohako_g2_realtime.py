import asyncio
import importlib.util
import json
import logging
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("kohako_g2_realtime", Path(__file__).with_name("__init__.py"))
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)

GOOD = {"sessionId": "0123456789abcdef", "instructions": "persona", "voice": "marin", "model": "gpt-realtime-2.1"}


class Response:
    def __init__(self, status, body):
        self.status, self._body = status, body

    def json(self):
        return json.loads(self._body)


class Request:
    def __init__(self, body, token="good"):
        self._body, self.headers = body, {"Authorization": f"Bearer {token}"}

    async def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class Adapter:
    def _check_auth(self, request):
        return None if request.headers.get("Authorization") == "Bearer good" else "401"


class Ctx:
    def __init__(self):
        self.handlers = {}

    def register_platform_handler(self, platform, factory):
        self.handlers[platform] = factory


class Tests(unittest.TestCase):
    def setUp(self):
        plugin._calls.clear()
        self.requests = []

    def requester(self, provider, method, url, *, json=None, headers=None, timeout=None):
        self.requests.append((provider, method, url, json, headers, timeout))
        return Response(200, '{"value": "ek_secret", "expires_at": 1700000000, "session": {"model": "gpt-realtime-2.1"}}')

    def handle(self, request, requester=None, now=None):
        return asyncio.run(plugin._handle(request, Adapter(), requester or self.requester, now))

    def test_registers_api_server_route_only(self):
        ctx = Ctx()
        plugin.register(ctx)
        self.assertEqual(list(ctx.handlers), ["api_server"])

    def test_mints_through_the_codex_sign_in_with_the_fixed_session_shape(self):
        status, body = self.handle(Request(GOOD))
        self.assertEqual((status, body), (200, {"value": "ek_secret", "expiresAt": 1700000000000, "model": "gpt-realtime-2.1"}))
        provider, method, url, payload, headers, timeout = self.requests[0]
        self.assertEqual((provider, method, url), ("openai-codex", "POST", "https://api.openai.com/v1/realtime/client_secrets"))
        session = payload["session"]
        self.assertEqual(session["instructions"], "persona")
        self.assertEqual(session["audio"]["output"]["voice"], "marin")
        self.assertEqual(session["audio"]["input"]["format"], {"type": "audio/pcm", "rate": 24000})
        self.assertTrue(session["audio"]["input"]["turn_detection"]["interrupt_response"])
        self.assertEqual(session["audio"]["input"]["transcription"]["language"], "ja")
        self.assertLessEqual(timeout, 30)

    def test_rejects_bad_auth_and_bad_bodies_without_minting(self):
        self.assertEqual(self.handle(Request(GOOD, token="bad"))[0], 401)
        self.assertEqual(self.handle(Request(ValueError("x")))[0], 400)
        for bad in [
            {**GOOD, "voice": "robot"},
            {**GOOD, "model": "gpt-4o"},
            {**GOOD, "sessionId": "x"},
            {**GOOD, "instructions": "a" * 2001},
            {**GOOD, "instructions": ""},
            {**GOOD, "extra": 1},
            {"sessionId": GOOD["sessionId"]},
            [],
            "text",
        ]:
            self.assertEqual(self.handle(Request(bad))[0], 400, bad)
        self.assertEqual(self.requests, [])

    def test_rate_limits_per_hour(self):
        clock = [1000.0]
        for _ in range(plugin.PER_HOUR):
            self.assertEqual(self.handle(Request(GOOD), now=lambda: clock[0])[0], 200)
        self.assertEqual(self.handle(Request(GOOD), now=lambda: clock[0]), (429, {"error": "rate_limited"}))
        clock[0] += 3601
        self.assertEqual(self.handle(Request(GOOD), now=lambda: clock[0])[0], 200)

    def test_maps_upstream_failures_without_leaking_bodies(self):
        class NotSignedIn(Exception):
            pass

        NotSignedIn.__name__ = "ProviderNotSignedIn"

        def failing(kind):
            def requester(*args, **kwargs):
                if kind == "perm":
                    raise PermissionError("Refusing to send the sign-in")
                if kind == "signin":
                    raise NotSignedIn("not signed in")
                if kind == "net":
                    raise OSError("boom")
                if kind == "status":
                    return Response(401, '{"error": {"message": "token ek_leaky invalid"}}')
                return Response(200, '{"unexpected": true}')
            return requester

        with self.assertLogs(plugin._log, level="WARNING") as logs:
            self.assertEqual(self.handle(Request(GOOD), failing("perm")), (503, {"error": "refused"}))
            self.assertEqual(self.handle(Request(GOOD), failing("signin")), (503, {"error": "not_signed_in"}))
            self.assertEqual(self.handle(Request(GOOD), failing("net")), (502, {"error": "upstream"}))
            self.assertEqual(self.handle(Request(GOOD), failing("status")), (502, {"error": "upstream", "status": 401}))
            self.assertEqual(self.handle(Request(GOOD), failing("schema")), (502, {"error": "upstream_schema"}))
        joined = "\n".join(logs.output)
        self.assertNotIn("ek_leaky", joined)
        self.assertNotIn("persona", joined)

    def test_success_log_has_no_secret(self):
        with self.assertLogs(plugin._log, level="INFO") as logs:
            self.handle(Request(GOOD))
        self.assertNotIn("ek_secret", "\n".join(logs.output))

    def test_fails_closed_without_adapter_auth(self):
        status, body = asyncio.run(plugin._handle(Request(GOOD), object(), self.requester))
        self.assertEqual((status, body), (503, {"error": "auth_unavailable"}))
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
