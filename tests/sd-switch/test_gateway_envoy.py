"""Exercise the gateway Lua and routes on the deployed Envoy digest."""
import json
from pathlib import Path
import subprocess
import tempfile
import textwrap
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[2]
ENVOY = 'docker.io/envoyproxy/envoy@sha256:4d496918618a7ebd6c71ae8285e31ebff092f3a0a5ad642d50decf4a54eb2456'
PYTHON = 'python:3.12-slim@sha256:6b25cbe6577518889d553cdd5a5da97d259348d8f6e45b527dd731caaba27a07'
SERVER = '''
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import threading
def make(name):
    class Handler(BaseHTTPRequestHandler):
        def reply(self):
            if self.path == '/ws' and name == 'backend':
                self.send_response(101); self.send_header('Upgrade', 'websocket')
                self.send_header('Connection', 'Upgrade'); self.end_headers(); return
            self.send_response(200); self.send_header('Content-Length', str(len(name))); self.end_headers()
            self.wfile.write(name.encode())
        do_GET = do_POST = reply
        def log_message(self, *args): pass
    return Handler
threading.Thread(target=ThreadingHTTPServer(('127.0.0.1', 7861), make('backend')).serve_forever, daemon=True).start()
ThreadingHTTPServer(('127.0.0.1', 8090), make('intake')).serve_forever()
'''
CLIENT = '''
import json, socket, sys, urllib.request, urllib.error
if sys.argv[1] == 'ws':
    s = socket.create_connection(('127.0.0.1', 7860), timeout=10)
    s.sendall(b'GET /ws HTTP/1.1\\r\\nHost: x\\r\\nConnection: Upgrade\\r\\nUpgrade: websocket\\r\\n\\r\\n')
    print(json.dumps(s.recv(64).decode().split('\\r\\n')[0])); raise SystemExit
method, path, headers = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
request = urllib.request.Request('http://127.0.0.1:7860' + path, data=b'{}' if method == 'POST' else None, method=method, headers=headers)
try:
    with urllib.request.urlopen(request, timeout=10) as r: print(json.dumps([r.status, r.read().decode()]))
except urllib.error.HTTPError as e: print(json.dumps([e.code, e.read().decode()]))
'''


def docker(*args):
    return subprocess.check_output(['docker', *args], text=True, stderr=subprocess.PIPE).strip()


class GatewayRoutingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ids = []
        cls.temp = tempfile.TemporaryDirectory()
        directory = Path(cls.temp.name)
        config = textwrap.dedent((ROOT / 'argoproj/stable-diffusion/sd-gateway-envoy-config.yaml').read_text().split('  envoy.yaml: |\n', 1)[1])
        config = (config.replace('type: STRICT_DNS', 'type: STATIC')
                  .replace('address: stable-diffusion-webui-backend.stable-diffusion.svc.cluster.local', 'address: 127.0.0.1'))
        before, after = config.rsplit('port_value: 7860', 1)
        (directory / 'envoy.yaml').write_text(before + 'port_value: 7861' + after)
        (directory / 'server.py').write_text(SERVER)
        (directory / 'client.py').write_text(CLIENT)
        try:
            name = 'boxp484-' + uuid.uuid4().hex[:12]
            cls.backend = docker('create', '--name', name, '--network', 'none', PYTHON, 'python3', '/server.py')
            cls.ids.append(cls.backend)
            docker('cp', str(directory / 'server.py'), cls.backend + ':/server.py')
            docker('cp', str(directory / 'client.py'), cls.backend + ':/client.py')
            docker('start', cls.backend)
            cls.envoy = docker('create', '--network', 'container:' + cls.backend, '--entrypoint', '/usr/local/bin/envoy',
                               ENVOY, '-c', '/envoy.yaml', '--concurrency', '1', '--log-level', 'warn')
            cls.ids.append(cls.envoy)
            docker('cp', str(directory / 'envoy.yaml'), cls.envoy + ':/envoy.yaml')
            shared = directory / 'sd-switch'
            shared.mkdir()
            docker('cp', str(shared), cls.envoy + ':/sd-switch')
            docker('start', cls.envoy)
            for _ in range(60):
                try:
                    if cls.request('GET', '/__sd/health')[0] == 200:
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
            docker('exec', self.envoy, 'rm', '-f', '/sd-switch/phase')
        else:
            docker('exec', self.envoy, '/bin/sh', '-c', f'printf "%s\\n" "{value}" > /sd-switch/.next && mv /sd-switch/.next /sd-switch/phase')

    def test_not_ready_is_fail_closed_to_intake(self):
        for phase in (None, 'broken!', 'llm-open', 'starting-sd', 'image-draining', 'failed'):
            self.phase(phase)
            for method, path in (('GET', '/'), ('GET', '/file=x.js'), ('POST', '/sdapi/v1/txt2img'), ('GET', '/sdapi/v1/progress')):
                self.assertEqual(self.request(method, path), [200, 'intake'], (phase, method, path))

    def test_image_ready_proxies_except_durable_generation(self):
        self.phase('image-ready')
        self.assertEqual(self.request('GET', '/'), [200, 'backend'])
        self.assertEqual(self.request('POST', '/queue/join'), [200, 'backend'])
        self.assertEqual(self.request('GET', '/__sd/status'), [200, 'intake'])
        for path in ('/sdapi/v1/txt2img', '/sdapi/v1/img2img', '/sdapi/v1/txt2img?x=1', '/sdapi/v1//txt2img'):
            self.assertEqual(self.request('POST', path), [200, 'intake'], path)

    def test_client_cannot_choose_route(self):
        self.phase('llm-open')
        self.assertEqual(self.request('GET', '/', {'x-sd-route': 'backend'}), [200, 'intake'])
        self.phase('image-ready')
        self.assertEqual(self.request('POST', '/sdapi/v1/txt2img', {'x-sd-route': 'backend'}), [200, 'intake'])

    def test_websocket_upgrade_reaches_backend(self):
        self.phase('image-ready')
        response = json.loads(docker('exec', self.backend, 'python3', '/client.py', 'ws'))
        self.assertIn('101', response)


if __name__ == '__main__':
    unittest.main()
