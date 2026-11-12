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
SERVER = base.SERVER.replace('7861', '8189')
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
