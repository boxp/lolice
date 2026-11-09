"""Durable llama <-> Stable Diffusion switcher (BOXP-484).

Holds no credential: every Kubernetes call goes through the existing file
transport in /runtime-api, whose worker enforces the target allowlist and the
conditional patch shape. The gate compare-and-set is the mutex against other
GPU owners. An intended write is persisted first, a write with an unknown
outcome is never replayed, and any doubt ends in the sticky `failed` phase with
admission still closed.

The private state directory is not visible to the internet-facing gateway, which
only shares /runtime-api/sd-switch (phase, jobs, intent, activity).
"""
import collections
import fcntl
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request
import uuid

ROOT = Path('/runtime-api')
LLM = 'http://llama-server.local-llm.svc.cluster.local'
SD = 'http://stable-diffusion-webui-backend.stable-diffusion.svc.cluster.local:7860'
SCALE = '/apis/apps/v1/namespaces/{}/deployments/{}/scale'
PATHS = {'llama': SCALE.format('local-llm', 'llama-server'),
         'sd': SCALE.format('stable-diffusion', 'stable-diffusion-webui'),
         'qwen': SCALE.format('stable-diffusion', 'qwen-image-eval'),
         'gate': '/api/v1/namespaces/local-llm/configmaps/llama-server-admission'}

COOLDOWN = 120
DRAIN_ABORT_COOLDOWN = 600
START_FAILURE_COOLDOWN = 3600
DRAIN_LIMIT = 2100
START_LIMIT = 1200
STOP_LIMIT = 600
PROBE_FAILURE_LIMIT = 1800
IDLE_RELEASE = 300
WINDOW_LIMIT = 1800
OBSERVE_INTERVAL = 10
STABLE_NEEDED = 3
PROBE_SPACING = 310
TICK = 5
STATUS_LIMIT = 65536

Snapshot = collections.namedtuple('Snapshot', 'llama sd qwen gate llama_running sd_running objects')


class Unavailable(Exception):
    """Observation failed; nothing changed, so a later tick may retry."""


class Conflict(Exception):
    """The API definitely refused a conditional write."""


class Stop(Exception):
    """Unsafe to continue automatically."""


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic(path, data):
    temporary = path.parent / ('.' + uuid.uuid4().hex + '.tmp')
    with temporary.open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    fsync_directory(path.parent)


def read_limited(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        return stream.read(STATUS_LIMIT)


class FileTransport:
    def __init__(self, root=ROOT, clock=time.time, sleep=time.sleep, timeout=60):
        self.root, self.clock, self.sleep, self.timeout = Path(root), clock, sleep, timeout

    def call(self, method, path, body=None):
        identity = uuid.uuid4().hex
        request = self.root / 'requests' / (identity + '.json')
        temporary = request.parent / ('.' + identity + '.tmp')
        with temporary.open('x') as stream:
            json.dump({'method': method, 'path': path, 'body': body, 'expires': self.clock() + 100}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, request)
        temporary.unlink()
        fsync_directory(request.parent)
        response = self.root / 'responses' / (identity + '.json')
        deadline = self.clock() + self.timeout
        while self.clock() < deadline:
            if response.exists():
                value = json.loads(response.read_text())
                # Request first: otherwise the worker would answer it again.
                for name in ('requests', 'attempts', 'responses'):
                    (self.root / name / (identity + '.json')).unlink(missing_ok=True)
                return value
            self.sleep(.2)
        return {'status': 'unknown'}


class Cluster:
    def __init__(self, transport):
        self.transport = transport

    def snapshot(self):
        objects = {}
        for name, path in PATHS.items():
            response = self.transport.call('GET', path)
            if response.get('status') != 200:
                raise Unavailable(name + ' read failed')
            objects[name] = response['object']
        replicas = [objects[name].get('spec', {}).get('replicas', 0) for name in ('llama', 'sd', 'qwen')]
        running = [objects[name].get('status', {}).get('replicas', 0) for name in ('llama', 'sd')]
        gate = objects['gate'].get('data', {}).get('state')
        if not isinstance(gate, str) or any(type(value) is not int or value not in (0, 1) for value in replicas):
            raise Stop('unexpected cluster value')
        return Snapshot(*replicas, gate, *running, objects)

    def patch(self, field, snapshot, value):
        target = snapshot.objects[field]
        if field == 'gate':
            pointer, old, new = '/data/state', snapshot.gate, value
        else:
            pointer, old, new = '/spec', target.get('spec', {}), dict(target.get('spec', {}), replicas=value)
        response = self.transport.call('PATCH', PATHS[field], [
            {'op': 'test', 'path': '/metadata/uid', 'value': target['metadata']['uid']},
            {'op': 'test', 'path': '/metadata/resourceVersion', 'value': target['metadata']['resourceVersion']},
            {'op': 'test', 'path': pointer, 'value': old},
            {'op': 'replace', 'path': pointer, 'value': new}])
        status = response.get('status')
        if status in (409, 422):
            raise Conflict(field)
        if status != 200:
            raise Stop('write outcome unknown for ' + field)


def http(url, method='GET', data=None, timeout=10):
    request = urllib.request.Request(url, data=data, method=method, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def acquire_owner(path):
    """One owner per node. Raises OSError while another instance holds it."""
    handle = open(path, 'a+b')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise
    return handle


class Switcher:
    def __init__(self, state, shared, cluster, http_call=http, clock=time.time, log=None):
        self.directory, self.shared = Path(state), Path(shared)
        self.cluster, self.http, self.clock = cluster, http_call, clock
        self.log = log or (lambda message: print(message, flush=True))
        self.stable, self.last_observed = 0, 0
        self.path = self.directory / 'state.json'
        if self.path.exists():
            self.state = json.loads(self.path.read_text())
        else:
            self.state = {'phase': 'llm-open', 'token': None, 'pending': None, 'phase_since': clock(),
                          'cooldown_until': 0, 'reason': None}
            self.save()
        self.publish()

    def save(self):
        atomic(self.path, json.dumps(self.state).encode())

    def publish(self, phase=None):
        atomic(self.shared / 'phase', ((phase or self.state['phase']) + '\n').encode())

    def enter(self, phase):
        self.state.update(phase=phase, phase_since=self.clock())
        self.stable, self.last_observed = 0, 0
        self.save()
        self.publish()
        self.log('phase ' + phase)

    def fail(self, reason):
        self.state['reason'] = reason
        self.enter('failed')

    def elapsed(self):
        return self.clock() - self.state['phase_since']

    def due(self, interval=OBSERVE_INTERVAL):
        if self.clock() - self.last_observed < interval:
            return False
        self.last_observed = self.clock()
        return True

    @staticmethod
    def triple(snapshot):
        return {'llama': snapshot.llama, 'sd': snapshot.sd, 'gate': snapshot.gate}

    def expect(self, snapshot, llama, sd, gates):
        if snapshot.qwen != 0 or snapshot.llama not in llama or snapshot.sd != sd or snapshot.gate not in gates:
            raise Stop('cluster state drifted in ' + self.state['phase'])

    def write(self, snapshot, field, value, following, via=None):
        desired = dict(self.triple(snapshot), **{field: value})
        self.state['pending'] = {'field': field, 'value': value, 'desired': desired, 'next': following}
        self.save()
        if via:
            self.publish(via)
        try:
            self.cluster.patch(field, snapshot, value)
        except Conflict:
            if self.state['phase'] != 'llm-open':
                raise Stop('conditional write refused for ' + field)
            self.state['pending'] = None
            self.save()
            self.publish()
            return False
        self.settle()
        return True

    def settle(self):
        """Only ever observes; never repeats the write."""
        pending = self.state['pending']
        if self.triple(self.cluster.snapshot()) != pending['desired']:
            raise Stop('interrupted write did not reach its desired state')
        self.state['pending'] = None
        self.enter(pending['next'])

    # Demand and observation

    def jobs(self):
        counts = {'queued': 0, 'dispatched': 0}
        root = self.shared / 'jobs'
        if not root.is_dir():
            return counts
        for directory in root.iterdir():
            if not re.fullmatch(r'[0-9a-f]{32}', directory.name):
                continue
            try:
                state = json.loads(read_limited(directory / 'status.json'))['state']
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if state in counts:
                counts[state] += 1
        return counts

    def demand(self):
        return self.jobs()['queued'] > 0 or (self.shared / 'intent.json').exists()

    def observed_activity(self):
        try:
            return float(read_limited(self.shared / 'activity'))
        except (OSError, ValueError):
            return 0

    def get(self, url):
        try:
            return self.http(url)
        except (OSError, ValueError, urllib.error.URLError):
            return None, b''

    def admission_is(self, gate):
        code, body = self.get(LLM + ':8080/__board_pilot__/admission')
        return code == 200 and body.decode(errors='replace').strip() == gate

    def llm_idle(self):
        try:
            code, body = self.get(LLM + ':9901/stats?format=json')
            stats = {item['name']: item['value'] for item in json.loads(body)['stats'] if 'name' in item}
            if code != 200 or any(stats.get('cluster.llama_server.' + key) != 0
                                  for key in ('upstream_rq_active', 'upstream_rq_pending_active')):
                return False
            code, body = self.get(LLM + ':9901/__board_pilot__/models')
            if code != 200:
                return False
            models = json.loads(body)['data']
            if any(model.get('status', {}).get('value') not in ('loaded', 'unloaded') for model in models):
                return False
            for model in models:
                if model['status']['value'] != 'loaded':
                    continue
                code, body = self.get(LLM + ':9901/__board_pilot__/slots?model=' + model['id'])
                if code == 503:
                    continue
                slots = json.loads(body) if code == 200 else None
                if not isinstance(slots, list) or not slots or any(slot.get('is_processing') is not False for slot in slots):
                    return False
            return True
        except (ValueError, KeyError, TypeError, AttributeError):
            return False

    def sd_busy(self):
        """True, False, or None when the backend cannot say. None is not idle."""
        code, body = self.get(SD + '/sdapi/v1/progress')
        if code != 200:
            return None
        try:
            value = json.loads(body)
            state = value['state']
            if state['job_count'] != 0 or state.get('job') or value.get('progress', 0) != 0:
                return True
            return False
        except (ValueError, KeyError, TypeError):
            return None

    def track_probe(self, busy):
        """A backend that stays unobservable must not hold the GPU forever."""
        if busy is None:
            since = self.state.setdefault('probe_failing_since', self.clock())
            if self.clock() - since >= PROBE_FAILURE_LIMIT:
                raise Stop('image backend unobservable')
        else:
            self.state.pop('probe_failing_since', None)

    def complete_window(self):
        self.state['cooldown_until'] = self.clock() + self.state.pop('next_cooldown', COOLDOWN)
        self.enter('llm-open')

    # Phases

    def on_llm_open(self):
        if self.clock() < self.state['cooldown_until'] or not self.demand():
            return
        snapshot = self.cluster.snapshot()
        if (snapshot.llama, snapshot.sd, snapshot.qwen) != (1, 0, 0) or not snapshot.gate.startswith('open:'):
            return
        self.state['token'] = 'sd-' + uuid.uuid4().hex[:16]
        self.write(snapshot, 'gate', 'closed:' + self.state['token'], 'draining', via='fencing')

    def closed(self):
        return 'closed:' + self.state['token']

    def verify_gate(self):
        return 'verify:' + self.state['token']

    def on_draining(self):
        snapshot = self.cluster.snapshot()
        self.expect(snapshot, (1,), 0, (self.closed(),))
        if self.elapsed() > DRAIN_LIMIT:
            self.state.update(reason='drain limit reached; inference was never stopped', next_cooldown=DRAIN_ABORT_COOLDOWN)
            if self.write(snapshot, 'gate', 'open:' + self.state['token'], 'reopening', via='reopening'):
                self.log('drain aborted')
            return
        if not self.due():
            return
        self.stable = self.stable + 1 if self.admission_is(snapshot.gate) and self.llm_idle() else 0
        if self.stable >= STABLE_NEEDED:
            self.write(snapshot, 'llama', 0, 'stopping-llm')

    def on_stopping_llm(self):
        snapshot = self.cluster.snapshot()
        self.expect(snapshot, (0,), 0, (self.closed(),))
        if self.elapsed() > STOP_LIMIT:
            raise Stop('llama did not stop')
        if snapshot.llama_running == 0:
            self.write(snapshot, 'sd', 1, 'starting-sd')

    def on_starting_sd(self):
        snapshot = self.cluster.snapshot()
        self.expect(snapshot, (0,), 1, (self.closed(),))
        if self.get(SD + '/sdapi/v1/progress')[0] == 200:
            (self.shared / 'intent.json').unlink(missing_ok=True)
            self.state.update(window_started=self.clock(), last_activity=self.clock())
            self.enter('image-ready')
        elif self.elapsed() > START_LIMIT:
            # Nothing was generated, so returning the GPU to LLM is safe.
            self.state.update(reason='image backend did not start', next_cooldown=START_FAILURE_COOLDOWN)
            self.write(snapshot, 'sd', 0, 'stopping-sd')

    def on_image_ready(self):
        if not self.due(15):
            return
        snapshot = self.cluster.snapshot()
        self.expect(snapshot, (0,), 1, (self.closed(),))
        busy = self.sd_busy()
        self.track_probe(busy)
        if busy:
            self.state['last_activity'] = self.clock()
        self.save()
        jobs = self.jobs()
        latest = max(self.state['last_activity'], self.observed_activity())
        idle = (not jobs['queued'] and not jobs['dispatched'] and busy is False and self.clock() - latest >= IDLE_RELEASE)
        if idle or self.clock() - self.state['window_started'] >= WINDOW_LIMIT:
            self.enter('image-draining')

    def on_image_draining(self):
        if not self.due():
            return
        snapshot = self.cluster.snapshot()
        self.expect(snapshot, (0,), 1, (self.closed(),))
        busy = self.sd_busy()
        self.track_probe(busy)
        self.save()
        self.stable = self.stable + 1 if busy is False and not self.jobs()['dispatched'] else 0
        if self.stable >= STABLE_NEEDED:
            self.write(snapshot, 'sd', 0, 'stopping-sd')

    def on_stopping_sd(self):
        snapshot = self.cluster.snapshot()
        self.expect(snapshot, (0,), 0, (self.closed(), self.verify_gate()))
        if self.elapsed() > STOP_LIMIT:
            raise Stop('image backend did not stop')
        if snapshot.sd_running != 0:
            return
        if snapshot.gate == self.closed():
            self.write(snapshot, 'gate', self.verify_gate(), 'stopping-sd')
        else:
            self.enter('starting-llm')

    def on_starting_llm(self):
        snapshot = self.cluster.snapshot()
        self.expect(snapshot, (0, 1), 0, (self.verify_gate(),))
        if self.elapsed() > START_LIMIT:
            raise Stop('llama did not start')
        if snapshot.llama == 0:
            self.write(snapshot, 'llama', 1, 'starting-llm')
        elif self.admission_is(snapshot.gate) and self.get(LLM + ':8080/health')[0] == 200:
            self.enter('verifying')

    def on_verifying(self):
        snapshot = self.cluster.snapshot()
        self.expect(snapshot, (1,), 0, (self.verify_gate(),))
        if self.elapsed() > START_LIMIT:
            raise Stop('llama short inference did not succeed')
        if self.clock() - self.state.get('probe_at', 0) < PROBE_SPACING:
            return
        self.state['probe_at'] = self.clock()
        self.save()
        try:
            code, body = self.http(LLM + ':9901/__board_pilot__/restore-probe', 'POST', b'{}', 360)
            ok = code == 200 and bool(json.loads(body)['choices'][0]['message']['content'].strip())
        except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError, urllib.error.URLError):
            ok = False
        if ok:
            self.write(snapshot, 'gate', 'open:' + self.state['token'], 'reopening', via='reopening')

    def on_reopening(self):
        snapshot = self.cluster.snapshot()
        self.expect(snapshot, (1,), 0, ('open:' + self.state['token'],))
        if self.elapsed() > STOP_LIMIT:
            raise Stop('gate did not reopen')
        if self.admission_is(snapshot.gate):
            self.state.pop('probe_at', None)
            self.complete_window()

    def on_failed(self):
        """Operator input: the failed token written to <state>/recover."""
        path = self.directory / 'recover'
        if not path.exists():
            return
        try:
            supplied = read_limited(path).decode(errors='replace').strip()
        finally:
            path.unlink()
        if not supplied or supplied != self.state['token']:
            self.log('recover ignored: token mismatch')
            return
        snapshot = self.cluster.snapshot()
        healthy = (snapshot.llama, snapshot.sd, snapshot.qwen) == (1, 0, 0) and snapshot.gate.startswith('open:')
        # GPU is free, llama is down, admission is still ours: restore LLM.
        stopped = ((snapshot.llama, snapshot.sd, snapshot.qwen) == (0, 0, 0)
                   and snapshot.gate in (self.closed(), self.verify_gate()))
        if not healthy and not stopped:
            self.log('recover ignored: observed state is not safe')
            return
        self.state.update(pending=None, reason=None)
        self.state.pop('probe_at', None)
        if healthy:
            self.complete_window()
        else:
            self.enter('stopping-sd')

    def run_once(self):
        try:
            if self.state['phase'] == 'failed':
                self.on_failed()
            elif self.state['pending']:
                self.settle()
            else:
                getattr(self, 'on_' + self.state['phase'].replace('-', '_'))()
        except Unavailable as error:
            self.log('observation unavailable: ' + str(error))
        except Stop as error:
            self.fail(str(error))
        except Exception as error:  # pylint: disable=broad-except
            self.fail('internal error: ' + type(error).__name__)


def main():
    os.umask(0o077)
    state, shared = ROOT / 'sd-switch-state', ROOT / 'sd-switch'
    state.mkdir(exist_ok=True)
    shared.mkdir(exist_ok=True)
    try:
        lock = acquire_owner(state / 'owner.lock')
    except OSError:
        print('another switcher owns this node', file=sys.stderr, flush=True)
        return 1
    switcher = Switcher(state, shared, Cluster(FileTransport()))
    while lock:
        switcher.run_once()
        time.sleep(TICK)
    return 0


if __name__ == '__main__':
    sys.exit(main())
