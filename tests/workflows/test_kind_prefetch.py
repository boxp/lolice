"""Exercise the image prefetch helpers with a recorded docker stub only (no cluster, no network)."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
PREFETCH = ROOT / 'tests/workflows/kind-prefetch-images.sh'
WORKFLOW = ROOT / '.github/workflows/chainsaw.yaml'

# docker ps は 2 ノードを返し、docker exec は呼び出しを記録して FAIL_IMAGE だけ失敗させる。
DOCKER_STUB = '''#!/bin/sh
if [ "$1" = ps ]; then
  printf 'chainsaw-test-control-plane\\tcontrol-plane\\nchainsaw-test-worker\\tworker\\n'
  exit 0
fi
printf '%s\\n' "$*" >> "$CALL_LOG"
case "$*" in *"$FAIL_IMAGE"*) echo "pull failed: 429 Too Many Requests" >&2; exit 1 ;; esac
'''


class NormalizeRefTest(unittest.TestCase):
    def normalize(self, ref):
        return subprocess.run(['bash', '-c', f'source "{PREFETCH}"; normalize_ref "$1"', '_', ref],
                              capture_output=True, text=True, check=True).stdout.strip()

    def test_short_names_are_fully_qualified_for_containerd(self):
        cases = {
            'python:3.13-alpine': 'docker.io/library/python:3.13-alpine',
            'grafana/loki:3.4.2': 'docker.io/grafana/loki:3.4.2',
            'docker.io/calico/node:v3.33.0': 'docker.io/calico/node:v3.33.0',
            'ghcr.io/dexidp/dex:v2.45.1': 'ghcr.io/dexidp/dex:v2.45.1',
            'public.ecr.aws/docker/library/redis:8.2.3-alpine': 'public.ecr.aws/docker/library/redis:8.2.3-alpine',
            'localhost:5000/app:dev': 'localhost:5000/app:dev',
            'docker.io/cloudflare/cloudflared:2026.10.1@sha256:' + '0' * 64:
                'docker.io/cloudflare/cloudflared:2026.10.1@sha256:' + '0' * 64,
        }
        for ref, expected in cases.items():
            with self.subTest(ref=ref):
                self.assertEqual(self.normalize(ref), expected)


class PrefetchTest(unittest.TestCase):
    def run_prefetch(self, images_tsv, fail_image='<none>'):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stub = root / 'docker'
            stub.write_text(DOCKER_STUB)
            stub.chmod(0o755)
            log = root / 'calls'
            log.write_text('')
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'],
                       CALL_LOG=str(log), FAIL_IMAGE=fail_image, PREFETCH_PARALLEL='2', PREFETCH_RETRY_SLEEP='0')
            result = subprocess.run(['bash', str(PREFETCH), 'chainsaw-test'], input=images_tsv,
                                    env=env, capture_output=True, text=True)
            return result, log.read_text().splitlines()

    def test_scope_selects_nodes_and_refs_are_qualified(self):
        result, calls = self.run_prefetch('grafana/alloy:v1.7.0\tall\npython:3.13-alpine\tworker\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(calls), sorted([
            'exec chainsaw-test-control-plane ctr -n k8s.io images pull --hosts-dir /etc/containerd/certs.d docker.io/grafana/alloy:v1.7.0',
            'exec chainsaw-test-worker ctr -n k8s.io images pull --hosts-dir /etc/containerd/certs.d docker.io/grafana/alloy:v1.7.0',
            'exec chainsaw-test-worker ctr -n k8s.io images pull --hosts-dir /etc/containerd/certs.d docker.io/library/python:3.13-alpine',
        ]))
        self.assertIn('ok=3 fail=0', result.stdout)

    def test_pull_failures_are_retried_and_never_fail_the_step(self):
        result, calls = self.run_prefetch('redis:8.2.3-alpine\tworker\ncurlimages/curl:8.14.1\tworker\n',
                                          fail_image='library/redis')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sum('library/redis' in c for c in calls), 3)
        self.assertEqual(sum('curlimages/curl' in c for c in calls), 1)
        self.assertIn('ok=1 fail=1', result.stdout)
        self.assertIn('429 Too Many Requests', result.stdout)

    def test_empty_input_is_a_noop(self):
        result, calls = self.run_prefetch('')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [])


class WorkflowWiringTest(unittest.TestCase):
    def test_kind_action_does_not_wait_for_cni_less_node_ready(self):
        # disableDefaultCNI の kind-config ではノードは Calico 導入まで Ready にならず、
        # kind-action の既定 wait (60s) は毎回タイムアウトするだけなので 0s にしておく。
        source = WORKFLOW.read_text()
        kind_step = source.split('        uses: helm/kind-action@v1\n', 1)[1].split('\n      - name:', 1)[0]
        self.assertIn('wait: 0s', kind_step)
        self.assertIn('kubectl wait --for=condition=Ready node --all', source)

    def test_prefetch_runs_in_background_and_is_best_effort(self):
        source = WORKFLOW.read_text()
        step = source.split('      - name: Build manifests and prefetch container images (background)\n', 1)[1].split('\n      - name:', 1)[0]
        self.assertIn('nohup bash -c', step)
        self.assertIn('kind-ci-images.sh', step)
        self.assertIn('kind-prefetch-images.sh', step)


if __name__ == '__main__':
    unittest.main()
