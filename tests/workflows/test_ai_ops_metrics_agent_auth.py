# BOXP-204: agent CLI 認証期限の exposition を ai-ops-metrics が配信する（tests/workflows の unittest step で実行）。
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

path = Path(__file__).resolve().parents[2] / 'argoproj/codex-workspace/ai-ops-metrics/server.py'
spec = importlib.util.spec_from_file_location('ai_ops_metrics_agent_auth', path)
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class AgentAuthTest(unittest.TestCase):
    def get(self, auth_path):
        handler = object.__new__(server.Handler)
        handler.path = '/metrics'
        with patch.object(server, 'PATH', '/nonexistent/metrics.prom'), patch.object(server, 'PREPAID_DIR', ''), \
                patch.object(server, 'SUBSCRIPTION_PATH', ''), patch.object(server, 'TICKETS_DIR', ''), \
                patch.object(server, 'AGENT_AUTH_PATH', auth_path), patch.object(handler, '_send') as send:
            handler.do_GET()
        return send.call_args.args[1].decode()

    def test_served_with_file_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            prom = Path(tmp) / 'agent-auth.prom'
            prom.write_text('ai_agent_auth_relogin_deadline_known{provider="codex"} 0')
            out = self.get(str(prom))
        self.assertIn('ai_agent_auth_relogin_deadline_known{provider="codex"} 0\n', out)
        self.assertIn('ai_agent_auth_metrics_file_present 1\n', out)

    def test_missing_file_is_reported_not_hidden(self):
        out = self.get('/nonexistent/agent-auth.prom')
        self.assertIn('ai_agent_auth_metrics_file_present 0\n', out)
        self.assertIn('ai_agent_auth_metrics_file_age_seconds -1\n', out)

    def test_disabled(self):
        self.assertNotIn('ai_agent_auth_metrics', self.get(''))


if __name__ == '__main__':
    unittest.main()
