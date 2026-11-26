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

    def test_s3_usage_types(self):
        # BOXP-598 仕様例: s3_usage は daily と同じ行列。同じ snapshot の分だけ出す。Other の使用量 null は出さない。
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            summary = {'schema': 'aws-cost/v1-draft', 'snapshot_id': 1, 'fetched_at': 1709251200, 'data_through': '2024-02-29', 'forecast': {'available': False}}
            s3 = {'schema': 'aws-cost/v1-draft', 'snapshot_id': 1, 'available': True, 'days': ['2024-02-14', '2024-02-29'], 'usage_types': ['APN1-TimedStorage-ByteHrs', 'Other'], 'values': [[9, 0.5], [0, 0.25]], 'quantities': [[1, 20.5], [None, None]]}
            (root / 'summary.json').write_text(json.dumps(summary))
            (root / 's3_usage.json').write_text(json.dumps(s3))
            output = mod.render(root)
            self.assertIn('aws_cost_s3_usage_available 1', output)
            self.assertIn('aws_cost_s3_usage_type_day_usd{usage_type="APN1-TimedStorage-ByteHrs",days_ago="1"} 0.5', output)
            self.assertIn('aws_cost_s3_usage_type_day_usd{usage_type="Other",days_ago="1"} 0.25', output)
            self.assertIn('aws_cost_s3_usage_type_day_quantity{usage_type="APN1-TimedStorage-ByteHrs",days_ago="1"} 20.5', output)
            self.assertNotIn('days_ago="15"', output)
            self.assertNotIn('aws_cost_s3_usage_type_day_quantity{usage_type="Other"', output)
            lines = output.splitlines()
            usd = [i for i, line in enumerate(lines) if line.startswith('aws_cost_s3_usage_type_day_usd{')]
            qty = [i for i, line in enumerate(lines) if line.startswith('aws_cost_s3_usage_type_day_quantity{')]
            self.assertLess(max(usd), min(qty))  # 系列ごとにまとまって出る（テキスト形式の要件）
            # 別 snapshot の残りや未取得は出さない（0 と読ませない）
            s3['snapshot_id'] = 2
            (root / 's3_usage.json').write_text(json.dumps(s3))
            output = mod.render(root)
            self.assertNotIn('aws_cost_s3_usage_type_day_usd', output)
            self.assertNotIn('aws_cost_s3_usage_available', output)
            (root / 's3_usage.json').write_text(json.dumps({'schema': 'aws-cost/v1-draft', 'snapshot_id': 1, 'available': False, 'error_code': 'Throttling'}))
            output = mod.render(root)
            self.assertIn('aws_cost_s3_usage_available 0', output)
            self.assertNotIn('aws_cost_s3_usage_type_day_usd', output)


if __name__ == '__main__':
    unittest.main()
