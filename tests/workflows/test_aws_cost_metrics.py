import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

path = Path(__file__).resolve().parents[2] / 'argoproj/prometheus-operator/aws-cost/server.py'
spec = importlib.util.spec_from_file_location('aws_cost_metrics', path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class MetricsTest(unittest.TestCase):
    def test_uninitialized_and_status(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'status.json').write_text(json.dumps({'last_result': 'error', 'error_code': 'Failure'}))
            output = mod.render(root)
            self.assertIn('aws_cost_snapshot_available 0', output)
            self.assertIn('aws_cost_last_attempt_success 0', output)
            self.assertNotIn('aws_cost_mtd_usd', output)

    def test_valid_and_escaping(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            data = {'summary': {'schema': 'aws-cost/v1-draft', 'snapshot_id': 1, 'fetched_at': 1709251200, 'data_through': '2024-02-29', 'mtd_usd': 2, 'forecast': {'available': False}}, 'services': {'schema': 'aws-cost/v1-draft', 'snapshot_id': 1, 'services': {'A"B': 2}}, 'daily': {'schema': 'aws-cost/v1-draft', 'snapshot_id': 1, 'days': ['2024-02-15', '2024-02-29'], 'services': ['A'], 'values': [[7, 2]]}}
            for key, value in data.items():
                (root / (key + '.json')).write_text(json.dumps(value))
            output = mod.render(root)
            self.assertIn('aws_cost_snapshot_consistent 1', output)
            self.assertIn('service="A\\"B"', output)
            self.assertIn('aws_cost_day_usd{service="A",days_ago="1"} 2', output)
            self.assertNotIn('days_ago="15"', output)
            self.assertNotIn('aws_cost_forecast_month_end_usd', output)
            data['daily']['snapshot_id'] = 2
            (root / 'daily.json').write_text(json.dumps(data['daily']))
            self.assertIn('aws_cost_snapshot_consistent 0', mod.render(root))


if __name__ == '__main__':
    unittest.main()
