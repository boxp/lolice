"""Boundary checks on the SD switch manifests."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2] / 'argoproj'


def text(name):
    return (ROOT / name).read_text()


class BoundaryTest(unittest.TestCase):
    def test_gateway_has_no_identity_or_gpu(self):
        gateway = text('stable-diffusion/sd-gateway-deployment.yaml')
        self.assertIn('automountServiceAccountToken: false', gateway)
        self.assertNotIn('gpu.intel.com', gateway)
        self.assertNotIn('serviceAccountName', gateway)

    def test_gateway_sees_only_the_shared_directory(self):
        paths = re.findall(r'path: (/var/lib/\S+)', text('stable-diffusion/sd-gateway-deployment.yaml'))
        self.assertEqual(paths, ['/var/lib/local-llm/models/boxp340-qwen-image-21/work/runtime-api/sd-switch'])

    def test_switcher_container_has_no_token(self):
        worker = text('local-llm/runtime-worker.yaml')
        container = worker[worker.index('- name: sd-switch'):worker.index('      volumes:')]
        self.assertNotIn('identity', container)
        self.assertNotIn('serviceAccount', container)

    def test_transport_allowlist_is_exact(self):
        source = text('local-llm/runtime_api_worker.py')
        targets = re.findall(r"^    '(/api[^']+)':", source, re.M)
        self.assertEqual(sorted(targets), sorted([
            '/apis/apps/v1/namespaces/local-llm/deployments/llama-server/scale',
            '/apis/apps/v1/namespaces/stable-diffusion/deployments/qwen-image-eval/scale',
            '/apis/apps/v1/namespaces/stable-diffusion/deployments/stable-diffusion-webui/scale',
            '/api/v1/namespaces/local-llm/configmaps/llama-server-admission']))

    def test_switcher_never_holds_a_credential(self):
        source = text('local-llm/runtime_switch_worker.py')
        for forbidden in ('runtime-identity', 'Authorization', 'kubernetes.default'):
            self.assertNotIn(forbidden, source)

    def test_rbac_rules_stay_named(self):
        for name in ('local-llm/runtime-authorization.yaml', 'stable-diffusion/runtime-authorization.yaml'):
            rules = text(name).count('- apiGroups:')
            self.assertEqual(text(name).count('resourceNames:'), rules, name)
            self.assertNotIn('"*"', text(name))

    def test_backend_is_not_reachable_around_the_gateway(self):
        policy = text('stable-diffusion/networkpolicy.yaml')
        ingress = policy[policy.index('ingress:'):policy.index('egress:')]
        self.assertNotIn('nets:', ingress)
        self.assertNotIn("selector: app == 'cloudflared'", ingress)
        self.assertIn("selector: app == 'sd-admission-gateway'", ingress)

    def test_tunnel_target_service_selects_the_gateway(self):
        front = text('stable-diffusion/service.yaml').split('---')[0]
        self.assertIn('name: stable-diffusion-webui\n', front)
        self.assertIn('app: sd-admission-gateway', front)


if __name__ == '__main__':
    unittest.main()
