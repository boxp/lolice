import pathlib
import subprocess
import sys
import unittest


class NotifyRegressionTest(unittest.TestCase):
    def test_regression_checks(self):
        check = pathlib.Path(__file__).resolve().with_name("boxp_notify_check.py")
        result = subprocess.run([sys.executable, "-B", str(check)], capture_output=True, text=True)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertFalse(any(line.startswith("FAIL ") for line in output.splitlines()), output)
        self.assertGreaterEqual(sum(line.startswith("ok") for line in output.splitlines()), 90, output)
