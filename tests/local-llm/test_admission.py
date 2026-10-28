#!/usr/bin/env python3
"""Exercise the deployed Envoy digest; no Kubernetes access, no GPU/model/API."""
import concurrent.futures
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
BACKEND = 'python:3.12-slim@sha256:6b25cbe6577518889d553cdd5a5da97d259348d8f6e45b527dd731caaba27a07'
SERVER = '''
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
import time
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b'{"data":[{"id":"gemma4-26b","status":{"value":"loaded"}},{"id":"qwen38-27b","status":{"value":"unloaded"}}]}' if self.path == '/models' else self.path.encode())
    def do_POST(self):
        if self.path == '/slow':
            Path('/tmp/started').touch()
            while not Path('/tmp/release').exists(): time.sleep(.05)
        self.send_response(200); self.end_headers(); self.wfile.write(b'completed')
ThreadingHTTPServer(('127.0.0.1',8081), Handler).serve_forever()
'''
CLIENT = '''
import urllib.request, urllib.error, json, sys
try:
    with urllib.request.urlopen(urllib.request.Request(sys.argv[1], method=sys.argv[2]), timeout=20) as r:
        print(json.dumps([r.status, r.read().decode()]))
except urllib.error.HTTPError as e:
    print(json.dumps([e.code, e.read().decode()]))
'''


def docker(*args):
    return subprocess.check_output(['docker', *args], text=True, stderr=subprocess.PIPE).strip()


class AdmissionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ids = []
        cls.temp = tempfile.TemporaryDirectory()
        cls.directory = Path(cls.temp.name)
        config = textwrap.dedent((ROOT / 'argoproj/local-llm/envoy-config.yaml').read_text().split('  envoy.yaml: |\n', 1)[1])
        (cls.directory / 'envoy.yaml').write_text(config)
        (cls.directory / 'server.py').write_text(SERVER)
        (cls.directory / 'state').write_text('open:test-a')
        try:
            name = 'boxp344-' + uuid.uuid4().hex[:12]
            cls.backend = docker('create', '--name', name, '--network', 'none', BACKEND, 'python3', '/server.py')
            cls.ids.append(cls.backend)
            docker('cp', str(cls.directory / 'server.py'), cls.backend + ':/server.py')
            docker('start', cls.backend)
            cls.envoy = docker('create', '--network', 'container:' + cls.backend,
                               '--entrypoint', '/usr/local/bin/envoy', ENVOY,
                               '-c', '/envoy.yaml', '--concurrency', '2', '--log-level', 'warn')
            cls.ids.append(cls.envoy)
            docker('cp', str(cls.directory / 'envoy.yaml'), cls.envoy + ':/envoy.yaml')
            # docker cp creates a directory from a directory source.
            gate = cls.directory / 'llm-admission'
            gate.mkdir()
            (gate / 'state').write_text('open:test-a')
            docker('cp', str(gate), cls.envoy + ':/etc/llm-admission')
            docker('start', cls.envoy)
            for _ in range(60):
                try:
                    if cls.request('/health')[0] == 200:
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
    def request(cls, path, method='GET', port=8080):
        return json.loads(docker('exec', cls.backend, 'python3', '-c', CLIENT,
                                 f'http://127.0.0.1:{port}{path}', method))

    def state(self, value):
        (self.directory / 'state').write_text(value)
        docker('cp', str(self.directory / 'state'), self.envoy + ':/tmp/state.next')
        docker('exec', self.envoy, '/bin/sh', '-c', 'mv /tmp/state.next /etc/llm-admission/state')

    def test_fence_drain_and_restore(self):
        self.assertEqual(self.request('/v1/chat/completions', 'POST')[0], 200)
        with concurrent.futures.ThreadPoolExecutor() as pool:
            slow = pool.submit(self.request, '/slow', 'POST')
            for _ in range(60):
                try:
                    docker('exec', self.backend, 'test', '-f', '/tmp/started')
                    break
                except subprocess.CalledProcessError:
                    time.sleep(.1)
            else:
                self.fail('long request did not start')
            self.state('closed:test-b')
            self.assertEqual(self.request('/__board_pilot__/admission'), [200, 'closed:test-b\n'])
            self.assertEqual(self.request('/v1/chat/completions', 'POST')[0], 503)
            self.assertFalse(slow.done(), 'fence must preserve admitted inference')
            stats = self.request('/stats?format=json', port=9901)
            gauges = {x['name']: x['value'] for x in json.loads(stats[1])['stats'] if 'name' in x}
            self.assertEqual(gauges['cluster.llama_server.upstream_rq_active'], 1)
            self.assertEqual(self.request('/health')[0], 200)
            docker('exec', self.backend, 'touch', '/tmp/release')
            self.assertEqual(slow.result(timeout=20), [200, 'completed'])
        stats = self.request('/stats?format=json', port=9901)
        gauges = {x['name']: x['value'] for x in json.loads(stats[1])['stats'] if 'name' in x}
        self.assertEqual(gauges['cluster.llama_server.upstream_rq_active'], 0)
        self.assertEqual(gauges['cluster.llama_server.upstream_rq_pending_active'], 0)
        self.state('open:test-c')
        self.assertEqual(self.request('/v1/chat/completions', 'POST')[0], 200)
        for value in ('', 'broken', 'open:bad/token', 'unknown:token'):
            self.state(value)
            self.assertEqual(self.request('/v1/chat/completions', 'POST')[0], 503)
        docker('exec', self.envoy, 'rm', '/etc/llm-admission/state')
        self.assertEqual(self.request('/v1/chat/completions', 'POST')[0], 503)
        self.state('open:test-d')
        for path in ('/quitquitquit', '/healthcheck/fail', '/runtime_modify', '/config_dump'):
            self.assertEqual(self.request(path, 'POST', 9901)[0], 404)
            self.assertEqual(self.request(path, 'GET', 9901)[0], 404)
        self.assertEqual(self.request('/stats/prometheus', port=9901)[0], 200)
        self.assertEqual(self.request('/server_info', port=9901)[0], 200)
        self.assertEqual(self.request('/stats', 'POST', 9901)[0], 404)
        self.assertEqual(self.request('/__board_pilot__/restore-probe', 'POST')[0], 503)
        self.state('verify:test-e')
        self.assertEqual(self.request('/v1/chat/completions', 'POST')[0], 503)
        self.assertEqual(self.request('/__board_pilot__/restore-probe', 'GET', 9901)[0], 503)
        self.assertEqual(self.request('/__board_pilot__/restore-probe', 'POST', 9901), [200, 'completed'])
        self.assertEqual(self.request('/__board_pilot__/restore-probe', 'POST', 9901)[0], 429)
        self.assertEqual(self.request('/__board_pilot__/slots?model=qwen38-27b', port=9901)[0], 503)
        self.assertEqual(self.request('/__board_pilot__/slots?model=gemma4-26b', port=9901),
                         [200, '/slots?model=gemma4-26b'])
        self.assertEqual(json.loads(self.request('/__board_pilot__/models', port=9901)[1])['data'][0]['id'], 'gemma4-26b')
        self.assertEqual(self.request('/__board_pilot__/slots', 'POST', 9901)[0], 503)


if __name__ == '__main__':
    unittest.main(verbosity=2)
