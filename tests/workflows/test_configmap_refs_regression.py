"""実際の kustomize render で生成 ConfigMap の参照回帰を検証する。"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class ConfigMapRegressionTest(unittest.TestCase):
    def test_render_before_and_after_fix(self):
        missing = [tool for tool in ('kustomize', 'conftest') if not shutil.which(tool)]
        if missing:
            if os.environ.get('CI'):
                self.fail('必要なツールがありません: ' + ', '.join(missing))
            self.skipTest('必要なツールがありません: ' + ', '.join(missing))
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'workspace'
            shutil.copytree(ROOT / 'argoproj/codex-workspace', target)
            manifest = Path(tmp) / 'render.yaml'
            for broken in (False, True):
                if broken:
                    path = target / 'kustomization.yaml'
                    text = path.read_text()
                    self.assertIn('namespace: codex-workspace\n', text)
                    path.write_text(text.replace('namespace: codex-workspace\n', '', 1))
                manifest.write_bytes(subprocess.check_output(['kustomize', 'build', str(target)]))
                result = subprocess.run(['conftest', 'test', '--no-color', '--combine', '-p', str(ROOT / 'policy/references'), str(manifest)], capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, not broken, result.stdout + result.stderr)
                if broken:
                    self.assertIn("configMapGenerator の生成名", result.stdout)
