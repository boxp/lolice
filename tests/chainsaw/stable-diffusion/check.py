"""Runs inside the intake container against its own Envoy."""
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request

BASE = 'http://127.0.0.1:7860'


def call(method, path, data=None, headers=None):
    request = urllib.request.Request(BASE + path, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


status, body = call('GET', '/')
assert status == 503 and '画像生成'.encode() in body, (status, body)
assert not Path('/sd-switch/intent.json').exists(), 'a GET created a switch intent'
assert not any(Path('/sd-switch/jobs').iterdir()), 'a GET created a job'

status, body = call('POST', '/sdapi/v1/txt2img', b'{"prompt":"x"}', {'Content-Type': 'application/json'})
assert status == 202, (status, body)
job = json.loads(body)
assert not Path('/sd-switch/intent.json').exists(), 'a job is not an interactive intent'
status, body = call('GET', job['status_url'])
assert status == 200 and json.loads(body)['state'] == 'queued', (status, body)

temporary = Path('/sd-switch/.phase')
temporary.write_text('image-ready\n')
os.replace(temporary, '/sd-switch/phase')
status, body = call('GET', '/')
assert (status, body) == (200, b'stub-backend'), (status, body)

for _ in range(60):
    status, body = call('GET', job['status_url'])
    if json.loads(body)['state'] == 'done':
        break
    time.sleep(1)
else:
    raise SystemExit('job was not dispatched: ' + body.decode())
status, body = call('GET', job['result_url'])
assert status == 200 and json.loads(body) == {'images': ['stub']}, (status, body)
print('gateway checks passed')
