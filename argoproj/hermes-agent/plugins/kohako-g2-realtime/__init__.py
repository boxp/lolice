"""Kohako G2 realtime mint plugin (BOXP-387).

Adds ``POST /v1/kohako/realtime/client-secrets`` to the Hermes API server. The Kohako G2 BFF calls it
(Bearer API_SERVER_KEY) to obtain a short-lived OpenAI Realtime client secret; the BFF then relays the
G2 audio to api.openai.com itself. The secret is minted through Hermes core's
``credentialed_provider_request("openai-codex", ...)``: the openai-codex sign-in stays inside Hermes, this
plugin never sees a token, never reads auth files and never refreshes anything. Only the subscription
sign-in is used: there is no API-key fallback (a missing sign-in is a 503).

The response carries only ``value`` (the ephemeral client secret), ``expiresAt`` (ms epoch) and ``model``.
Logs carry status codes and timings only: never the secret, the instructions or the upstream body.
"""
import asyncio
import logging
import re
import threading
import time

_log = logging.getLogger(__name__)
_lock = threading.Lock()
_calls = []

ROUTE = "/v1/kohako/realtime/client-secrets"
MODELS = ("gpt-realtime-2.1", "gpt-realtime", "gpt-realtime-mini")
VOICES = ("marin", "cedar", "alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse")
MAX_INSTRUCTIONS = 2000
PER_HOUR = 12
RATE = 24000
_SESSION_ID = re.compile(r"^[a-f0-9]{16}$")


def register(ctx):
    ctx.register_platform_handler("api_server", _wire)


def _wire(native, adapter):
    """Factory invoked by the API server adapter with its aiohttp ``web.Application``."""
    from aiohttp import web

    async def handler(request):
        status, body = await _handle(request, adapter)
        return web.json_response(body, status=status)

    native.router.add_post(ROUTE, handler)
    _log.info("kohako-g2-realtime: route registered")


async def _handle(request, adapter, requester=None, now=None):
    """Authenticate with the API server's own bearer check, validate, then mint off the event loop."""
    now = now or time.time
    check = getattr(adapter, "_check_auth", None)
    if check is None:
        return 503, {"error": "auth_unavailable"}
    if check(request) is not None:
        return 401, {"error": "unauthorized"}
    try:
        body = await request.json()
    except Exception:
        return 400, {"error": "invalid"}
    session = validate(body)
    if session is None:
        return 400, {"error": "invalid"}
    with _lock:
        _calls[:] = [at for at in _calls if now() - at < 3600]
        if len(_calls) >= PER_HOUR:
            return 429, {"error": "rate_limited"}
        _calls.append(now())
    started = time.monotonic()
    status, result = await asyncio.to_thread(mint, session, requester)
    _log.info("kohako-g2-realtime: mint status=%s ms=%d", status, int((time.monotonic() - started) * 1000))
    return status, result


def validate(body):
    """Return the session payload for OpenAI, or None when the BFF request is out of contract."""
    if not isinstance(body, dict) or set(body) != {"sessionId", "instructions", "voice", "model"}:
        return None
    session_id, instructions, voice, model = body["sessionId"], body["instructions"], body["voice"], body["model"]
    if not (isinstance(session_id, str) and _SESSION_ID.match(session_id)):
        return None
    if not (isinstance(instructions, str) and 0 < len(instructions) <= MAX_INSTRUCTIONS):
        return None
    if voice not in VOICES or model not in MODELS:
        return None
    return {
        "type": "realtime",
        "model": model,
        "instructions": instructions,
        "audio": {
            "input": {
                "format": {"type": "audio/pcm", "rate": RATE},
                "noise_reduction": {"type": "near_field"},
                "turn_detection": {"type": "server_vad", "silence_duration_ms": 600, "create_response": True, "interrupt_response": True},
                "transcription": {"model": "gpt-4o-mini-transcribe", "language": "ja"},
            },
            "output": {"format": {"type": "audio/pcm", "rate": RATE}, "voice": voice},
        },
    }


def mint(session, requester=None):
    """Mint via Hermes core (sync httpx inside core; run in a worker thread). Returns (status, body)."""
    if requester is None:
        from hermes_cli.plugin_provider_requests import credentialed_provider_request
        requester = credentialed_provider_request
    try:
        response = requester(
            "openai-codex", "POST", "https://api.openai.com/v1/realtime/client_secrets",
            json={"session": session}, headers={"Content-Type": "application/json"}, timeout=20.0,
        )
    except PermissionError:
        _log.warning("kohako-g2-realtime: refused by Hermes (plugin not allowed to use the sign-in)")
        return 503, {"error": "refused"}
    except Exception as exc:
        name = type(exc).__name__
        if name == "ProviderNotSignedIn":
            _log.warning("kohako-g2-realtime: openai-codex sign-in missing")
            return 503, {"error": "not_signed_in"}
        _log.warning("kohako-g2-realtime: upstream request failed (%s)", name)
        return 502, {"error": "upstream"}
    if response.status != 200:
        _log.warning("kohako-g2-realtime: upstream status=%s", response.status)
        return 502, {"error": "upstream", "status": response.status}
    try:
        data = response.json()
        value = data.get("value") or (data.get("client_secret") or {}).get("value")
        expires = data.get("expires_at") or (data.get("client_secret") or {}).get("expires_at")
        model = (data.get("session") or {}).get("model") or session["model"]
    except Exception:
        return 502, {"error": "upstream_schema"}
    if not (isinstance(value, str) and value and isinstance(expires, (int, float)) and expires > 0):
        return 502, {"error": "upstream_schema"}
    return 200, {"value": value, "expiresAt": int(expires * 1000), "model": str(model)}
