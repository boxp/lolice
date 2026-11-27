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
                               ('gateway: [\n', '[bootstrap] WARNING'),
                               ('gateway: [a]\n', '[bootstrap] WARNING'),
                               ('gateway: 3\n', '[bootstrap] WARNING'),
                               ('gateway: {a: 1,\n  b: 2}\n', '[bootstrap] WARNING')):
            path.write_text(kept)
            self.assertIn(expected, self.run_block('PYWATCHDOG', prefix))
            self.assertEqual(path.read_text(), kept)
        # An existing gateway section keeps its other settings and gains the key.
        for before, after in (
                ('model: x\ngateway:\n  other: 1\n  nested:\n    a: b\nlast: 1\n',
                 'model: x\ngateway:\n  loop_watchdog_max_strikes: 20\n  other: 1\n  nested:\n    a: b\nlast: 1\n'),
                ('gateway:  # note\n\n    # c\n    other: 1\n',
                 'gateway:  # note\n    loop_watchdog_max_strikes: 20\n\n    # c\n    other: 1\n'),
                ('gateway:\nlast: 1\n', 'gateway:\n  loop_watchdog_max_strikes: 20\nlast: 1\n'),
                ('model: x\ngateway:', 'model: x\ngateway:\n  loop_watchdog_max_strikes: 20\n'),
                ('gateway: {}\n', 'gateway: {loop_watchdog_max_strikes: 20}\n'),
                ('gateway: {other: 1}  # c\n', 'gateway: {loop_watchdog_max_strikes: 20, other: 1}  # c\n')):
            path.write_text(before)
            self.assertIn('configured gateway loop watchdog tolerance', self.run_block('PYWATCHDOG', prefix))
            self.assertEqual(path.read_text(), after)
            self.assertIn('already configured', self.run_block('PYWATCHDOG', prefix))
            self.assertEqual(path.read_text(), after)

    def test_default_model_migration(self):
        """BOXP-706 TD-08: expected results come from the vault spec
        Projects/kohako-opus-migration/default-model-spec (examples 1-10)."""
        path = self.root / 'data/config.yaml'
        marker = self.root / 'data/.boxp-default-model-revision'
        prefix = "python3 - <<'PYMODEL'\n"
        revision = 'BOXP-706-u1-20261127'
        head = '# top\nmodel:\n  provider: openai-codex\n'
        tail = '  base_url: https://chatgpt.com/backend-api/codex\ndisplay:\n  tool_progress: new\n'

        def start(text, mark=None):
            path.write_text(text)
            path.chmod(0o640)
            if mark is None:
                marker.unlink(missing_ok=True)
            else:
                marker.write_text(mark)
            return self.run_block('PYMODEL', prefix)

        # Example 1: only the value of model.default changes; the marker is written.
        out = start(head + '  default: gpt-6.1-sol\n' + tail)
        self.assertIn('[bootstrap] default model gpt-6.1-sol -> gpt-6.2-sol', out)
        self.assertNotIn('WARNING', out)
        changed = head + '  default: gpt-6.2-sol\n' + tail
        self.assertEqual(path.read_text(), changed)
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)
        self.assertEqual(marker.read_text().strip(), revision)
        self.assertFalse((self.root / 'data/config.yaml.tmp').exists())
        # Example 2: a second start changes nothing.
        out = self.run_block('PYMODEL', prefix)
        self.assertNotIn('WARNING', out)
        self.assertEqual(path.read_text(), changed)
        # Example 3: a value the Owner chose by hand after the migration is not overwritten.
        by_hand = head + '  default: gpt-6.1-sol\n' + tail
        out = start(by_hand, mark=revision + '\n')
        self.assertNotIn('->', out)
        self.assertEqual(path.read_text(), by_hand)
        self.assertEqual(marker.read_text().strip(), revision)
        # Example 4: already on the target; config untouched, marker written.
        out = start(changed)
        self.assertNotIn('WARNING', out)
        self.assertEqual(path.read_text(), changed)
        self.assertEqual(marker.read_text().strip(), revision)
        # Examples 5-7 and other unsupported shapes: warn, change neither config nor marker.
        for kept in (head + '  default: gpt-5.6-terra\n' + tail,
                     '# top\nmodel:\n  provider: custom\n  default: gpt-6.1-sol\n' + tail,
                     'model:\n  default: gpt-6.1-sol\n' + tail,
                     'model: gpt-6.1-sol\ndisplay: {}\n',
                     'model: {provider: openai-codex, default: gpt-6.1-sol}\n',
                     head + '  default: gpt-6.1-sol\n' + tail + 'model:\n  provider: openai-codex\n  default: gpt-6.1-sol\n',
                     head + '  default: &m gpt-6.1-sol\n' + tail,
                     head + '  default:\n    gpt-6.1-sol\n' + tail,
                     head + '  default: gpt-6.1-sol\n  model: gpt-5.6-terra\n' + tail,
                     'display: {}\n',
                     'model: [\n'):
            out = start(kept)
            self.assertIn('[bootstrap] WARNING', out, kept)
            self.assertEqual(path.read_text(), kept)
            self.assertFalse(marker.exists(), kept)
        # Example 8: quotes and trailing comments stay; model.model with the same value follows.
        out = start(head + '  default: "gpt-6.1-sol"  # main\n  model: gpt-6.1-sol\n' + tail)
        self.assertEqual(path.read_text(), head + '  default: "gpt-6.2-sol"  # main\n  model: gpt-6.2-sol\n' + tail)
        self.assertEqual(marker.read_text().strip(), revision)
        out = start("model:   # note\n    # c\n\n    default: 'gpt-6.1-sol'\n    provider: openai-codex")
        self.assertEqual(path.read_text(), "model:   # note\n    # c\n\n    default: 'gpt-6.2-sol'\n    provider: openai-codex")
        # Example 9: the same key in other sections is not touched.
        other = ('fallback:\n  default: gpt-6.1-sol\n  provider: openai-codex\n' + head + '  default: gpt-6.1-sol\n'
                 '  nested:\n    default: gpt-6.1-sol\n' + tail + 'cron:\n  default: gpt-6.1-sol\n')
        start(other)
        self.assertEqual(path.read_text(), other.replace(head + '  default: gpt-6.1-sol\n', head + '  default: gpt-6.2-sol\n'))
        # A marker from another revision does not stop this revision (example 10, forward direction).
        out = start(head + '  default: gpt-6.1-sol\n' + tail, mark='BOXP-000-older\n')
        self.assertEqual(path.read_text(), changed)
        self.assertEqual(marker.read_text().strip(), revision)

    def test_default_model_rollback(self):
        """Spec example 10: swapping FROM/TO under a new REVISION returns the default to 6.1."""
        path = self.root / 'data/config.yaml'
        marker = self.root / 'data/.boxp-default-model-revision'
        prefix = "python3 - <<'PYMODEL'\n"
        script = type(self).script
        for a, b in (('REVISION = "BOXP-706-u1-20261127"', 'REVISION = "BOXP-706-u1-rollback"'),
                     ('FROM = "gpt-6.1-sol"', 'FROM = "gpt-6.2-sol"'), ('TO = "gpt-6.2-sol"', 'TO = "gpt-6.1-sol"')):
            self.assertEqual(script.count(a), 1, a)
            script = script.replace(a, b)
        self.script = script
        text = 'model:\n  provider: openai-codex\n  default: gpt-6.2-sol\n  base_url: x\n'
        path.write_text(text)
        marker.write_text('BOXP-706-u1-20261127\n')
        out = self.run_block('PYMODEL', prefix)
        self.assertIn('[bootstrap] default model gpt-6.2-sol -> gpt-6.1-sol', out)
        self.assertEqual(path.read_text(), text.replace('gpt-6.2-sol', 'gpt-6.1-sol'))
        self.assertEqual(marker.read_text().strip(), 'BOXP-706-u1-rollback')


if __name__ == '__main__':
    unittest.main()
