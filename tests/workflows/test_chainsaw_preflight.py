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
VALIDATION_SOURCE = textwrap.dedent(
    WORKFLOW.read_text().split('        run: |\n', 1)[1].split(
        '          echo "manifest_sha=$MANIFEST_SHA" >> "$GITHUB_OUTPUT"\n', 1)[0]
)


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


class InputValidationTest(unittest.TestCase):
    IMAGE = 'kindest/node@sha256:' + 'a' * 64
    CANDIDATE_IMAGE = 'kindest/node@sha256:' + 'b' * 64

    def run_validation(self, current, candidate):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'github-output'
            env = dict(
                os.environ,
                MANIFEST_SHA='a' * 40,
                CURRENT_KIND_IMAGE=self.IMAGE,
                CANDIDATE_KIND_IMAGE=self.CANDIDATE_IMAGE,
                CURRENT_KUBERNETES_VERSION=current,
                CANDIDATE_KUBERNETES_VERSION=candidate,
                GITHUB_OUTPUT=str(output),
            )
            return subprocess.run(['bash', '-c', VALIDATION_SOURCE], env=env, capture_output=True, text=True)

    def test_allows_patch_and_minor_upgrades(self):
        for current, candidate in [('v1.36.1', 'v1.36.2'), ('v1.36.9', 'v1.37.0')]:
            with self.subTest(current=current, candidate=candidate):
                self.assertEqual(self.run_validation(current, candidate).returncode, 0)

    def test_rejects_equal_or_downgrade_candidates(self):
        for current, candidate in [('v1.36.1', 'v1.36.1'), ('v1.36.1', 'v1.36.0'), ('v1.37.0', 'v1.36.9')]:
            with self.subTest(current=current, candidate=candidate):
                result = self.run_validation(current, candidate)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('must be a later', result.stderr)

    def test_rejects_non_semver_version_identifiers(self):
        self.assertNotEqual(self.run_validation('v1.036.1', 'v1.36.2').returncode, 0)


if __name__ == '__main__':
    unittest.main()
