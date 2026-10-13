"""Run the workflow shell with real yq and isolated, recorded cluster commands.

Requires Mike Farah yq v4 on PATH, as the workflow's preparation steps do.
No Kubernetes cluster or network access is used by these tests.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[2]
PIN = '0.18.2'
DIGEST = '5d0645b815837af4ce1fd0436879e4adfe8230072103c65429c4a41d9e00f43d'


def workflow_script(workflow, name):
    source = (ROOT / '.github/workflows' / workflow).read_text()
    step = source.split(f'      - name: {name}\n', 1)[1].split('\n      - name:', 1)[0]
    return textwrap.dedent(step.split('        run: |\n', 1)[1])


# Cluster and network effects are replaced, while version selection and guards
# execute verbatim from the workflow. Digest rejection uses real sha256sum.
COMMAND_STUB = '''#!/usr/bin/env python3
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['COMMAND_LOG'], 'a') as log:
    log.write(json.dumps([name, *args]) + '\\n')
if name == 'helm' and args[0] == 'pull':
    version = args[args.index('--version') + 1]
    destination = pathlib.Path(args[args.index('--destination') + 1])
    (destination / ('external-secrets-' + version + '.tgz')).write_bytes(b'tampered archive')
elif name == 'kubectl' and args[0] == 'apply':
    sys.stdin.read()
elif name == 'curl':
    content = '{"object":{"sha":"e258ee23c3e52266d407572f4bcdfe7d9ed36cb5"}}' if '/git/ref/tags/' in ' '.join(args) else 'kind: CustomResourceDefinition\\n'
    if '-o' in args:
        pathlib.Path(args[args.index('-o') + 1]).write_text(content)
    else:
        print(content)
elif name == 'kustomize':
    print('kind: ConfigMap')
'''

# Model a verified archive for the successful preparation path. Assert the
# fixed expected digest reaches the checker; do not bypass a changed pin.
VERIFIED_DIGEST_STUB = '''#!/usr/bin/env python3
import json, os, sys
with open(os.environ['COMMAND_LOG'], 'a') as log:
    log.write(json.dumps(['sha256sum', *sys.argv[1:]]) + '\\n')
if '--check' in sys.argv:
    expected = sys.stdin.read().split()
    if len(expected) != 2 or expected[0] != os.environ['VERIFIED_DIGEST']:
        sys.exit(1)
else:
    print(os.environ['VERIFIED_DIGEST'], ' ', sys.argv[1])
'''


class EsoVersionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.yq = shutil.which('yq')
        if cls.yq is None:
            raise RuntimeError('Mike Farah yq v4 is required on PATH; version selection must not be skipped')
        version = subprocess.run([cls.yq, '--version'], capture_output=True, text=True, check=True)
        if 'version v4.' not in version.stdout:
            raise RuntimeError('These workflows require Mike Farah yq v4')

    def run_preparation(self, revision, component='external-secrets-operator',
                        preflight=False, chart_first=False, verified_digest=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = root / 'argoproj/external-secrets-operator/application.yaml'
            application.parent.mkdir(parents=True)
            sources = [
                {'repoURL': 'https://example.invalid/manifests.git', 'targetRevision': 'main', 'ref': 'values'},
                {'repoURL': 'https://charts.external-secrets.io', 'chart': 'external-secrets', 'targetRevision': revision},
                {'repoURL': 'https://example.invalid/other-chart', 'chart': 'other', 'targetRevision': '9.9.9'},
            ]
            if chart_first:
                sources = [sources[1], sources[0], sources[2]]
            application.write_text(json.dumps({'spec': {'sources': sources}}))
            binary_dir = root / 'bin'
            binary_dir.mkdir()
            for command in ['helm', 'kubectl', 'curl', 'kustomize']:
                executable = binary_dir / command
                executable.write_text(COMMAND_STUB)
                executable.chmod(0o755)
            if verified_digest:
                executable = binary_dir / 'sha256sum'
                executable.write_text(VERIFIED_DIGEST_STUB)
                executable.chmod(0o755)
            (root / 'evidence').mkdir()
            log = root / 'commands.jsonl'
            env = dict(os.environ, PATH=str(binary_dir) + ':' + os.environ['PATH'],
                       REPO_ROOT=str(root), RUNNER_TEMP=str(root), COMMAND_LOG=str(log),
                       CHAINSAW_KIND_KUBECONFIG=str(root / 'isolated.kubeconfig'),
                       COMPONENT=component, COMPONENTS=json.dumps([component]), VERIFIED_DIGEST=DIGEST)
            if preflight:
                script = workflow_script('chainsaw-preflight.yaml', 'Prepare selected component in isolated kind')
            else:
                script = workflow_script('chainsaw.yaml', 'Apply component manifests to kind cluster')
            result = subprocess.run(['bash', '-c', script], cwd=root, env=env, capture_output=True, text=True)
            commands = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
            expected_file = root / 'evidence/eso-chart-expected.sha256'
            evidence = expected_file.read_text() if expected_file.exists() else None
            return result, commands, evidence

    def test_normal_ci_uses_chart_revision_from_multiple_sources(self):
        for component in ['external-secrets-operator', 'argocd']:
            for chart_first in [False, True]:
                with self.subTest(component=component, chart_first=chart_first):
                    result, commands, _ = self.run_preparation('0.20.4', component, chart_first=chart_first)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    install = next(command for command in commands if command[:2] == ['helm', 'install'])
                    self.assertEqual(install[install.index('--version') + 1], '0.20.4')

    def test_normal_ci_rejects_nonfixed_chart_versions_before_fetch(self):
        for revision in ['main', '^0.20.4', '2.*', '0.20', 'v0.20.4', '0.20.4-rc.1', '']:
            with self.subTest(revision=revision):
                result, commands, _ = self.run_preparation(revision)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('must be a fixed chart version', result.stderr)
                self.assertEqual(commands, [])

    def test_preflight_rejects_version_mismatch_before_any_fetch(self):
        for component in ['external-secrets-operator', 'argocd']:
            with self.subTest(component=component):
                result, commands, evidence = self.run_preparation('0.20.4', component, preflight=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('differs from the immutable preflight pin', result.stderr)
                self.assertEqual(commands, [])
                self.assertIsNone(evidence)

    def test_matching_preflight_version_still_rejects_tampered_archive(self):
        result, commands, evidence = self.run_preparation(PIN, preflight=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(any(command[:2] == ['helm', 'pull'] for command in commands))
        self.assertFalse(any(command[:2] == ['helm', 'install'] for command in commands))
        self.assertFalse(any(command[0] == 'curl' for command in commands))
        self.assertEqual(evidence.split()[0], DIGEST)

    def test_matching_preflight_version_verifies_digest_before_install(self):
        for component in ['external-secrets-operator', 'argocd']:
            with self.subTest(component=component):
                result, commands, evidence = self.run_preparation(PIN, component, preflight=True, verified_digest=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                check = commands.index(['sha256sum', '--check', '--status'])
                install = next(i for i, command in enumerate(commands) if command[:2] == ['helm', 'install'])
                self.assertLess(check, install)
                self.assertEqual(evidence.split()[0], DIGEST)
                pull = next(command for command in commands if command[:2] == ['helm', 'pull'])
                self.assertEqual(pull[pull.index('--version') + 1], PIN)


if __name__ == '__main__':
    unittest.main()
