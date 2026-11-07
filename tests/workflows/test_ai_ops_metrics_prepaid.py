# BOXP-463: tests/workflows の既存 unittest step で実行される（workflow 変更なしで CI に載せるためここに置く）。
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

path = Path(__file__).resolve().parents[2] / 'argoproj/codex-workspace/ai-ops-metrics/server.py'
spec = importlib.util.spec_from_file_location('ai_ops_metrics', path)
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class PrepaidTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.baseline = dict(schema='gemini-prepaid-baseline/v1', amount=10, currency='USD', observed_at='2026-01-01T00:00:00Z', source='ai_studio_ui_owner')

    def write_baseline(self):
        (self.root / 'baseline.json').write_text(json.dumps(self.baseline))

    def event(self, ts, cost, **kw):
        return dict(ts=ts, estimated_cost=cost, currency='USD', basis='estimate', client='SECRET_CLIENT', model='SECRET_MODEL', **kw)

    def output(self):
        return server.prepaid_metrics(str(self.root)).decode()

    def state(self, output, state):
        self.assertIn(f'ai_prepaid_estimate_state{{provider="gemini_api",state="{state}"}} 1', output)

    def test_feature_off(self):
        handler = object.__new__(server.Handler)
        handler.path = '/metrics'
        with patch.object(server, 'PREPAID_DIR', ''), patch.object(server, 'SUBSCRIPTION_PATH', ''), patch.object(server, 'read_exposition', return_value=(b'example 1\n', 1, 4)), patch.object(handler, '_send') as send:
            handler.do_GET()
        self.assertEqual(send.call_args.args[1], b'example 1\n' + server.file_metrics('ai_ops_metrics', 'ledger', 1, 4))
        self.assertNotIn(b'ai_prepaid_', send.call_args.args[1])

    def test_missing_and_invalid(self):
        self.state(self.output(), 'baseline_missing')
        (self.root / 'baseline.json').write_text('{')
        self.state(self.output(), 'baseline_invalid')

    def test_missing_ledger_is_not_zero_usage(self):
        self.write_baseline()
        out = self.output()
        self.state(out, 'usage_ledger_missing')
        self.assertIn('ai_prepaid_usage_ledger_present{provider="gemini_api"} 0', out)
        self.assertNotIn('ai_prepaid_balance_estimated_remaining{', out)
        self.assertNotIn('ai_prepaid_tracked_usage_since_baseline{', out)
        self.assertNotIn('ai_prepaid_tracked_usage_events_since_baseline{', out)

    def test_unreadable_ledger_is_not_zero_usage(self):
        self.write_baseline()
        (self.root / 'usage.jsonl').mkdir()
        out = self.output()
        self.state(out, 'usage_ledger_missing')
        self.assertNotIn('ai_prepaid_balance_estimated_remaining{', out)

    def test_empty_ledger_and_negative(self):
        self.baseline['amount'] = -2
        self.write_baseline()
        (self.root / 'usage.jsonl').write_text('')
        out = self.output()
        self.state(out, 'ok')
        self.assertIn('ai_prepaid_usage_ledger_present{provider="gemini_api"} 1', out)
        self.assertIn('currency="USD"} -2.0', out)
        self.assertIn('basis="actual"} 0.0', out)

    def test_events_and_private_labels(self):
        self.write_baseline()
        events = [self.event('2025-12-31T23:00:00Z', 9), self.event('2026-01-01T01:00:00+00:00', 2)]
        (self.root / 'usage.jsonl').write_text('\n'.join(map(json.dumps, events)))
        out = self.output()
        self.state(out, 'ok')
        self.assertIn('basis="estimate"} 2.0', out)
        self.assertIn('currency="USD"} 8.0', out)
        self.assertIn('ai_prepaid_tracked_usage_events_since_baseline{provider="gemini_api"} 1', out)
        self.assertNotIn('SECRET_', out)

    def test_mismatch_precedes_invalid(self):
        self.write_baseline()
        event = self.event('2026-01-02T00:00:00Z', 2)
        event['currency'] = 'JPY'
        (self.root / 'usage.jsonl').write_text(json.dumps(event) + '\ninvalid\n')
        out = self.output()
        self.state(out, 'currency_mismatch')
        self.assertIn('ai_prepaid_usage_ledger_invalid_lines{provider="gemini_api"} 1', out)
        self.assertNotIn('ai_prepaid_balance_estimated_remaining{', out)

    def test_invalid_lines(self):
        self.write_baseline()
        (self.root / 'usage.jsonl').write_text('bad\n' + json.dumps(self.event('2026-01-02T00:00:00Z', -1)) + '\n{}\n')
        out = self.output()
        self.state(out, 'usage_ledger_invalid')
        self.assertIn('ai_prepaid_usage_ledger_invalid_lines{provider="gemini_api"} 3', out)

    def test_allowlist(self):
        self.baseline.update(currency='XYZ', source='untrusted')
        self.write_baseline()
        (self.root / 'usage.jsonl').write_text('')
        out = self.output()
        self.state(out, 'ok')
        self.assertIn('currency="other",source="other"', out)
        self.assertNotIn('XYZ', out)
        self.assertNotIn('untrusted', out)


if __name__ == '__main__':
    unittest.main()
