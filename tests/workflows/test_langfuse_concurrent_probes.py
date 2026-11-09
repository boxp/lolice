"""Exercise the real Langfuse probe helpers without a cluster."""
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'tests/chainsaw/langfuse/dynamic-checks.sh'


def helpers():
    source = SCRIPT.read_text()
    return source[source.index('expect() {'):source.index('\nnetwork_policy() {')]


def run_batch(probes, extra=''):
    with tempfile.TemporaryDirectory() as directory:
        # Each stub waits for all peers to start; serial execution would time out.
        script = '''set -euo pipefail
''' + helpers() + '''
probe() {
  local name="$1" code="$2" count=0
  touch "$PROBE_DIR/$name"
  while [ "$(find "$PROBE_DIR" -type f | wc -l)" -lt "$PROBE_COUNT" ]; do
    [ "$count" -lt 200 ] || return 99
    sleep 0.02
    count=$((count + 1))
  done
  return "$code"
}
probe_pids=()
''' + '\n'.join(f'expect_background {want} {label} probe {label} {code}'
                for want, label, code in probes) + '\n' + extra + '\nwait_probes\n'
        return subprocess.run(['bash', '-c', script], capture_output=True, text=True,
                              env={'PATH': '/usr/bin:/bin', 'PROBE_DIR': directory,
                                   'PROBE_COUNT': str(len(probes))}, timeout=10)


class ConcurrentProbesTest(unittest.TestCase):
    def test_success_and_overlap(self):
        probes = [('allow', 'first', 0), ('deny', 'second', 3), ('allow', 'third', 0)]
        result = run_batch(probes)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertCountEqual(result.stdout.splitlines(),
                              [f'ok {want}: {label}' for want, label, _ in probes])
        self.assertEqual(result.stderr, '')

    def test_mismatch_waits_for_all(self):
        for code in (0, 1):
            with self.subTest(code=code):
                result = run_batch([('deny', 'bad', code), ('allow', 'other', 0),
                                    ('deny', 'last', 3)])
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f'NetworkPolicy 不一致: bad は deny のはずが exit {code}', result.stderr)
                self.assertCountEqual(result.stdout.splitlines(), ['ok allow: other', 'ok deny: last'])

    def test_killed_probe_fails_closed_and_waits_for_others(self):
        result = run_batch([('allow', 'victim', 0), ('allow', 'other', 0)],
                           'kill -STOP "${probe_pids[0]}"\nkill -KILL "${probe_pids[0]}"')
        self.assertNotEqual(result.returncode, 0)
        # The survivor may time out if the victim was killed before starting, but is still awaited.
        self.assertTrue('ok allow: other' in result.stdout or
                        'NetworkPolicy 不一致: other は allow のはずが exit 99' in result.stderr)

    def test_probe_counts(self):
        source = SCRIPT.read_text()
        for name, following, count in [('network_policy', 'otlp', 13),
                                       ('usage_view', 'clickhouse_count', 6)]:
            with self.subTest(section=name):
                section = source.split(f'\n{name}() {{', 1)[1].split(f'\n{following}() {{', 1)[0]
                self.assertEqual(len(re.findall(r'^  expect_background (?:allow|deny) ', section, re.M)), count)
                self.assertEqual(len(re.findall(r'^  wait_probes$', section, re.M)), 1)


if __name__ == '__main__':
    unittest.main()
