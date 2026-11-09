import importlib.util
import json
from pathlib import Path
import http.client
import tempfile
import threading
import unittest

path = Path(__file__).resolve().parents[2] / 'argoproj/stable-diffusion/sd_intake.py'
spec = importlib.util.spec_from_file_location('sd_intake', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class IntakeTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.now = 1000
        self.calls = []
        self.intake = module.Intake(self.root, lambda: self.now, self.send)

    def send(self, path, body):
        self.calls.append((path, body))
        return 200, b'{"images": []}', 'application/json'

    def create(self, key=None):
        return self.intake.create('/sdapi/v1/txt2img', {'prompt': 'test'}, key)

    def test_idempotency_and_capacity(self):
        identity = self.create('same')
        self.assertEqual(identity, self.create('same'))
        for _ in range(7):
            self.create()
        with self.assertRaises(OverflowError):
            self.create()
        self.assertEqual(self.intake.summary()['queued'], 8)
        self.assertNotIn(identity, json.dumps(self.intake.summary()))

    def test_dispatch_and_restart_unknown(self):
        identity = self.create()
        (self.root / 'phase').write_text('image-ready')
        self.assertTrue(self.intake.dispatch_once())
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(module.read_json(self.root / 'jobs' / identity / 'status.json')['state'], 'done')
        other = self.create()
        self.intake.status(other, 'dispatched')
        restarted = module.Intake(self.root, lambda: self.now, self.send)
        self.assertEqual(module.read_json(self.root / 'jobs' / other / 'status.json')['state'], 'unknown')
        self.assertFalse(restarted.dispatch_once())
        self.assertEqual(len(self.calls), 1)

    def test_phase_change_during_dispatch_sends_nothing(self):
        identity = self.create()
        (self.root / 'phase').write_text('image-ready')
        original = self.intake.status

        def status(job, state, **extra):
            value = original(job, state, **extra)
            if state == 'dispatched':
                (self.root / 'phase').write_text('image-draining')
            return value
        self.intake.status = status
        self.assertFalse(self.intake.dispatch_once())
        self.assertEqual(self.calls, [])
        self.assertEqual(module.read_json(self.root / 'jobs' / identity / 'status.json')['state'], 'queued')

    def test_failed_phase_and_retention(self):
        identity = self.create()
        (self.root / 'phase').write_text('image-draining')
        self.assertFalse(self.intake.dispatch_once())
        self.intake.status(identity, 'unknown')
        self.now += 24 * 3600
        self.intake.cleanup()
        self.assertTrue((self.root / 'jobs' / identity).exists())
        self.now += 48 * 3600
        self.intake.cleanup()
        self.assertFalse((self.root / 'jobs' / identity).exists())

    def test_intent_is_idempotent(self):
        self.intake.intent()
        before = (self.root / 'intent.json').read_bytes()
        self.intake.intent()
        self.assertEqual(before, (self.root / 'intent.json').read_bytes())


class HttpSurfaceTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.intake = module.Intake(self.root)
        self.server = module.ThreadingHTTPServer(('127.0.0.1', 0), module.handler(self.intake))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def call(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        self.addCleanup(connection.close)
        connection.request(method, path, body, headers or {})
        response = connection.getresponse()
        return response.status, response.read(), response

    def no_side_effects(self):
        self.assertFalse((self.root / 'intent.json').exists())
        self.assertEqual(list((self.root / 'jobs').iterdir()), [])

    def test_reads_never_create_intent_or_job(self):
        for path, code in (('/', 503), ('/file=app.js', 503), ('/health', 503), ('/sdapi/v1/progress', 503),
                           ('/__sd/status', 200), ('/__sd/health', 200)):
            self.assertEqual(self.call('GET', path)[0], code, path)
        self.no_side_effects()

    def test_waiting_page_and_json_explain_retry(self):
        status, body, response = self.call('GET', '/', headers={'Accept': 'text/html'})
        self.assertEqual(status, 503)
        self.assertIn('画像生成モードへ切替', body.decode())
        self.assertTrue(response.getheader('Retry-After'))
        status, body, response = self.call('GET', '/sdapi/v1/options')
        self.assertIn('GET does not start', json.loads(body)['message'])

    def test_switch_needs_csrf_header(self):
        self.assertEqual(self.call('POST', '/__sd/switch')[0], 403)
        self.no_side_effects()
        self.assertEqual(self.call('POST', '/__sd/switch', headers={'X-SD-Switch': '1'})[0], 202)
        self.assertTrue((self.root / 'intent.json').exists())

    def test_generation_needs_json_and_is_durable(self):
        path = '/sdapi/v1/txt2img'
        self.assertEqual(self.call('POST', path, b'{}', {'Content-Type': 'text/plain'})[0], 415)
        json_type = {'Content-Type': 'application/json'}
        self.assertEqual(self.call('POST', path, b'[]', json_type)[0], 400)
        self.assertEqual(self.call('POST', path, b'{bad', json_type)[0], 400)
        self.no_side_effects()
        status, body, response = self.call('POST', path, b'{"prompt":"x"}', json_type)
        self.assertEqual(status, 202)
        self.assertTrue(response.getheader('Retry-After'))
        accepted = json.loads(body)
        self.assertTrue((self.root / 'jobs' / accepted['job_id'] / 'request.json').exists())
        status, body, _ = self.call('GET', accepted['status_url'])
        self.assertEqual((status, json.loads(body)['state'], json.loads(body)['position']), (200, 'queued', 1))
        self.assertEqual(self.call('GET', accepted['result_url'])[0], 409)
        self.assertEqual(self.call('GET', '/__sd/jobs/' + 'f' * 32)[0], 404)

    def test_body_limit(self):
        headers = {'Content-Type': 'application/json', 'Content-Length': str(module.LIMIT + 1)}
        self.assertEqual(self.call('POST', '/sdapi/v1/txt2img', b'', headers)[0], 413)

    def test_invalid_idempotency_key(self):
        headers = {'Content-Type': 'application/json', 'Idempotency-Key': 'x' * 129}
        self.assertEqual(self.call('POST', '/sdapi/v1/txt2img', b'{}', headers)[0], 400)


if __name__ == '__main__':
    unittest.main()
