"""Keep workflow preparation pinned to the exported isolated kind kubeconfig."""
from pathlib import Path
import os
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[2]
KUBECONFIG_EXPORT = 'export KUBECONFIG="${CHAINSAW_KIND_KUBECONFIG:?An isolated kubeconfig is required}"'


def step_script(workflow, name):
    source = (ROOT / '.github/workflows' / workflow).read_text()
    marker = f'      - name: {name}\n'
    return source.split(marker, 1)[1].split('\n      - name:', 1)[0]


class WorkflowKubeconfigTest(unittest.TestCase):
    def test_preflight_checks_and_preparation_use_exported_kubeconfig(self):
        for step in ['Verify Kubernetes version and record tools', 'Prepare selected component in isolated kind']:
            with self.subTest(step=step):
                script = step_script('chainsaw-preflight.yaml', step)
                self.assertIn(KUBECONFIG_EXPORT, script)
                self.assertLess(script.index(KUBECONFIG_EXPORT), script.index('kubectl'))

    def test_pr_preparation_uses_exported_kubeconfig_before_helm_or_kubectl(self):
        script = step_script('chainsaw.yaml', 'Apply component manifests to kind cluster')
        self.assertIn(KUBECONFIG_EXPORT, script)
        self.assertLess(script.index(KUBECONFIG_EXPORT), script.index('helm repo add'))
        self.assertLess(script.index(KUBECONFIG_EXPORT), script.index('kubectl create namespace'))

    def test_preflight_rejects_wrong_download_before_install(self):
        step = step_script('chainsaw-preflight.yaml', 'Install kustomize and kubectl')
        script = textwrap.dedent(step.split('        run: |\n', 1)[1])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'evidence').mkdir()
            curl = root / 'curl'
            curl.write_text('#!/bin/sh\nprintf invalid-download > kustomize.tar.gz\n')
            curl.chmod(0o755)
            sudo = root / 'sudo'
            sudo.write_text('#!/bin/sh\ntouch "$RUNNER_TEMP/install-attempted"\n')
            sudo.chmod(0o755)
            env = dict(os.environ, PATH=str(root) + ':' + os.environ['PATH'], RUNNER_TEMP=directory)
            result = subprocess.run(['bash', '-c', script], cwd=root, env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / 'install-attempted').exists())
            self.assertTrue((root / 'evidence/kustomize-expected.sha256').exists())


if __name__ == '__main__':
    unittest.main()
