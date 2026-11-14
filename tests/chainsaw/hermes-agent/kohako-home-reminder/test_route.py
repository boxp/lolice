"""本番 ConfigMap から route と整形スクリプトを読み、外部通信なしで検証する。"""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[4]


class RouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = yaml.safe_load((ROOT / 'argoproj/hermes-agent/configmap.yaml').read_text())['data']
        cls.tmp = tempfile.TemporaryDirectory()
        cls.script = Path(cls.tmp.name) / 'route.clj'
        cls.script.write_text(cls.config['kohako_home_reminder.clj'])

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_route(self, payload):
        return subprocess.run(['bb', str(self.script)], input=json.dumps(payload), text=True, capture_output=True)

    def test_route_and_deployment(self):
        route = json.loads(self.config['kohako_home_reminder_route.json'])['kohako-home-reminder']
        self.assertEqual(route, {'script': 'kohako_home_reminder.sh', 'prompt': '{message}', 'deliver_only': True, 'deliver': 'telegram', 'mirror_to_session': True, 'enabled': True})
        deployment = (ROOT / 'argoproj/hermes-agent/deployment.yaml').read_text()
        self.assertIn('/bootstrap/kohako_home_reminder_route.json', deployment)
        self.assertIn('/opt/data/scripts/kohako_home_reminder.clj', deployment)
        self.assertIn('/webhooks/kohako-home-reminder', (ROOT / 'argoproj/codex-workspace/deployment.yaml').read_text())

    def test_alertmanager_channel_and_plugin_rendered(self):
        route = json.loads(self.config['alertmanager_webhook_route.json'])['alertmanager']
        self.assertEqual(route['deliver'], 'boxp_notify')
        self.assertEqual(route['deliver_extra'], {'chat_id': 'direct:alertmanager'})
        self.assertFalse(route['mirror_to_session'])
        self.assertTrue(route['deliver_only'])
        rendered = subprocess.check_output(['kustomize', 'build', str(ROOT / 'argoproj/hermes-agent')], text=True)
        docs = list(yaml.safe_load_all(rendered))
        deployment = next(d for d in docs if d['kind'] == 'Deployment' and d['metadata']['name'] == 'hermes-agent')
        pod = deployment['spec']['template']['spec']
        volume = next(v for v in pod['volumes'] if v['name'] == 'boxp-notify-plugin')
        cm = next(d for d in docs if d['kind'] == 'ConfigMap' and d['metadata']['name'] == volume['configMap']['name'])
        self.assertEqual(set(cm['data']), {'plugin.yaml', '__init__.py'})
        init = next(c for c in pod['initContainers'] if c['name'] == 'bootstrap-config')
        self.assertTrue(any(m['name'] == 'boxp-notify-plugin' and m['mountPath'] == '/bootstrap-plugins/boxp-notify' for m in init['volumeMounts']))
        self.assertIn('boxp-notify', yaml.safe_load(self.config['config.yaml'])['plugins']['enabled'])

    def test_valid_and_limits(self):
        for kind, limit in [('reminder', 300), ('fallback', 3800)]:
            result = self.run_route({'kind': kind, 'text': 'あ\u0001' + '文' * 4000})
            self.assertEqual(result.returncode, 0)
            text = json.loads(result.stdout)['message']
            self.assertEqual(len(text), limit)
            self.assertNotIn('\u0001', text)

    def test_invalid(self):
        for payload in [{'kind': 'other', 'text': 'x'}, {'kind': 'reminder', 'text': ''}, {'kind': 'fallback', 'text': '\u0001'}, {}]:
            self.assertNotEqual(self.run_route(payload).returncode, 0)


if __name__ == '__main__':
    unittest.main()
