"""ComfyUI waiting page and switch request (BOXP-498)."""
import http.client
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

path = Path(__file__).resolve().parents[2] / 'argoproj/stable-diffusion/comfy_intake.py'
spec = importlib.util.spec_from_file_location('comfy_intake', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ComfyIntakeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.intake = module.Intake(self.root, lambda: 1000.0)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), module.handler(self.intake))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def phase(self, value):
        (self.root / 'phase').write_text(value + '\n')

    def call(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        self.addCleanup(connection.close)
        connection.request(method, path, body, headers or {})
        response = connection.getresponse()
        return response.status, response.read(), response

    def requested(self):
        return (self.root / 'intent.json').exists()

    def test_reads_never_request_a_switch(self):
        self.phase('llm-open')
        for path, code in (('/', 503), ('/ws', 503), ('/api/queue', 503), ('/system_stats', 503),
                           ('/__comfy/status', 200), ('/__comfy/health', 200)):
            self.assertEqual(self.call('GET', path)[0], code, path)
        self.assertFalse(self.requested())

    def test_switch_needs_the_custom_header(self):
        self.phase('llm-open')
        self.assertEqual(self.call('POST', '/__comfy/switch')[0], 403)
        self.assertFalse(self.requested())
        status, body, _ = self.call('POST', '/__comfy/switch', headers={'X-Comfy-Switch': '1'})
        self.assertEqual((status, json.loads(body)['requested']), (202, True))
        self.assertTrue(self.requested())

    def test_repeated_switch_keeps_one_request(self):
        self.phase('llm-open')
        headers = {'X-Comfy-Switch': '1'}
        self.call('POST', '/__comfy/switch', headers=headers)
        first = (self.root / 'intent.json').read_text()
        self.assertEqual(self.call('POST', '/__comfy/switch', headers=headers)[0], 202)
        self.assertEqual((self.root / 'intent.json').read_text(), first)

    def test_switch_is_refused_outside_llm_open(self):
        for phase in ('busy', 'draining', 'image-draining', 'stopping-sd', 'verifying', 'failed', 'nonsense'):
            self.phase(phase)
            self.assertEqual(self.call('POST', '/__comfy/switch', headers={'X-Comfy-Switch': '1'})[0], 409, phase)
            self.assertFalse(self.requested(), phase)
        (self.root / 'phase').unlink()
        self.assertEqual(self.call('POST', '/__comfy/switch', headers={'X-Comfy-Switch': '1'})[0], 409)
        self.assertFalse(self.requested())

    def test_other_writes_are_refused_and_stored_nowhere(self):
        self.phase('image-draining')
        before = sorted(item.name for item in self.root.iterdir())
        for method, path in (('POST', '/prompt'), ('POST', '/api/prompt'), ('POST', '/upload/image'),
                             ('PUT', '/userdata/x.json'), ('DELETE', '/api/userdata/x.json')):
            status, body, response = self.call(method, path, b'{"prompt": {}}', {'Content-Type': 'application/json'})
            self.assertEqual(status, 503, path)
            self.assertEqual(json.loads(body)['phase'], 'image-draining')
            self.assertTrue(response.getheader('Retry-After'))
        self.assertEqual(sorted(item.name for item in self.root.iterdir()), before)

    def test_page_offers_the_button_only_when_a_switch_can_start(self):
        self.phase('llm-open')
        status, body, response = self.call('GET', '/')
        self.assertEqual(status, 503)
        self.assertIn('動画生成モードへ切替', body.decode())
        self.assertTrue(response.getheader('Retry-After'))
        self.call('POST', '/__comfy/switch', headers={'X-Comfy-Switch': '1'})
        self.assertNotIn('<button', self.call('GET', '/')[1].decode())
        for phase in ('busy', 'starting-sd', 'failed'):
            self.phase(phase)
            self.assertNotIn('<button', self.call('GET', '/')[1].decode(), phase)
        self.assertIn('手動復旧', self.call('GET', '/')[1].decode())

    def test_status_reports_the_window(self):
        self.phase('image-ready')
        self.assertEqual(json.loads(self.call('GET', '/__comfy/status')[1]),
                         {'phase': 'image-ready', 'requested': False, 'window_active': True})
        self.phase('bogus')
        self.assertEqual(json.loads(self.call('GET', '/__comfy/status')[1])['phase'], 'unavailable')


if __name__ == '__main__':
    unittest.main()
