"""Exercise the ComfyUI gateway Lua and routes on the deployed Envoy digest (BOXP-498)."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import textwrap
import time
import unittest
import uuid

spec = importlib.util.spec_from_file_location('sd_gateway_envoy', Path(__file__).with_name('test_gateway_envoy.py'))
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
ROOT, docker = base.ROOT, base.docker
SERVER = r'''
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import json, threading
from pathlib import Path

def make(name):
    class Handler(BaseHTTPRequestHandler):
        def reply(self):
            if self.path == '/ws' and name == 'backend':
                self.send_response(101); self.send_header('Upgrade', 'websocket')
                self.send_header('Connection', 'Upgrade'); self.end_headers(); return
            if self.path == '/free' and name == 'backend':
                length = int(self.headers.get('Content-Length', 0))
                with open('/tmp/free-calls', 'a') as f:
                    f.write(json.dumps({'headers': dict(self.headers), 'body': self.rfile.read(length).decode()}) + '\n')
                status = int(Path('/tmp/free-status').read_text()) if Path('/tmp/free-status').exists() else 200
                if status == 0:
                    self.close_connection = True; return
                self.send_response(status); self.send_header('Content-Length', '0'); self.end_headers(); return
            self.send_response(200); self.send_header('Content-Length', str(len(name))); self.end_headers()
            self.wfile.write(name.encode())
        do_GET = do_POST = reply
        def log_message(self, *args): pass
    return Handler
threading.Thread(target=ThreadingHTTPServer(('127.0.0.1', 8189), make('backend')).serve_forever, daemon=True).start()
ThreadingHTTPServer(('127.0.0.1', 8090), make('intake')).serve_forever()
'''
CLIENT = base.CLIENT.replace('7860', '8188')
INTAKE, BACKEND = [200, 'intake'], [200, 'backend']


class ComfyGatewayRoutingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ids = []
        cls.temp = tempfile.TemporaryDirectory()
        directory = Path(cls.temp.name)
        source = (ROOT / 'argoproj/stable-diffusion/comfyui-gateway-envoy-config.yaml').read_text()
        config = textwrap.dedent(source.split('  envoy.yaml: |\n', 1)[1])
        config = (config.replace('type: STRICT_DNS', 'type: STATIC')
                  .replace('address: comfyui-backend.stable-diffusion.svc.cluster.local', 'address: 127.0.0.1'))
        before, after = config.rsplit('port_value: 8188', 1)
        (directory / 'envoy.yaml').write_text(before + 'port_value: 8189' + after)
        (directory / 'server.py').write_text(SERVER)
        (directory / 'client.py').write_text(CLIENT)
        try:
            name = 'boxp498-' + uuid.uuid4().hex[:12]
            cls.backend = docker('create', '--name', name, '--network', 'none', base.PYTHON, 'python3', '/server.py')
            cls.ids.append(cls.backend)
            docker('cp', str(directory / 'server.py'), cls.backend + ':/server.py')
            docker('cp', str(directory / 'client.py'), cls.backend + ':/client.py')
            docker('start', cls.backend)
            cls.envoy = docker('create', '--network', 'container:' + cls.backend, '--entrypoint', '/usr/local/bin/envoy',
                               base.ENVOY, '-c', '/envoy.yaml', '--concurrency', '1', '--log-level', 'warn')
            cls.ids.append(cls.envoy)
            docker('cp', str(directory / 'envoy.yaml'), cls.envoy + ':/envoy.yaml')
            shared = directory / 'comfy-switch'
            shared.mkdir()
            docker('cp', str(shared), cls.envoy + ':/comfy-switch')
            docker('start', cls.envoy)
            for _ in range(60):
                try:
                    if cls.request('GET', '/__comfy/health')[0] == 200:
                        break
                except subprocess.CalledProcessError:
                    pass
                time.sleep(.1)
            else:
                raise RuntimeError(docker('logs', cls.envoy))
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        for container in reversed(cls.ids):
            subprocess.run(['docker', 'rm', '-f', container], capture_output=True)
        cls.temp.cleanup()

    @classmethod
    def request(cls, method, path, headers=None):
        return json.loads(docker('exec', cls.backend, 'python3', '/client.py', method, path, json.dumps(headers or {})))

    def phase(self, value):
        if value is None:
            docker('exec', self.envoy, 'rm', '-f', '/comfy-switch/phase')
        else:
            docker('exec', self.envoy, '/bin/sh', '-c',
                   f'printf "%s\\n" "{value}" > /comfy-switch/.next && mv /comfy-switch/.next /comfy-switch/phase')

    def reset_free(self, status=200):
        docker('exec', self.backend, 'sh', '-c', f'rm -f /tmp/free-calls; echo {status} > /tmp/free-status')

    def free_calls(self):
        result = docker('exec', self.backend, 'sh', '-c', 'cat /tmp/free-calls 2>/dev/null || true')
        return [json.loads(line) for line in result.splitlines()]

    def test_free_before_each_ready_prompt(self):
        self.phase('image-ready')
        for path in ('/prompt', '/api/prompt'):
            self.reset_free()
            self.assertEqual(self.request('POST', path, {'Origin': 'https://example.com', 'x-comfy-route': 'intake'}), BACKEND)
            calls = self.free_calls()
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]['body'], '{"unload_models":true,"free_memory":true}')
            headers = {k.lower(): v for k, v in calls[0]['headers'].items()}
            self.assertNotIn('origin', headers)
            self.assertEqual(headers['content-type'], 'application/json')

    def test_free_is_not_called_for_other_requests_or_phases(self):
        for phase, method, path in (('image-ready', 'GET', '/prompt'),
                                    ('image-ready', 'POST', '/upload/image'),
                                    ('image-draining', 'POST', '/prompt'),
                                    ('image-draining', 'POST', '/queue'),
                                    ('llm-open', 'POST', '/prompt')):
            self.phase(phase)
            self.reset_free()
            self.request(method, path)
            self.assertEqual(self.free_calls(), [], (phase, method, path))

    def test_free_failure_still_forwards_prompt(self):
        self.phase('image-ready')
        for status in (503, 0):
            self.reset_free(status)
            self.assertEqual(self.request('POST', '/prompt'), BACKEND)
            self.assertEqual(len(self.free_calls()), 1)
        logs = subprocess.run(['docker', 'logs', self.envoy], capture_output=True, text=True, check=True)
        self.assertIn('ComfyUI /free failed; forwarding prompt', logs.stdout + logs.stderr)

    def test_not_ready_is_fail_closed_to_intake(self):
        for phase in (None, 'broken!', 'llm-open', 'busy', 'starting-sd', 'stopping-sd', 'failed'):
            self.phase(phase)
            for method, path in (('GET', '/'), ('GET', '/api/queue'), ('POST', '/prompt'), ('POST', '/api/prompt'),
                                 ('GET', '/view?filename=x.mp4'), ('POST', '/interrupt')):
                self.assertEqual(self.request(method, path), INTAKE, (phase, method, path))

    def test_ready_proxies_the_ui_and_the_queue(self):
        self.phase('image-ready')
        for method, path in (('GET', '/'), ('GET', '/api/queue'), ('POST', '/prompt'), ('POST', '/api/prompt'),
                             ('POST', '/upload/image'), ('POST', '/api/interrupt'), ('GET', '/view?filename=x.mp4')):
            self.assertEqual(self.request(method, path), BACKEND, (method, path))
        self.assertEqual(self.request('GET', '/__comfy/status'), INTAKE)
        self.assertEqual(self.request('POST', '/__comfy/switch'), INTAKE)

    def test_internal_routes_never_reach_the_backend(self):
        self.phase('image-ready')
        for path in ('/internal/logs', '/api/internal/logs/raw', '/internal/files/output', '/internal',
                     '/api/internal', '/%69nternal/logs', '//internal/logs', '/api//internal/folder_paths'):
            self.assertEqual(self.request('GET', path), INTAKE, path)
            self.assertEqual(self.request('POST', path), INTAKE, path)

    def test_draining_lets_results_out_but_nothing_new_in(self):
        self.phase('image-draining')
        for method, path in (('GET', '/'), ('GET', '/api/history'), ('GET', '/view?filename=x.mp4'),
                             ('POST', '/interrupt'), ('POST', '/api/interrupt'), ('POST', '/queue'), ('POST', '/api/queue')):
            self.assertEqual(self.request(method, path), BACKEND, (method, path))
        for path in ('/prompt', '/api/prompt', '/prompt?x=1', '/api//prompt', '/upload/image', '/api/userdata/x.json', '/free'):
            self.assertEqual(self.request('POST', path), INTAKE, path)

    def test_client_cannot_choose_route(self):
        self.phase('llm-open')
        self.assertEqual(self.request('GET', '/', {'x-comfy-route': 'backend'}), INTAKE)
        self.phase('image-draining')
        self.assertEqual(self.request('POST', '/prompt', {'x-comfy-route': 'backend'}), INTAKE)

    def test_websocket_upgrade_reaches_backend(self):
        self.phase('image-ready')
        response = json.loads(docker('exec', self.backend, 'python3', '/client.py', 'ws'))
        self.assertIn('101', response)


if __name__ == '__main__':
    unittest.main()
