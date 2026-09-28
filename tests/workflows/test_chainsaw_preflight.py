"""Exercise the actual workflow report gate without a Kubernetes cluster."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest

WORKFLOW = Path(__file__).resolve().parents[2] / '.github/workflows/chainsaw-preflight.yaml'
SOURCE = textwrap.dedent(WORKFLOW.read_text().split("<<'PYREPORT'\n", 1)[1].split('\n          PYREPORT', 1)[0])


class ReportGateTest(unittest.TestCase):
    def run_gate(self, xml, status=0, log_status=0):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            if xml is not None:
                (root / 'chainsaw-report.xml').write_text(xml)
            env = dict(os.environ, CHAINSAW_STATUS=str(status), LOG_STATUS=str(log_status),
                       GITHUB_STEP_SUMMARY=str(root / 'summary'))
            run = subprocess.run(['python3', '-c', SOURCE, directory], env=env, capture_output=True, text=True)
            self.assertIn(run.returncode, (0, 1), run.stderr)
            return run.returncode, json.loads((root / 'result.json').read_text())

    def test_nonempty_success(self):
        code, result = self.run_gate('<testsuites><testsuite><testcase name="pass"/></testsuite></testsuites>')
        self.assertEqual(code, 0)
        self.assertEqual(result['passed'], 1)

    def test_skips_errors_and_failures_are_not_passes(self):
        for tag, field in [('skipped', 'skipped'), ('failure', 'failures'), ('error', 'errors')]:
            with self.subTest(tag=tag):
                code, result = self.run_gate(f'<testsuites><testsuite><testcase><{tag}/></testcase><testcase/></testsuite></testsuites>')
                self.assertEqual(code, 1)
                self.assertEqual(result[field], 1)
                self.assertEqual(result['passed'], 1)

    def test_missing_empty_and_malformed_reports_fail(self):
        for xml in [None, '', '<testsuites/>', '<testsuites>']:
            with self.subTest(xml=xml):
                code, result = self.run_gate(xml)
                self.assertEqual(code, 1)
                self.assertEqual(result['passed'], 0)

    def test_failed_process_or_log_is_not_overridden_by_report(self):
        for status, log_status in [(1, 0), (0, 1)]:
            code, _ = self.run_gate('<testsuites><testcase/></testsuites>', status, log_status)
            self.assertEqual(code, 1)


if __name__ == '__main__':
    unittest.main()
