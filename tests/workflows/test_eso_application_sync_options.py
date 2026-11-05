"""Guard the External Secrets Application against the CRD annotation limit.

ESO chart 0.20.4 and later ship ``secretstores`` / ``clustersecretstores``
CRDs larger than 256 KiB. Argo CD's default client-side apply stores the whole
manifest in ``kubectl.kubernetes.io/last-applied-configuration`` and fails with
``metadata.annotations: Too long: may not be more than 262144 bytes``.
Upstream requires Server-Side Apply (BOXP-256). This test fails closed when the
Application drops that sync option, which no kind-based chart install detects
because Helm never writes the last-applied annotation.

Requires Mike Farah yq v4 on PATH, as the workflow's preparation steps do.
"""
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[2]
APPLICATION = ROOT / 'argoproj/external-secrets-operator/application.yaml'
# Kubernetes rejects annotations whose total size exceeds this many bytes.
ANNOTATION_LIMIT = 262144
# First chart version whose SecretStore CRDs exceed the limit (294,659 B).
FIRST_OVERSIZED_CHART = (0, 20, 4)


def yq(expression):
    binary = shutil.which('yq')
    if binary is None:
        raise RuntimeError('Mike Farah yq v4 is required on PATH; this guard must not be skipped')
    result = subprocess.run([binary, '-r', expression, str(APPLICATION)],
                            capture_output=True, text=True, check=True)
    return result.stdout.strip()


class EsoApplicationSyncOptionsTest(unittest.TestCase):
    def test_chart_version_is_fixed(self):
        version = yq('.spec.sources[] | select(.chart == "external-secrets") | .targetRevision')
        self.assertRegex(version, r'^\d+\.\d+\.\d+$')

    def test_oversized_crd_chart_uses_server_side_apply(self):
        version = yq('.spec.sources[] | select(.chart == "external-secrets") | .targetRevision')
        parsed = tuple(int(part) for part in version.split('.'))
        if parsed < FIRST_OVERSIZED_CHART:
            self.skipTest(f'chart {version} predates the oversized CRDs')
        options = yq('.spec.syncPolicy.syncOptions // [] | .[]').splitlines()
        self.assertIn('ServerSideApply=true', options,
                      f'ESO chart {version} ships CRDs over {ANNOTATION_LIMIT} bytes; '
                      'Argo CD must apply them with ServerSideApply=true')
