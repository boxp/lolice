"""The suite must refuse unmarked local execution before invoking kubectl."""
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[2]


class IsolationGuardTest(unittest.TestCase):
    def test_context_guard_on_every_mutating_suite(self):
        for component in ['argocd', 'external-secrets-operator', 'kube-vip']:
            source = (ROOT / 'tests/chainsaw' / component / 'chainsaw-test.yaml').read_text()
            guard = textwrap.dedent(source.split('        content: |\n', 1)[1].split('\n  - name:', 1)[0])
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                stub = root / 'kubectl'
                stub.write_text('#!/bin/sh\nprintf called >> "$CALL_LOG"\nprintf "%s\\n" "$ACTUAL_CONTEXT"\n')
                stub.chmod(0o755)
                for expected, actual, success, called in [('', 'in-cluster', False, False),
                                                          ('in-cluster', 'in-cluster', False, False),
                                                          ('kind-test', 'in-cluster', False, True),
                                                          ('kind-test', 'kind-test', True, True)]:
                    with self.subTest(component=component, expected=expected, actual=actual):
                        log = root / 'calls'
                        log.write_text('')
                        env = dict(os.environ, PATH=directory + os.pathsep + os.environ['PATH'],
                                   CHAINSAW_KIND_CONTEXT=expected, ACTUAL_CONTEXT=actual, CALL_LOG=str(log))
                        result = subprocess.run(['sh', '-c', guard], env=env, capture_output=True)
                        self.assertEqual(result.returncode == 0, success)
                        self.assertEqual(bool(log.read_text()), called)


if __name__ == '__main__':
    unittest.main()
