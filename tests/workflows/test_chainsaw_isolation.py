"""Exercise the real suite guard using local kubectl stubs only."""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
GUARD = ROOT / 'tests/workflows/require-isolated-kind.sh'
CHAINSAW = ROOT / 'tests/chainsaw'
# suites that never talk to a cluster and therefore need no isolation guard.
NO_CLUSTER_SUITES = {'kohako-studio'}
LOOPBACK_SUITES = {'argocd', 'codex-workspace/task-board-langfuse', 'grafana-alloy'}
MUTATING_HELPERS = ['tests/chainsaw/argocd/network-policy-impact.sh', 'tests/chainsaw/grafana-alloy/log-pipeline.sh']

STUB = '''#!/bin/sh
case "$*" in
  *current-context*) printf called >> "$CALL_LOG"; printf '%s\\n' "$CONFIG_CONTEXT" ;;
  *--kubeconfig*) printf called >> "$CALL_LOG"; printf '%s\\n' "$KIND_SERVER" ;;
  *config*view*) printf called >> "$CALL_LOG"; printf '%s\\n' "$ACTIVE_SERVER" ;;
  *) printf called >> "$CALL_LOG"; printf mutation >> "$MUTATION_LOG" ;;
esac
'''


class GuardHarness:
    def __init__(self, directory):
        self.root = Path(directory)
        self.config = self.root / 'kind-config'
        self.config.write_text('stub only')
        stub = self.root / 'kubectl'
        stub.write_text(STUB)
        stub.chmod(0o755)
        self.calls = self.root / 'calls'
        self.mutations = self.root / 'mutations'

    def env(self, expected, actual, kind_server, active_server, **extra):
        self.calls.write_text('')
        self.mutations.write_text('')
        return dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ['PATH'],
                    CHAINSAW_KIND_CONTEXT=expected, CHAINSAW_KIND_KUBECONFIG=str(self.config),
                    CONFIG_CONTEXT=actual, KIND_SERVER=kind_server, ACTIVE_SERVER=active_server,
                    CALL_LOG=str(self.calls), MUTATION_LOG=str(self.mutations),
                    REPO_ROOT=str(ROOT), **extra)


def suites():
    for test in CHAINSAW.rglob('chainsaw-test.yaml'):
        yield test.parent.relative_to(CHAINSAW).as_posix(), test


class IsolationGuardTest(unittest.TestCase):
    def test_every_cluster_suite_runs_guard_first(self):
        seen = set()
        for suite, test in suites():
            seen.add(suite)
            if suite in NO_CLUSTER_SUITES:
                continue
            with self.subTest(suite=suite):
                steps = re.findall(r'^  - name: (\S+)$', test.read_text(), re.M)
                self.assertEqual(steps[0], 'require-isolated-kind')
                first = test.read_text().split('  - name: ', 2)[1]
                option = ' --loopback' if suite in LOOPBACK_SUITES else ''
                expected = f"""content: '"${{REPO_ROOT}}/tests/workflows/require-isolated-kind.sh"{option}'\n"""
                self.assertIn(expected, first)
        self.assertIn('hermes-agent/alertmanager-relay', seen)
        self.assertTrue(NO_CLUSTER_SUITES <= seen)

    def test_no_cluster_suites_do_not_use_kubectl(self):
        for suite in NO_CLUSTER_SUITES:
            for path in (CHAINSAW / suite).rglob('*'):
                if path.is_file():
                    with self.subTest(path=str(path)):
                        self.assertNotIn('kubectl', path.read_text())

    def test_guard_fails_closed_before_any_cluster_call(self):
        # Chainsaw may rewrite current-context; bind to the server in the
        # independently exported and validated kind kubeconfig.
        cases = [('', 'kind-test', 'https://kind', 'https://kind', False, False),
                 ('in-cluster', 'in-cluster', 'https://prod', 'https://prod', False, False),
                 ('kind-test', 'in-cluster', 'https://prod', 'https://prod', False, True),
                 ('kind-test', 'kind-test', 'https://kind', 'https://prod', False, True),
                 ('kind-test', 'kind-test', '', '', False, True),
                 ('kind-test', 'kind-test', 'https://kind', 'https://kind', True, True)]
        with tempfile.TemporaryDirectory() as directory:
            harness = GuardHarness(directory)
            for expected, actual, kind_server, active_server, success, called in cases:
                with self.subTest(expected=expected, actual=actual, active=active_server):
                    result = subprocess.run(['bash', str(GUARD)], capture_output=True,
                                            env=harness.env(expected, actual, kind_server, active_server))
                    self.assertEqual(result.returncode == 0, success, result.stderr)
                    self.assertEqual(bool(harness.calls.read_text()), called)
                    self.assertEqual(harness.mutations.read_text(), '')

    def test_loopback_guard_requires_local_api_server(self):
        cases = [('kind-test', 'kind-test', 'https://prod', 'https://prod', False),
                 ('kind-test', 'kind-test', 'https://127.0.0.1:1234', 'https://127.0.0.1:1234', True),
                 ('kind-test', 'kind-test', 'https://localhost:1234', 'https://localhost:1234', True)]
        with tempfile.TemporaryDirectory() as directory:
            harness = GuardHarness(directory)
            for expected, actual, kind_server, active_server, success in cases:
                with self.subTest(server=active_server):
                    result = subprocess.run(['bash', str(GUARD), '--loopback'], capture_output=True,
                                            env=harness.env(expected, actual, kind_server, active_server))
                    self.assertEqual(result.returncode == 0, success, result.stderr)

    def test_unknown_guard_option_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = GuardHarness(directory)
            result = subprocess.run(['bash', str(GUARD), '--bogus'], capture_output=True,
                                    env=harness.env('kind-test', 'kind-test', 'https://kind', 'https://kind'))
            self.assertEqual(result.returncode, 2)
            self.assertEqual(harness.calls.read_text(), '')

    def test_mutating_helpers_reject_unsafe_context_before_mutation(self):
        unsafe = [('', 'kind-test', 'https://127.0.0.1:1234', 'https://127.0.0.1:1234'),
                  ('in-cluster', 'in-cluster', 'https://prod', 'https://prod'),
                  ('kind-test', 'in-cluster', 'https://prod', 'https://prod'),
                  ('kind-test', 'kind-test', 'https://prod', 'https://prod'),
                  ('kind-test', 'kind-test', 'https://127.0.0.1:1234', 'https://prod')]
        for helper in MUTATING_HELPERS:
            with tempfile.TemporaryDirectory() as directory:
                harness = GuardHarness(directory)
                for expected, actual, kind_server, active_server in unsafe:
                    with self.subTest(helper=helper, context=expected, server=active_server):
                        env = harness.env(expected, actual, kind_server, active_server,
                                          CHAINSAW_NETWORK_POLICY_TEST='true')
                        result = subprocess.run(['bash', str(ROOT / helper), 'section-not-reached'], capture_output=True, env=env)
                        self.assertNotEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(harness.mutations.read_text(), '')

    def test_matrix_is_skipped_only_without_network_policy_support(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = GuardHarness(directory)
            env = harness.env('', '', '', '', CHAINSAW_NETWORK_POLICY_TEST='false')
            result = subprocess.run(['bash', str(ROOT / MUTATING_HELPERS[0]), 'create_probes'], capture_output=True, env=env, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('NetworkPolicy通信matrix未実施', result.stdout)
            self.assertEqual(harness.calls.read_text(), '')


if __name__ == '__main__':
    unittest.main()
