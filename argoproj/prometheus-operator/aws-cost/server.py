import datetime
import json
import math
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path('/snapshot')


def read(name, root=ROOT):
    try:
        value = json.loads((root / (name + '.json')).read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def render(root=ROOT):
    summary, services, daily, status = (read(n, root) for n in ('summary', 'services', 'daily', 'status'))
    lines = []
    def metric(name, value, labels=None):
        if value is None or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return
        full = 'aws_cost_' + name
        if full not in declared:
            lines.extend([f'# HELP {full} AWS cost snapshot {name.replace("_", " ")}.', f'# TYPE {full} gauge'])
            declared.add(full)
        def escape(v):
            return str(v).replace('\\', '\\\\').replace('\n', '\\n').replace('"', '\\"')
        suffix = '{' + ','.join(f'{k}="{escape(v)}"' for k, v in labels.items()) + '}' if labels else ''
        lines.append(f'{full}{suffix} {value}')
    declared = set()
    metric('last_attempt_timestamp_seconds', status.get('last_attempt_at'))
    if status.get('last_result') in ('ok', 'error'):
        metric('last_attempt_success', int(status['last_result'] == 'ok'))
    if status.get('last_result') == 'error' and isinstance(status.get('error_code'), str):
        metric('last_attempt_error_info', 1, {'code': status['error_code']})
    valid = summary.get('schema') == 'aws-cost/v1-draft' and isinstance(summary.get('snapshot_id'), int) and isinstance(summary.get('fetched_at'), (int, float)) and isinstance(summary.get('data_through'), str)
    try:
        through = datetime.date.fromisoformat(summary.get('data_through', ''))
        fetched = datetime.datetime.fromtimestamp(summary.get('fetched_at', 0), datetime.timezone.utc).date()
    except (ValueError, OverflowError, OSError, TypeError):
        valid = False
    metric('snapshot_available', int(valid))
    if not valid:
        return '\n'.join(lines) + '\n'
    metric('snapshot_info', 1, {'definition': summary['schema'], 'account_suffix': summary.get('account_suffix', '')})
    metric('snapshot_consistent', int(all(part.get('schema') == summary['schema'] and part.get('snapshot_id') == summary['snapshot_id'] for part in (services, daily))))
    metric('fetched_timestamp_seconds', summary['fetched_at'])
    metric('data_through_timestamp_seconds', int(datetime.datetime.combine(through, datetime.time(), datetime.timezone.utc).timestamp()))
    for key in ('mtd_usd', 'prev_month_total_usd', 'prev_month_same_period_usd', 'linked_accounts'):
        metric(key, summary.get(key))
    metric('fetch_requests', summary.get('requests'))
    for period in ('current', 'prev'):
        value = summary.get('estimated_' + period)
        if isinstance(value, bool):
            metric('estimated', int(value), {'period': period})
    forecast = summary.get('forecast')
    if isinstance(forecast, dict) and isinstance(forecast.get('available'), bool):
        metric('forecast_available', int(forecast['available']))
        if forecast['available']:
            for bound in ('mean', 'lower', 'upper'):
                metric('forecast_month_end_usd', forecast.get(bound), {'bound': bound})
    if services.get('snapshot_id') == summary['snapshot_id'] and isinstance(services.get('services'), dict):
        for service, value in services['services'].items():
            metric('mtd_by_service_usd', value, {'service': service})
    # daily is a matrix: values[i][j] is services[i] on days[j]; days_ago is relative to the fetch date.
    days, names, values = daily.get('days'), daily.get('services'), daily.get('values')
    if daily.get('snapshot_id') == summary['snapshot_id'] and all(isinstance(x, list) for x in (days, names, values)) and len(names) == len(values):
        try:
            agos = [(fetched - datetime.date.fromisoformat(d)).days for d in days]
        except (ValueError, TypeError):
            agos = []
        for service, row in zip(names, values):
            if not isinstance(row, list) or len(row) != len(agos):
                continue
            for ago, value in zip(agos, row):
                if 1 <= ago <= 14:
                    metric('day_usd', value, {'service': service, 'days_ago': ago})
    # BOXP-598: S3 cost by usage type, same matrix form; only rows from this snapshot, null quantity (Other) is skipped.
    s3 = read('s3_usage', root)
    if s3.get('schema') == summary['schema'] and s3.get('snapshot_id') == summary['snapshot_id'] and isinstance(s3.get('available'), bool):
        metric('s3_usage_available', int(s3['available']))
        days, names, values = s3.get('days'), s3.get('usage_types'), s3.get('values')
        quantities = s3.get('quantities')
        if s3['available'] and all(isinstance(x, list) for x in (days, names, values)) and len(names) == len(values):
            try:
                agos = [(fetched - datetime.date.fromisoformat(d)).days for d in days]
            except (ValueError, TypeError):
                agos = []
            if not (isinstance(quantities, list) and len(quantities) == len(names)):
                quantities = [None] * len(names)
            # One metric family at a time: the text format must not interleave families.
            for kind, rows in (('usd', values), ('quantity', quantities)):
                for usage_type, r in zip(names, rows):
                    if not isinstance(r, list) or len(r) != len(agos):
                        continue
                    for ago, value in zip(agos, r):
                        if 1 <= ago <= 14:
                            metric('s3_usage_type_day_' + kind, value, {'usage_type': usage_type, 'days_ago': ago})
    return '\n'.join(lines) + '\n'


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/healthz':
            body, code = b'ok\n', 200
        elif self.path == '/metrics':
            body, code = render().encode(), 200
        else:
            body, code = b'not found\n', 404
        self.send_response(code)
        self.send_header('Content-Type', 'text/plain; version=0.0.4')
        self.end_headers()
        self.wfile.write(body)


if __name__ == '__main__':
    HTTPServer(('0.0.0.0', 9465), Handler).serve_forever()
