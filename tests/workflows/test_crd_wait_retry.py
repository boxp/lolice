"""Exercise the real CRD wait block from chainsaw.yaml with a stub kubectl."""
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / '.github/workflows/chainsaw.yaml'
STUB = '''#!/bin/sh
echo "$*" >> "$CALL_LOG"
count=$(wc -l < "$CALL_LOG")
[ "$count" -gt "$FAILS" ]
'''


def block():
    source = WORKFLOW.read_text()
    start = source.index('          CRD_WAIT_DEADLINE=')
    end = source.index('          echo "CRD インストール完了"', start)
    return textwrap.dedent(source[start:end])


def run(fails, budget=120):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for name, body in (('kubectl', STUB), ('sleep', '#!/bin/sh\n/bin/sleep 0.2\n')):
            stub = root / name
            stub.write_text(body)
            stub.chmod(0o755)
        log = root / 'calls'
        log.write_text('')
        script = 'set -e\n' + block().replace('SECONDS + 120', f'SECONDS + {budget}') + 'echo finished\n'
        result = subprocess.run(['bash', '-c', script], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, PATH=f'{root}{os.pathsep}{os.environ["PATH"]}',
                                         CALL_LOG=str(log), FAILS=str(fails)))
        return result, log.read_text().splitlines()


class CrdWaitRetryTest(unittest.TestCase):
    def test_first_success_does_not_retry(self):
        result, calls = run(0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls), 1)
        self.assertIn('--for=condition=established', calls[0])
        self.assertTrue(calls[0].endswith('crd --all'))

    def test_transient_failure_is_retried(self):
        result, calls = run(2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls), 3)

    def test_persistent_failure_fails_closed_at_deadline(self):
        result, calls = run(10000, budget=3)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('finished', result.stdout)
        self.assertIn('Established', result.stderr)
        self.assertGreaterEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
