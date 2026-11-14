"""Run the actual bootstrap Python against isolated fixture files."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]


class BootstrapTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        deployment = yaml.safe_load((ROOT / 'argoproj/hermes-agent/deployment.yaml').read_text())
        cls.script = deployment['spec']['template']['spec']['initContainers'][0]['command'][2]
        cls.data = yaml.safe_load((ROOT / 'argoproj/hermes-agent/configmap.yaml').read_text())['data']

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'bootstrap').mkdir()
        (self.root / 'data').mkdir()
        for name in ('task_board_webhook_route.json', 'kohako_home_reminder_route.json', 'alertmanager_webhook_route.json'):
            (self.root / 'bootstrap' / name).write_text(self.data[name])

    def run_block(self, marker, prefix):
        start = self.script.index(prefix) + len(prefix)
        end = self.script.index('\n' + marker + '\n', start)
        code = self.script[start:end].replace('/opt/data/', str(self.root / 'data') + '/').replace('/bootstrap/', str(self.root / 'bootstrap') + '/')
        code = code.replace('os.chown(tmp, 1000, 10000)', 'os.chown(tmp, os.getuid(), os.getgid())')
        return subprocess.run(['python3', '-c', code], text=True, capture_output=True, check=True).stdout

    def test_channel_route_only(self):
        """BOXP-556: task-board-channel is the only Task Board route; the former Telegram route is removed."""
        managed = json.loads(self.data['task_board_webhook_route.json'])
        self.assertEqual(list(managed), ['task-board-channel'])
        channel = managed['task-board-channel']
        self.assertEqual(channel['deliver'], 'boxp_notify')
        self.assertEqual(channel['deliver_extra'], {'chat_id': 'tb:{event_id}'})
        self.assertFalse(channel['mirror_to_session'])
        self.assertTrue(channel['enabled'])
        self.assertEqual(channel['script'], 'task_board_webhook_filter.sh')
        self.assertIn('Owner の返信は家の会話で受け', channel['prompt'])
        self.assertIn('最終応答は通知用 channel が家', channel['prompt'])
        for gone in ('mirror_to_session）', 'Telegram セッションで受ける', '[Webhook delivery: task-board]'):
            self.assertNotIn(gone, channel['prompt'])
        # A PVC written by the previous bootstrap: the old route, the derived channel route, a dynamic one.
        old = {'task-board': {'deliver': 'telegram', 'mirror_to_session': True, 'enabled': True},
               'task-board-channel': {'deliver': 'boxp_notify', 'enabled': False, 'prompt': 'stale'},
               'dynamic': {'deliver': 'log'}}
        (self.root / 'data/webhook_subscriptions.json').write_text(json.dumps(old))
        out = self.run_block('PY', "python3 - <<'PY'\n")
        self.assertIn('[bootstrap] removed the former task-board webhook route', out)
        routes = json.loads((self.root / 'data/webhook_subscriptions.json').read_text())
        self.assertNotIn('task-board', routes)
        self.assertEqual(routes['task-board-channel'], channel)
        self.assertEqual(routes['dynamic'], {'deliver': 'log'})
        self.assertIn('kohako-home-reminder', routes)
        self.assertFalse(routes['alertmanager']['enabled'])  # no secret in this fixture: fail closed
        # A second Pod start changes nothing and reports no removal.
        before = (self.root / 'data/webhook_subscriptions.json').read_text()
        self.assertNotIn('removed the former', self.run_block('PY', "python3 - <<'PY'\n"))
        self.assertEqual((self.root / 'data/webhook_subscriptions.json').read_text(), before)

    def test_display_migration(self):
        path = self.root / 'data/config.yaml'
        prefix = "python3 - <<'PYDISPLAY'\n"
        original = 'platforms:\n  telegram: yes\ndisplay:\n  tool_progress: new\nother: 42\n'
        path.write_text(original)
        path.chmod(0o640)
        self.run_block('PYDISPLAY', prefix)
        changed = path.read_text()
        self.assertEqual(changed, original.replace('display:\n', 'display:\n  platforms:\n    webhook:\n      suppress_warning_notifications: true\n'))
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)
        self.run_block('PYDISPLAY', prefix)
        self.assertEqual(path.read_text(), changed)
        for invalid in ('display: {}\n', 'display:\n  platforms: {}\n', 'display: null\n', 'other: 1\n', 'display:\n  tool_progress: new\ndisplay:\n  tool_progress: old\n'):
            path.write_text(invalid)
            self.assertIn('[bootstrap] WARNING', self.run_block('PYDISPLAY', prefix))
            self.assertEqual(path.read_text(), invalid)

    def test_watchdog_migration(self):
        path = self.root / 'data/config.yaml'
        prefix = "python3 - <<'PYWATCHDOG'\n"
        original = 'model: x\ndisplay:\n  tool_progress: new\n'
        path.write_text(original)
        path.chmod(0o640)
        self.assertIn('configured gateway loop watchdog tolerance', self.run_block('PYWATCHDOG', prefix))
        changed = path.read_text()
        self.assertTrue(changed.startswith(original))
        loaded = yaml.safe_load(changed)
        self.assertEqual(loaded['gateway'], {'loop_watchdog_max_strikes': 20})
        self.assertEqual({k: v for k, v in loaded.items() if k != 'gateway'}, yaml.safe_load(original))
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)
        self.assertIn('already configured', self.run_block('PYWATCHDOG', prefix))
        self.assertEqual(path.read_text(), changed)
        for kept, expected in (('gateway:\n  loop_watchdog_max_strikes: 5\n', 'already configured'),
                               ('loop_watchdog_max_strikes: 7\n', 'already configured'),
                               ('gateway:\n  other: 1\n', '[bootstrap] WARNING'),
                               ('gateway: [\n', '[bootstrap] WARNING')):
            path.write_text(kept)
            self.assertIn(expected, self.run_block('PYWATCHDOG', prefix))
            self.assertEqual(path.read_text(), kept)


if __name__ == '__main__':
    unittest.main()
