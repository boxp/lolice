import os
import tempfile
import unittest
from pathlib import Path
import importlib.util
spec = importlib.util.spec_from_file_location("vault_plugin", Path(__file__).with_name("__init__.py"))
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)
read_image, MAX_BYTES = plugin.read_image, plugin.MAX_BYTES


class VaultImages(unittest.TestCase):
    def test_regular_image_and_boundary_rejections(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as outside:
            root = Path(d)
            (root / 'attachments').mkdir()
            (root / 'attachments/案 1.png').write_bytes(b'\x89PNG\r\n\x1a\nfixture')
            self.assertEqual(read_image(d, 'attachments/案 1.png')['mime'], 'image/png')
            (Path(outside) / 'secret.png').write_bytes(b'\x89PNG\r\n\x1a\nprivate')
            (root / 'escape').symlink_to(outside)
            (root / 'link.png').symlink_to(Path(outside) / 'secret.png')
            (root / '.secret.png').write_bytes(b'\x89PNG\r\n\x1a\nprivate')
            (root / 'fake.png').write_bytes(b'<html>secret</html>')
            (root / 'fake.jpg').write_bytes(b'\x89PNG\r\n\x1a\nfixture')
            (root / 'large.png').write_bytes(b'\x89PNG\r\n\x1a\n' + b'X' * MAX_BYTES)
            os.mkfifo(root / 'pipe.png')
            for path in ('../secret.png', '/secret.png', 'attachments//案 1.png', 'attachments/../fake.png', '.secret.png', 'escape/secret.png', 'link.png', 'fake.png', 'fake.jpg', 'large.png', 'pipe.png', 'x.svg', 'a\\b.png', 'a\x00.png'):
                with self.subTest(path=path), self.assertRaises((ValueError, OSError)):
                    read_image(d, path)
            with self.assertRaises((ValueError, OSError)):
                read_image('', 'attachments/案 1.png')

    def test_signature_formats(self):
        with tempfile.TemporaryDirectory() as d:
            for name, data, mime in [('a.jpg', b'\xff\xd8\xffx', 'image/jpeg'), ('a.gif', b'GIF89ax', 'image/gif'), ('a.webp', b'RIFF0000WEBPx', 'image/webp')]:
                Path(d, name).write_bytes(data)
                self.assertEqual(read_image(d, name)['mime'], mime)


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_auth_and_image(self):
        from aiohttp import web
        from types import SimpleNamespace
        from unittest.mock import patch
        request = SimpleNamespace(query={"path": "a.png"})
        missing = await plugin.handle(object(), request)
        self.assertEqual(missing.status, 503)
        denied = web.json_response({"error": "unauthorized"}, status=401)
        adapter = SimpleNamespace(_check_auth=lambda request: denied, _api_key="key")
        self.assertEqual((await plugin.handle(adapter, request)).status, 401)
        adapter._check_auth = lambda request: None
        adapter._api_key = ""
        self.assertEqual((await plugin.handle(adapter, request)).status, 403)
        adapter._api_key = "key"
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"OBSIDIAN_VAULT_PATH": root}):
            response = await plugin.handle(adapter, request)
            self.assertEqual(response.status, 404)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            Path(root, "a.png").write_bytes(b"\x89PNG\r\n\x1a\nfixture")
            response = await plugin.handle(adapter, request)
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_route_registration(self):
        from aiohttp import web
        app = web.Application()
        plugin._wire(app, object())
        self.assertTrue(any(r.resource.canonical == plugin.ROUTE for r in app.router.routes()))
        plugin._wire(app, object())  # a second registration (image that already has the route) is a no-op
        self.assertEqual(sum(1 for r in app.router.routes() if r.method == "GET" and r.resource.canonical == plugin.ROUTE), 1)


if __name__ == "__main__":
    unittest.main()
