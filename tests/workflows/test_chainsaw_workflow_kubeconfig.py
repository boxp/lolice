"""Keep workflow preparation pinned to the exported isolated kind kubeconfig."""
from pathlib import Path
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


if __name__ == '__main__':
    unittest.main()
