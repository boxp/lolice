"""Boundary checks on the ComfyUI manifests (BOXP-498)."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2] / 'argoproj'


def text(name):
    return (ROOT / name).read_text()


class ComfyBoundaryTest(unittest.TestCase):
    def test_gateway_has_no_identity_or_gpu(self):
        gateway = text('stable-diffusion/comfyui-gateway-deployment.yaml')
        self.assertIn('automountServiceAccountToken: false', gateway)
        self.assertNotIn('gpu.intel.com', gateway)
        self.assertNotIn('serviceAccountName', gateway)
        self.assertNotIn('secret', gateway.lower())

    def test_gateway_sees_only_its_shared_directory(self):
        paths = re.findall(r'path: (/var/lib/\S+)', text('stable-diffusion/comfyui-gateway-deployment.yaml'))
        self.assertEqual(paths, ['/var/lib/local-llm/models/boxp340-qwen-image-21/work/runtime-api/comfy-switch'])

    def test_backend_has_no_identity_secret_or_host_path(self):
        backend = text('stable-diffusion/comfyui-deployment.yaml')
        self.assertIn('automountServiceAccountToken: false', backend)
        self.assertIn('enableServiceLinks: false', backend)
        self.assertIn('runAsNonRoot: true', backend)
        self.assertIn('allowPrivilegeEscalation: false', backend)
        for forbidden in ('serviceAccountName', 'secretKeyRef', 'secretRef', 'envFrom', 'hostPath', 'hostNetwork', 'privileged'):
            self.assertNotIn(forbidden, backend)

    def test_backend_resolves_public_host_locally_and_fails_dns_fast(self):
        backend = text('stable-diffusion/comfyui-deployment.yaml')
        for ip in ('127.0.0.1', '"::1"'):
            self.assertIn(f'- ip: {ip}\n          hostnames: [comfyui.b0xp.io]', backend)
        self.assertIn('dnsPolicy: "None"\n      dnsConfig:\n        nameservers: ["127.0.0.1"]', backend)

    def test_backend_caps_vram_below_the_shared_memory_limit(self):
        backend = text('stable-diffusion/comfyui-deployment.yaml')
        self.assertIn('args: ["--reserve-vram", "48"]', backend)
        self.assertIn('memory: 72Gi', backend)

    def test_backend_mounts_only_its_own_directory_of_the_shared_volume(self):
        backend = text('stable-diffusion/comfyui-deployment.yaml')
        self.assertEqual(backend.count('claimName:'), 1)
        self.assertRegex(backend, r'- name: data\n\s+mountPath: /data\n\s+subPath: comfyui\n')

    def test_backend_starts_stopped_and_is_exclusive_on_the_gpu(self):
        backend = text('stable-diffusion/comfyui-deployment.yaml')
        self.assertIn('replicas: 0', backend)
        self.assertIn('type: Recreate', backend)
        self.assertEqual(backend.count('gpu.intel.com/i915: "1"'), 2)
        self.assertIn('priorityClassName: gpu-workload-high', backend)

    def test_backend_has_no_egress_and_no_way_around_the_gateway(self):
        policy = text('stable-diffusion/comfyui-networkpolicy.yaml').split('---')[0]
        self.assertIn("selector: app == 'comfyui'", policy)
        self.assertIn('- Egress', policy)
        self.assertNotIn('egress:', policy)
        self.assertNotIn('nets:', policy)
        self.assertNotIn("app == 'cloudflared'", policy)
        self.assertEqual(policy.count('- action: Allow'), 2)
        self.assertIn("selector: app == 'comfyui-gateway'", policy)
        self.assertIn("selector: app == 'qwen-runtime-controller'", policy)

    def test_gateway_accepts_only_the_tunnel(self):
        policy = text('stable-diffusion/comfyui-networkpolicy.yaml').split('---')[1]
        ingress = policy[policy.index('ingress:'):policy.index('egress:')]
        self.assertNotIn('nets:', ingress)
        self.assertEqual(ingress.count('- action: Allow'), 1)
        self.assertIn("selector: app == 'cloudflared'", ingress)

    def test_front_service_selects_the_gateway(self):
        front, back = text('stable-diffusion/comfyui-service.yaml').split('---')
        self.assertIn('name: comfyui\n', front)
        self.assertIn('app: comfyui-gateway', front)
        self.assertIn('name: comfyui-backend\n', back)

    def test_switcher_may_scale_only_the_named_deployment(self):
        role = text('stable-diffusion/runtime-authorization.yaml')
        self.assertIn('resourceNames: [comfyui]', role)
        self.assertEqual(role.count('resources: [deployments/scale]'), role.count('- apiGroups:'))
        self.assertEqual(set(re.findall(r'verbs: \[([^\]]+)\]', role)), {'get, patch'})

    def test_argo_leaves_only_the_replica_count_to_the_switcher(self):
        application = text('stable-diffusion/application.yaml')
        self.assertEqual(application.count('jsonPointers: [/spec/replicas]'), application.count('jsonPointers:'))
        self.assertIn('name: comfyui\n', application)
        self.assertIn('selfHeal: true', application)

    def test_sd_manifests_are_untouched_by_name(self):
        kustomization = text('stable-diffusion/kustomization.yaml')
        for name in ('deployment.yaml', 'service.yaml', 'networkpolicy.yaml', 'sd-gateway-deployment.yaml',
                     'sd-gateway-envoy-config.yaml', 'sd-gateway-networkpolicy.yaml'):
            self.assertIn('  - ' + name + '\n', kustomization)


if __name__ == '__main__':
    unittest.main()
