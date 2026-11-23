"""Deadline regressions; negative lab scenarios retain their observation sleeps."""
import importlib.util
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
from types import SimpleNamespace
import unittest

spec = importlib.util.spec_from_file_location('lab_wait', Path(__file__).parent / 'notify-lab' / 'wait.py')
wait = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wait)


class WaitTest(unittest.TestCase):
    def command(self, code):
        return [sys.executable, '-c', code]

    def test_success_and_timeout(self):
        self.assertTrue(wait.wait(self.command('pass'), 1, .01))
        self.assertFalse(wait.wait(self.command('raise SystemExit(1)'), .15, .01))
        self.assertFalse(wait.wait(self.command('from unittest.mock import patch
from types import SimpleNamespace; time.sleep(1)'), .1, .01))

    def test_retries_until_match_with_decreasing_command_budget(self):
        with patch.object(wait.time, 'monotonic', side_effect=[0, 0, .2, .4]), \
             patch.object(wait.time, 'sleep') as sleep, \
             patch.object(wait.subprocess, 'run', side_effect=[SimpleNamespace(returncode=1), SimpleNamespace(returncode=0)]) as run:
            self.assertTrue(wait.wait(['probe'], 1, .2))
        self.assertEqual(run.call_count, 2)
        self.assertEqual([call.kwargs['timeout'] for call in run.call_args_list], [1, .6])
        sleep.assert_called_once_with(.2)

    def test_deadline_expires_without_another_probe(self):
        with patch.object(wait.time, 'monotonic', side_effect=[0, 0, .8, 1]), \
             patch.object(wait.time, 'sleep') as sleep, \
             patch.object(wait.subprocess, 'run', return_value=SimpleNamespace(returncode=1)) as run:
            self.assertFalse(wait.wait(['probe'], 1, .5))
        self.assertEqual(run.call_count, 1)
        self.assertAlmostEqual(sleep.call_args.args[0], .2)


if __name__ == '__main__':
    unittest.main()
