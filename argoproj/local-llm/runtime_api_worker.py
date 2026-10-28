"""Restricted local-file API transport. The projected SA token never leaves this Pod.

Requests contain control metadata only. A durable intent precedes API I/O; an
interrupted request is returned as unknown and is never automatically repeated.
"""
import fcntl
import json
import os
from pathlib import Path
import re
import ssl
import time
import urllib.error
import urllib.request
import uuid

ROOT = Path('/runtime-api')
TOKEN = Path('/var/run/runtime-identity/token')
CA = '/var/run/runtime-identity/ca.crt'
BASE = 'https://kubernetes.default.svc'
TARGETS = {
    '/apis/apps/v1/namespaces/local-llm/deployments/llama-server/scale': '/spec',
    '/apis/apps/v1/namespaces/stable-diffusion/deployments/qwen-image-eval/scale': '/spec',
    '/api/v1/namespaces/local-llm/configmaps/llama-server-admission': '/data/state',
}


def validate(request):
    if set(request) != {'method', 'path', 'body', 'expires'}:
        raise ValueError('request keys')
    method, path, body = (request[k] for k in ('method', 'path', 'body'))
    if path not in TARGETS or method not in ('GET', 'PATCH'):
        raise ValueError('target')
    if not isinstance(request['expires'], (int, float)) or not time.time() < request['expires'] <= time.time() + 120:
        raise ValueError('expired request')
    if method == 'GET':
        if body is not None:
            raise ValueError('GET body')
        return
    if not isinstance(body, list) or len(body) != 4:
        raise ValueError('patch shape')
    pointer = TARGETS[path]
    for entry, op, key in zip(body, ('test', 'test', 'test', 'replace'),
                              ('/metadata/uid', '/metadata/resourceVersion', pointer, pointer)):
        if not isinstance(entry, dict) or set(entry) != {'op', 'path', 'value'} or entry['op'] != op or entry['path'] != key:
            raise ValueError('conditional patch required')
    if any(not isinstance(x['value'], str) or not x['value'] for x in body[:2]):
        raise ValueError('UID/resourceVersion')
    for entry in body[2:]:
        value = entry['value']
        if pointer == '/spec':
            if not isinstance(value, dict) or set(value) - {'replicas'} or type(value.get('replicas', 0)) is not int or value.get('replicas', 0) not in (0, 1):
                raise ValueError('replica boundary')
        elif not isinstance(value, str) or not re.fullmatch(r'(open|closed|verify):[A-Za-z0-9_-]{1,120}', value):
            raise ValueError('gate boundary')


def write_new(path, value):
    data = json.dumps(value).encode()
    temporary = path.parent / ('.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.link(temporary, path)
    temporary.unlink()
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def call(request):
    data = None if request['body'] is None else json.dumps(request['body']).encode()
    headers = {'Authorization': 'Bearer ' + TOKEN.read_text().strip(),
               'Content-Type': 'application/json-patch+json'}
    req = urllib.request.Request(BASE + request['path'], data=data, headers=headers, method=request['method'])
    with urllib.request.urlopen(req, context=ssl.create_default_context(cafile=CA), timeout=20) as response:
        return {'status': response.status, 'object': json.load(response)}


def process(root, path, api=call):
    identity = path.stem
    if not re.fullmatch(r'[a-f0-9]{32}', identity) or path.is_symlink():
        return
    result = root / 'responses' / (identity + '.json')
    attempt = root / 'attempts' / (identity + '.json')
    if result.exists():
        return
    if attempt.exists():
        write_new(result, {'status': 'unknown'})
        return
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            content = stream.read(65537)
        if len(content) > 65536:
            raise ValueError('request too large')
        request = json.loads(content)
        validate(request)
    except (OSError, ValueError, TypeError, KeyError):
        write_new(result, {'status': 'rejected'})
        return
    write_new(attempt, {'started': time.time()})
    try:
        response = api(request)
    except urllib.error.HTTPError as error:
        response = {'status': error.code}
    except Exception:
        response = {'status': 'unknown'}
    write_new(result, response)


def main():
    os.umask(0o077)
    for name in ('requests', 'responses', 'attempts'):
        (ROOT / name).mkdir(exist_ok=True)
    with (ROOT / 'worker.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while True:
            for path in sorted((ROOT / 'requests').glob('*.json')):
                process(ROOT, path)
            time.sleep(0.2)


if __name__ == '__main__':
    main()
