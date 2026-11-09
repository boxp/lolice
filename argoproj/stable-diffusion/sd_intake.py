"""Durable image generation intake; job IDs are bearer capabilities."""
import json
import os
from pathlib import Path
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path('/sd-switch')
BACKEND = 'http://stable-diffusion-webui-backend:7860'
LIMIT = 16 * 1024 * 1024
RETRY = 30
GENERATION = ('/sdapi/v1/txt2img', '/sdapi/v1/img2img')
FINISHED = ('done', 'failed', 'unknown')


def atomic(path, data):
    temporary = path.parent / ('.' + uuid.uuid4().hex + '.tmp')
    with temporary.open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path, value):
    atomic(path, json.dumps(value, ensure_ascii=False).encode())


def read_json(path):
    return json.loads(path.read_text())


class Intake:
    def __init__(self, root=ROOT, clock=time.time, http=None):
        self.root, self.clock = Path(root), clock
        self.http = http or self.send
        self.lock = threading.RLock()
        self.jobs = self.root / 'jobs'
        self.jobs.mkdir(parents=True, exist_ok=True)
        with self.lock:
            for job in self.all_jobs():
                if job['state'] == 'dispatched':
                    self.status(job['job_id'], 'unknown', explanation='Dispatch was interrupted; the job will not be sent again.')

    def phase(self):
        try:
            phase = (self.root / 'phase').read_text().strip()
        except OSError:
            return 'unavailable'
        return phase if phase in ('llm-open', 'fencing', 'draining', 'stopping-llm', 'starting-sd', 'image-ready', 'image-draining', 'stopping-sd', 'starting-llm', 'verifying', 'reopening', 'failed') else 'unavailable'

    def all_jobs(self):
        jobs = []
        for path in self.jobs.iterdir():
            if not path.is_dir() or not re.fullmatch(r'[0-9a-f]{32}', path.name):
                continue
            try:
                job = read_json(path / 'status.json')
                if job['job_id'] == path.name:
                    jobs.append(job)
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return sorted(jobs, key=lambda job: (job['created'], job['job_id']))

    def status(self, identity, state, **extra):
        path = self.jobs / identity / 'status.json'
        value = read_json(path)
        value.update(state=state, updated=self.clock(), **extra)
        write_json(path, value)
        return value

    def create(self, path, body, key):
        if key is not None and (not key or len(key) > 128 or any(ord(c) < 32 or ord(c) > 126 for c in key)):
            raise ValueError('invalid Idempotency-Key')
        with self.lock:
            jobs = self.all_jobs()
            if key is not None:
                for job in jobs:
                    request = read_json(self.jobs / job['job_id'] / 'request.json')
                    if request.get('idempotency_key') == key:
                        return job['job_id']
            if sum(job['state'] not in FINISHED for job in jobs) >= 8:
                raise OverflowError('queue full')
            identity = uuid.uuid4().hex
            directory = self.jobs / identity
            directory.mkdir(mode=0o700)
            fd = os.open(self.jobs, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            write_json(directory / 'request.json', {'path': path, 'body': body, 'idempotency_key': key})
            now = self.clock()
            write_json(directory / 'status.json', {'job_id': identity, 'state': 'queued', 'created': now, 'updated': now})
            return identity

    def intent(self):
        with self.lock:
            path = self.root / 'intent.json'
            if not path.exists():
                write_json(path, {'created': self.clock(), 'id': uuid.uuid4().hex})

    def summary(self):
        with self.lock:
            jobs = [job for job in self.all_jobs() if job['state'] not in FINISHED]
            phase = self.phase()
            return {'phase': phase, 'queued': sum(job['state'] == 'queued' for job in jobs),
                    'dispatched': sum(job['state'] == 'dispatched' for job in jobs),
                    'window_active': phase in ('image-ready', 'image-draining')}

    @staticmethod
    def send(path, body):
        request = urllib.request.Request(BACKEND + path, data=json.dumps(body).encode(),
                                         headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with urllib.request.urlopen(request, timeout=1800) as response:
                return response.status, response.read(), response.headers.get('Content-Type', 'application/json')
        except urllib.error.HTTPError as error:
            return error.code, b'', 'application/json'

    def dispatch_once(self):
        with self.lock:
            if self.phase() != 'image-ready':
                return False
            queued = [job for job in self.all_jobs() if job['state'] == 'queued']
            if not queued:
                return False
            identity = queued[0]['job_id']
            request = read_json(self.jobs / identity / 'request.json')
            self.status(identity, 'dispatched')
            # The switcher publishes image-draining and then looks for dispatched
            # jobs; recording first and re-reading here means one of us always
            # sees the other. Nothing was sent yet, so queued is still accurate.
            if self.phase() != 'image-ready':
                self.status(identity, 'queued')
                return False
        try:
            code, data, content_type = self.http(request['path'], request['body'])
            with self.lock:
                if 200 <= code < 300:
                    atomic(self.jobs / identity / 'result.json', data)
                    self.status(identity, 'done', content_type=content_type)
                else:
                    self.status(identity, 'failed', http_status=code)
        except Exception:
            with self.lock:
                self.status(identity, 'unknown', explanation='The backend outcome is unknown; the job will not be sent again.')
        finally:
            atomic(self.root / 'activity', str(self.clock()).encode())
        return True

    def cleanup(self):
        with self.lock:
            for job in self.all_jobs():
                lifetime = 72 * 3600 if job['state'] == 'unknown' else 24 * 3600
                if job['state'] in FINISHED and self.clock() - job['updated'] >= lifetime:
                    shutil.rmtree(self.jobs / job['job_id'])

    def run(self):
        while True:
            self.dispatch_once()
            self.cleanup()
            time.sleep(1)


def handler(intake):
    class Handler(BaseHTTPRequestHandler):
        def respond(self, code, body, content_type='application/json', retry=False):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            if retry:
                self.send_header('Retry-After', str(RETRY))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            path = self.path.split('?', 1)[0]
            if path == '/__sd/switch':
                # A custom header cannot be sent cross-site without a CORS
                # preflight, so another site cannot start a GPU switch.
                if self.headers.get('X-SD-Switch') != '1':
                    self.respond(403, {'error': 'X-SD-Switch: 1 required'})
                    return
                intake.intent()
                self.respond(202, {'phase': intake.phase()})
                return
            if path not in GENERATION:
                self.respond(404, {'error': 'not found'})
                return
            if self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower() != 'application/json':
                self.respond(415, {'error': 'application/json required'})
                return
            try:
                length = int(self.headers.get('Content-Length', ''))
                if length < 0:
                    raise ValueError()
                if length > LIMIT:
                    self.respond(413, {'error': 'body too large'})
                    return
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError()
                identity = intake.create(path, body, self.headers.get('Idempotency-Key'))
            except (ValueError, UnicodeError):
                self.respond(400, {'error': 'invalid request'})
                return
            except OverflowError:
                self.respond(429, {'error': 'queue full'}, retry=True)
                return
            self.respond(202, {'job_id': identity, 'status_url': '/__sd/jobs/' + identity,
                               'result_url': '/__sd/jobs/' + identity + '/result', 'retry_after': RETRY}, retry=True)

        def do_GET(self):
            path = self.path.split('?', 1)[0]
            if path in ('/__sd/status', '/__sd/health'):
                self.respond(200, intake.summary())
                return
            match = re.fullmatch(r'/__sd/jobs/([0-9a-f]{32})(/result)?', path)
            if match:
                with intake.lock:
                    try:
                        status = read_json(intake.jobs / match[1] / 'status.json')
                        if match[2] and status['state'] == 'done':
                            self.respond(200, (intake.jobs / match[1] / 'result.json').read_bytes(), status.get('content_type', 'application/json'))
                        elif match[2]:
                            self.respond(409, {'error': 'result not available', 'state': status['state']})
                        else:
                            queued = [job['job_id'] for job in intake.all_jobs() if job['state'] == 'queued']
                            if match[1] in queued:
                                status['position'] = queued.index(match[1]) + 1
                            self.respond(200, status)
                    except (OSError, ValueError):
                        self.respond(404, {'error': 'not found'})
                return
            if path.startswith('/sdapi/') or 'application/json' in self.headers.get('Accept', ''):
                self.respond(503, {'phase': intake.phase(), 'message': 'Generation POSTs are queued durably; a GET does not start a switch.'}, retry=True)
                return
            guidance = ('手動復旧が必要です。管理者に連絡してください。' if intake.phase() == 'failed'
                        else '切替には時間がかかります。しばらくしてから再試行してください。')
            page = ('<!doctype html><html lang="ja"><meta charset="utf-8"><title>画像生成の準備</title>'
                    '<meta http-equiv="refresh" content="30"><h1>画像生成の準備</h1><p>状態: ' + intake.phase() +
                    '</p><p>' + guidance + '</p>'
                    '<button onclick="fetch(\'/__sd/switch\',{method:\'POST\',headers:{\'X-SD-Switch\':\'1\'}}).then(()=>location.reload())">画像生成モードへ切替</button></html>')
            self.respond(503, page.encode(), 'text/html; charset=utf-8', retry=True)

    return Handler


def main():
    intake = Intake()
    threading.Thread(target=intake.run, daemon=True).start()
    ThreadingHTTPServer(('127.0.0.1', 8090), handler(intake)).serve_forever()


if __name__ == '__main__':
    main()
