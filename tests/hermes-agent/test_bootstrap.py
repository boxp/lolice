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

    def test_route_and_failure_closed(self):
        source = json.loads(self.data['task_board_webhook_route.json'])['task-board']
        self.run_block('PY', "python3 - <<'PY'\n")
        routes = json.loads((self.root / 'data/webhook_subscriptions.json').read_text())
        self.assertEqual(routes['task-board'], source)
        channel = routes['task-board-channel']
        self.assertEqual(channel['deliver'], 'boxp_notify')
        self.assertEqual(channel['deliver_extra'], {'chat_id': 'tb:{event_id}'})
        self.assertFalse(channel['mirror_to_session'])
        for key in source.keys() - {'deliver', 'deliver_extra', 'mirror_to_session', 'description', 'prompt'}:
            self.assertEqual(channel[key], source[key])
        self.assertIn('Owner の返信は家の会話で受け', channel['prompt'])
        self.assertIn('最終応答は通知用 channel が家', channel['prompt'])
        sentences = (
            '最終応答がそのまま Telegram へ届き、同じ文面が Telegram 会話の履歴にも `[Webhook delivery: task-board]` 付きの通知結果として残る（mirror_to_session）。',
            'Owner の返信は Telegram セッションで受けるため、返信に必要な文脈（要点・必要入力）は本文と Notes の summary の両方に残す。',
        )
        for sentence in sentences:
            for duplicate in (False, True):
                broken = json.loads(self.data['task_board_webhook_route.json'])
                prompt = broken['task-board']['prompt']
                broken['task-board']['prompt'] = prompt + sentence if duplicate else prompt.replace(sentence, '')
                (self.root / 'bootstrap/task_board_webhook_route.json').write_text(json.dumps(broken))
                self.assertIn('[bootstrap] WARNING', self.run_block('PY', "python3 - <<'PY'\n"))
                routes = json.loads((self.root / 'data/webhook_subscriptions.json').read_text())
                self.assertFalse(routes['task-board-channel']['enabled'])
                self.assertEqual(routes['task-board'], broken['task-board'])

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


if __name__ == '__main__':
    unittest.main()
