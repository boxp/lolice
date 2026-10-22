"""Guard the PR status-check wiring that the main ruleset relies on (BOXP-266).

The ruleset for main requires the ``status-check`` context. If the workflow
regains a ``paths`` filter, or a job is added without being listed in
``status-check.needs``, PRs either can never merge (missing context) or merge
with a failed job that nobody aggregated. These tests read the workflow text
so they run without PyYAML.
"""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / '.github/workflows/chainsaw.yaml'


def job_ids(text):
    jobs = text.split('\njobs:\n', 1)[1]
    return re.findall(r'^  ([A-Za-z0-9_-]+):$', jobs, flags=re.MULTILINE)


def job_block(text, job):
    jobs = text.split('\njobs:\n', 1)[1]
    match = re.search(rf'^  {re.escape(job)}:$\n(.*?)(?=^  [A-Za-z0-9_-]+:$|\Z)', jobs, flags=re.MULTILINE | re.DOTALL)
    assert match, job
    return match.group(1)


class StatusCheckWiringTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text()

    def test_workflow_runs_on_every_pull_request(self):
        trigger = self.text.split('\non:\n', 1)[1].split('\njobs:\n', 1)[0]
        self.assertIn('pull_request:', trigger)
        self.assertNotIn('paths:', trigger, 'required status check needs this workflow on docs-only PRs too')
        self.assertNotIn('paths-ignore:', trigger)

    def test_status_check_aggregates_every_other_job(self):
        ids = job_ids(self.text)
        self.assertIn('status-check', ids)
        block = job_block(self.text, 'status-check')
        needs = re.findall(r'^      - ([A-Za-z0-9_-]+)$', block.split('needs:', 1)[1].split('if:', 1)[0], flags=re.MULTILINE)
        self.assertEqual(sorted(needs), sorted(j for j in ids if j != 'status-check'))
        self.assertIn('if: always()', block)
        self.assertIn('permissions: {}', block)
        self.assertIn('.value.result == "success" or .value.result == "skipped"', block)

    def test_static_policy_runs_without_path_condition(self):
        block = job_block(self.text, 'static-policy')
        self.assertNotIn('\n    if:', block)
        self.assertIn('scripts/static-manifest-check.sh', block)
        self.assertIn('conftest verify --no-color -p policy/kubernetes', block)
        self.assertIn('conftest verify --no-color -p policy/kustomize', block)
        self.assertIn('python3 -B -m unittest discover -s tests/workflows', block)
        self.assertEqual(block.count('sha256sum -c -'), 2, 'kustomize and conftest downloads must be checksum verified')

    def test_chainsaw_still_selects_components_from_detect(self):
        block = job_block(self.text, 'chainsaw')
        self.assertIn("if: needs.detect.outputs.has_tests == 'true'", block)

    def test_renovate_validator_only_on_config_change(self):
        block = job_block(self.text, 'renovate-config-validator')
        self.assertIn("if: needs.detect.outputs.renovate_config_changed == 'true'", block)
        self.assertIn('npx --yes --package renovate@', block)
        self.assertIn('-c "renovate-config-validator --strict renovate.json"', block)
        self.assertIn("grep -qx 'renovate.json' /tmp/changed_files.txt", job_block(self.text, 'detect'))


class StaticManifestCheckScriptTest(unittest.TestCase):
    def test_script_is_executable_and_strict(self):
        script = ROOT / 'scripts/static-manifest-check.sh'
        self.assertTrue(script.stat().st_mode & 0o111)
        text = script.read_text()
        self.assertIn('set -euo pipefail', text)
        self.assertIn('kustomize build argoproj/', text)
        self.assertIn('conftest test --no-color -p policy/kubernetes', text)
        self.assertIn('conftest test --no-color -p policy/kustomize', text)

    def test_policy_dirs_have_tests(self):
        for policy_dir in ['policy/kubernetes', 'policy/kustomize']:
            rego = sorted(p.name for p in (ROOT / policy_dir).glob('*.rego') if not p.name.endswith('_test.rego'))
            self.assertTrue(rego, policy_dir)
            for name in rego:
                self.assertTrue((ROOT / policy_dir / name.replace('.rego', '_test.rego')).exists(), name)


if __name__ == '__main__':
    unittest.main()
