"""Kohako vault image plugin (BOXP-542, moved out of the Hermes source by BOXP-556).

Adds ``GET /v1/kohako/vault-image?path=<vault-relative path>`` to the Hermes API server through the
official ``register_platform_handler("api_server", ...)`` extension point (the same one as
kohako-g2-realtime). Read-only image transport for Kohako Home: the root is always
``OBSIDIAN_VAULT_PATH``, never a request value; only the API server key holder may read.
"""
import base64
import logging
import os
import stat
from pathlib import PurePosixPath

_log = logging.getLogger(__name__)
MAX_BYTES = 5 * 1024 * 1024
MIMES = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif', '.webp': 'image/webp'}


def read_image(root, relative):
    if not root or not isinstance(relative, str) or len(relative) > 1500 or '\\' in relative or any(ord(c) < 32 for c in relative):
        raise ValueError('invalid_path')
    parts = relative.split('/')
    if not parts or any(not p or p.startswith('.') for p in parts) or PurePosixPath(relative).is_absolute():
        raise ValueError('invalid_path')
    mime = MIMES.get(PurePosixPath(relative).suffix.lower())
    if not mime:
        raise ValueError('unsupported_type')
    # Directory descriptors pin each component; concurrent symlink substitution
    # cannot redirect this read outside the configured vault.
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        image_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(image_fd, 'rb') as f:
            meta = os.fstat(f.fileno())
            if not stat.S_ISREG(meta.st_mode):
                raise ValueError('invalid_file')
            if meta.st_size > MAX_BYTES:
                raise ValueError('too_large')
            data = f.read(MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_BYTES:
        raise ValueError('too_large')
    detected = ('image/png' if data.startswith(b'\x89PNG\r\n\x1a\n') else
                'image/jpeg' if data.startswith(b'\xff\xd8\xff') else
                'image/gif' if data[:6] in (b'GIF87a', b'GIF89a') else
                'image/webp' if data[:4] == b'RIFF' and data[8:12] == b'WEBP' else None)
    if detected != mime:
        raise ValueError('unsupported_type')
    return {'mime': mime, 'data': base64.b64encode(data).decode('ascii')}


async def handle(adapter, request):
    import asyncio
    from aiohttp import web
    # Runs/session delegated credentials must not become vault read credentials.
    check = getattr(adapter, "_check_auth", None)
    if not callable(check):
        return web.json_response({"error": "auth_unavailable"}, status=503)
    error = check(request)
    if error is not None:
        return error
    if not getattr(adapter, "_api_key", None):
        return web.json_response({'error': 'forbidden'}, status=403)
    try:
        result = await asyncio.to_thread(read_image, os.getenv('OBSIDIAN_VAULT_PATH'), request.query.get('path'))
        return web.json_response(result, headers={'Cache-Control': 'no-store'})
    except (ValueError, OSError):
        return web.json_response({'error': 'image_unavailable'}, status=404, headers={'Cache-Control': 'no-store'})


ROUTE = "/v1/kohako/vault-image"


def register(ctx):
    ctx.register_platform_handler("api_server", _wire)


def _wire(native, adapter):
    """Factory invoked by the API server adapter with its aiohttp ``web.Application``."""
    # An image that still carries the former build-time route (BOXP-542) keeps serving it: adding the
    # same method and path twice is an aiohttp error, and the two handlers are the same code.
    for route in native.router.routes():
        if route.method == "GET" and getattr(route.resource, "canonical", None) == ROUTE:
            _log.info("kohako-vault-image: route already present; plugin route not added")
            return

    async def handler(request):
        return await handle(adapter, request)

    native.router.add_get(ROUTE, handler)
    _log.info("kohako-vault-image: route registered")
