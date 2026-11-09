import importlib.util
import json
from pathlib import Path
import tempfile
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


if __name__ == '__main__':
    unittest.main()
