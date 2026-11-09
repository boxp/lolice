"""Verify the detect job compares the merge commit with its original base."""
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / '.github/workflows/chainsaw.yaml'


class DetectDiffTest(unittest.TestCase):
    def test_merge_base_and_non_merge_head(self):
        step = WORKFLOW.read_text().split('      - name: Detect changed components with chainsaw tests\n', 1)[1]
        snippet = step.split('        run: |\n', 1)[1].split('          echo "Changed files:"', 1)[0]
        snippet = textwrap.dedent(snippet)
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)

            def git(*args):
                return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()

            git('init', '-b', 'main')
            git('config', 'user.name', 'Test')
            git('config', 'user.email', 'test@example.com')
            (root / 'base.txt').write_text('base\n')
            git('add', 'base.txt')
            git('commit', '-m', 'base')
            git('checkout', '-b', 'pr')
            (root / 'pr.txt').write_text('pr\n')
            git('add', 'pr.txt')
            git('commit', '-m', 'pr')
            git('checkout', 'main')
            git('merge', '--no-ff', 'pr', '-m', 'merge')
            merge = git('rev-parse', 'HEAD')
            git('checkout', '-b', 'advanced-main', 'HEAD^1')
            (root / 'main-only.txt').write_text('main\n')
            git('add', 'main-only.txt')
            git('commit', '-m', 'advance main')
            git('update-ref', 'refs/remotes/origin/main', 'HEAD')
            output = root / 'changed_files.txt'
            script = snippet.replace('/tmp/changed_files.txt', str(output))
            result = subprocess.run(['bash', '-e', '-c', script], cwd=root, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('HEAD is not a merge commit', result.stderr)
            git('checkout', '--detach', merge)
            result = subprocess.run(['bash', '-e', '-c', script], cwd=root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(output.read_text().splitlines(), ['pr.txt'])


if __name__ == '__main__':
    unittest.main()
