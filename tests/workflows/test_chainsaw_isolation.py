"""Exercise the real suite guard using local kubectl stubs only."""
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[2]


class IsolationGuardTest(unittest.TestCase):
    def test_guard_on_every_mutating_suite(self):
        for component in ['argocd', 'external-secrets-operator', 'kube-vip']:
            source = (ROOT / 'tests/chainsaw' / component / 'chainsaw-test.yaml').read_text()
            guard = textwrap.dedent(source.split('        content: |\n', 1)[1].split('\n  - name:', 1)[0])
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = root / 'kind-config'
                config.write_text('stub only')
                stub = root / 'kubectl'
                stub.write_text('''#!/bin/sh
printf called >> "$CALL_LOG"
case "$*" in
  *current-context*) printf '%s\\n' "$CONFIG_CONTEXT" ;;
  *--kubeconfig*) printf '%s\\n' "$KIND_SERVER" ;;
  *) printf '%s\\n' "$ACTIVE_SERVER" ;;
esac
''')
                stub.chmod(0o755)
                # Chainsaw may rewrite current-context; bind to the server in
                # the independently exported and validated kind kubeconfig.
                cases = [('', 'kind-test', 'https://kind', 'https://kind', False, False),
                         ('in-cluster', 'in-cluster', 'https://prod', 'https://prod', False, False),
                         ('kind-test', 'in-cluster', 'https://prod', 'https://prod', False, True),
                         ('kind-test', 'kind-test', 'https://kind', 'https://prod', False, True),
                         ('kind-test', 'kind-test', '', '', False, True),
                         ('kind-test', 'kind-test', 'https://kind', 'https://kind', True, True)]
                for expected, actual, kind_server, active_server, success, called in cases:
                    with self.subTest(component=component, expected=expected, server=active_server):
                        log = root / 'calls'
                        log.write_text('')
                        env = dict(os.environ, PATH=directory + os.pathsep + os.environ['PATH'],
                                   CHAINSAW_KIND_CONTEXT=expected, CHAINSAW_KIND_KUBECONFIG=str(config),
                                   CONFIG_CONTEXT=actual, KIND_SERVER=kind_server,
                                   ACTIVE_SERVER=active_server, CALL_LOG=str(log))
                        result = subprocess.run(['sh', '-c', guard], env=env, capture_output=True)
                        self.assertEqual(result.returncode == 0, success, result.stderr)
                        self.assertEqual(bool(log.read_text()), called)

    def test_network_matrix_guard_rejects_unsafe_context_before_mutation(self):
        source = (ROOT / 'tests/chainsaw/argocd/network-policy-impact.sh').read_text()
        guard = source.split('kubectl create namespace', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'kind-config'
            config.write_text('stub only')
            stub = root / 'kubectl'
            stub.write_text('''#!/bin/sh
case "$*" in
  *current-context*) printf '%s\\n' "$CONFIG_CONTEXT" ;;
  *--kubeconfig*) printf '%s\\n' "$KIND_SERVER" ;;
  *config*view*) printf '%s\\n' "$ACTIVE_SERVER" ;;
  *) printf mutation >> "$CALL_LOG" ;;
esac
''')
            stub.chmod(0o755)
            cases = [('', 'kind-test', 'https://127.0.0.1:1234', 'https://127.0.0.1:1234', False),
                     ('in-cluster', 'in-cluster', 'https://prod', 'https://prod', False),
                     ('kind-test', 'in-cluster', 'https://prod', 'https://prod', False),
                     ('kind-test', 'kind-test', 'https://prod', 'https://prod', False),
                     ('kind-test', 'kind-test', 'https://127.0.0.1:1234', 'https://prod', False),
                     ('kind-test', 'kind-test', 'https://127.0.0.1:1234', 'https://127.0.0.1:1234', True)]
            for expected, actual, kind_server, active_server, success in cases:
                with self.subTest(context=expected, server=active_server):
                    log = root / 'calls'
                    log.write_text('')
                    env = dict(os.environ, PATH=directory + os.pathsep + os.environ['PATH'],
                               CHAINSAW_KIND_CONTEXT=expected, CHAINSAW_KIND_KUBECONFIG=str(config),
                               CHAINSAW_NETWORK_POLICY_TEST="true",
                               CONFIG_CONTEXT=actual, KIND_SERVER=kind_server,
                               ACTIVE_SERVER=active_server, CALL_LOG=str(log))
                    result = subprocess.run(['bash', '-c', guard], env=env, capture_output=True)
                    self.assertEqual(result.returncode == 0, success, result.stderr)
                    self.assertEqual(log.read_text(), '')


if __name__ == '__main__':
    unittest.main()
