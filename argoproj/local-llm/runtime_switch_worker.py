"""Durable llama <-> image backend switcher (BOXP-484, BOXP-498).

One window lends the single GPU to exactly one image backend: SD.Next (`sd`) or
ComfyUI (`comfy`). The phase names keep their BOXP-484 spelling for both.

Holds no credential: every Kubernetes call goes through the existing file
transport in /runtime-api, whose worker enforces the target allowlist and the
conditional patch shape. The gate compare-and-set is the mutex against other
GPU owners. An intended write is persisted first, a write with an unknown
outcome is never replayed, and any doubt ends in the sticky `failed` phase with
admission still closed.

The private state directory is not visible to the internet-facing gateways. The
SD gateway only shares /runtime-api/sd-switch (phase, jobs, intent, activity) and
the ComfyUI gateway only /runtime-api/comfy-switch (phase, intent).
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
COMFY = 'http://comfyui-backend.stable-diffusion.svc.cluster.local:8188'
SCALE = '/apis/apps/v1/namespaces/{}/deployments/{}/scale'
PATHS = {'llama': SCALE.format('local-llm', 'llama-server'),
         'sd': SCALE.format('stable-diffusion', 'stable-diffusion-webui'),
         'qwen': SCALE.format('stable-diffusion', 'qwen-image-eval'),
         'comfy': SCALE.format('stable-diffusion', 'comfyui'),
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
# A video job runs for a long time and its author composes the next prompt in
# the UI, so ComfyUI keeps the GPU longer than SD.Next before an idle release.
COMFY_IDLE_RELEASE = 900
# The first start pulls a multi-gigabyte image.
COMFY_START_LIMIT = 2400
COMFY_WINDOW_LIMIT = 4 * 3600
BACKENDS = ('sd', 'comfy')
STATUS_LIMIT = 65536

Snapshot = collections.namedtuple('Snapshot', 'llama sd qwen gate llama_running sd_running objects comfy comfy_running',
                                  defaults=(0, 0))


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
        replicas = [objects[name].get('spec', {}).get('replicas', 0) for name in ('llama', 'sd', 'qwen', 'comfy')]
        running = [objects[name].get('status', {}).get('replicas', 0) for name in ('llama', 'sd', 'comfy')]
        gate = objects['gate'].get('data', {}).get('state')
        if not isinstance(gate, str) or any(type(value) is not int or value not in (0, 1) for value in replicas):
            raise Stop('unexpected cluster value')
        return Snapshot(*replicas[:3], gate, *running[:2], objects, replicas[3], running[2])

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
    def __init__(self, state, shared, cluster, http_call=http, clock=time.time, log=None, comfy_shared=None):
        self.directory, self.shared = Path(state), Path(shared)
        # Without its shared directory ComfyUI can neither ask for nor get a window.
        self.comfy_shared = Path(comfy_shared) if comfy_shared else None
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

    def backend(self):
        """The image backend that owns the current or the most recent window."""
        return self.state.get('backend', 'sd')

    def publish(self, phase=None):
        phase = phase or self.state['phase']
        # Only the owner of the window sees its phases, so the other gateway can
        # never route to a backend that is not running.
        other = phase if phase in ('llm-open', 'failed') else 'busy'
        owner = self.backend()
        atomic(self.shared / 'phase', ((phase if owner == 'sd' else other) + '\n').encode())
        if self.comfy_shared:
            atomic(self.comfy_shared / 'phase', ((phase if owner == 'comfy' else other) + '\n').encode())

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
        return {'llama': snapshot.llama, 'sd': snapshot.sd, 'comfy': snapshot.comfy, 'gate': snapshot.gate}

    def expect(self, snapshot, llama, image, gates):
        """`image` is the replica count of the window owner; every other GPU user must be 0."""
        owner = self.backend()
        foreign = snapshot.qwen != 0 or any(getattr(snapshot, name) != 0 for name in BACKENDS if name != owner)
        if foreign or snapshot.llama not in llama or getattr(snapshot, owner) != image or snapshot.gate not in gates:
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
        observed = self.triple(self.cluster.snapshot())
        # A write persisted before BOXP-498 has no `comfy` key; it must still be 0.
        if observed != dict({'comfy': 0}, **pending['desired']):
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
        """The backend to open a window for, or None."""
        wanted = []
        if self.jobs()['queued'] > 0 or (self.shared / 'intent.json').exists():
            wanted.append('sd')
        if self.comfy_shared and (self.comfy_shared / 'intent.json').exists():
            wanted.append('comfy')
        if len(wanted) == 2:
            # Take turns, so neither entrance can starve the other.
            wanted.remove(self.backend())
        return wanted[0] if wanted else None

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
                # Any failure, including 503, is not evidence of idleness: an
                # unloaded model simply disappears from the next list.
                slots = json.loads(body) if code == 200 else None
                if not isinstance(slots, list) or not slots or any(slot.get('is_processing') is not False for slot in slots):
                    return False
            return True
        except (ValueError, KeyError, TypeError, AttributeError):
            return False

    def comfy_busy(self):
        """Running or pending prompts keep the GPU; an open browser tab does not."""
        code, body = self.get(COMFY + '/queue')
        if code != 200:
            return None
        try:
            value = json.loads(body)
            running, pending = value['queue_running'], value['queue_pending']
            if not isinstance(running, list) or not isinstance(pending, list):
                return None
            return bool(running or pending)
        except (ValueError, KeyError, TypeError):
            return None

    def image_busy(self):
        return self.comfy_busy() if self.backend() == 'comfy' else self.sd_busy()

    def image_started(self):
        url = COMFY + '/queue' if self.backend() == 'comfy' else SD + '/sdapi/v1/progress'
        return self.get(url)[0] == 200

    def intent_path(self):
        return (self.comfy_shared if self.backend() == 'comfy' else self.shared) / 'intent.json'

    def sd_busy(self):
        """True, False, or None when the backend cannot say. None is not idle."""
        code, body = self.get(SD + '/sdapi/v1/progress')
        if code != 200:
            return None
        try:
            value = json.loads(body)
            # SD.Next leaves the last sampling step behind after end(), so
            # `progress` stays non-zero while idle. Only the job fields are reset.
            state = value['state']
            return state['job_count'] != 0 or bool(state.get('job'))
        except (ValueError, KeyError, TypeError):
            return None

    def track_probe(self, busy, snapshot):
        """A backend that stays unobservable must not hold the GPU forever.

        True when the window was closed here and the caller must not go on.
        """
        if busy is not None:
            self.state.pop('probe_failing_since', None)
            return False
        since = self.state.setdefault('probe_failing_since', self.clock())
        if self.clock() - since < PROBE_FAILURE_LIMIT:
            return False
        if self.backend() != 'comfy':
            raise Stop('image backend unobservable')
        # ComfyUI's liveness probe restarts a silent container long before this
        # limit, so a job that was running is already gone. Leaving LLM down
        # until an operator arrives would protect nothing: stop ComfyUI and let
        # the usual path wait for its pod to end before LLM starts.
        self.state.pop('probe_failing_since', None)
        self.state.update(reason='ComfyUI unobservable; window closed', next_cooldown=START_FAILURE_COOLDOWN)
        self.write(snapshot, 'comfy', 0, 'stopping-sd')
        return True

    def complete_window(self):
        self.state['cooldown_until'] = self.clock() + self.state.pop('next_cooldown', COOLDOWN)
        self.enter('llm-open')

    # Phases

    def on_llm_open(self):
        if self.clock() < self.state['cooldown_until']:
            return
        wanted = self.demand()
        if not wanted:
            return
        snapshot = self.cluster.snapshot()
        if ((snapshot.llama, snapshot.sd, snapshot.qwen, snapshot.comfy) != (1, 0, 0, 0)
                or not snapshot.gate.startswith('open:')):
            return
        self.state['backend'] = wanted
        self.state['token'] = wanted + '-' + uuid.uuid4().hex[:16]
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
            self.write(snapshot, self.backend(), 1, 'starting-sd')

    def on_starting_sd(self):
        snapshot = self.cluster.snapshot()
        self.expect(snapshot, (0,), 1, (self.closed(),))
        if self.image_started():
            self.intent_path().unlink(missing_ok=True)
            self.state.update(window_started=self.clock(), last_activity=self.clock())
            self.enter('image-ready')
        elif self.elapsed() > (COMFY_START_LIMIT if self.backend() == 'comfy' else START_LIMIT):
            # Nothing was generated, so returning the GPU to LLM is safe.
            self.state.update(reason='image backend did not start', next_cooldown=START_FAILURE_COOLDOWN)
            if self.backend() == 'comfy':
                # Nobody waits at a ComfyUI that never came up; asking again is a
                # button press, and LLM must not be stopped again every hour.
                self.intent_path().unlink(missing_ok=True)
            self.write(snapshot, self.backend(), 0, 'stopping-sd')

    def on_image_ready(self):
        if not self.due(15):
            return
        snapshot = self.cluster.snapshot()
        self.expect(snapshot, (0,), 1, (self.closed(),))
        busy = self.image_busy()
        if self.track_probe(busy, snapshot):
            return
        if busy:
            self.state['last_activity'] = self.clock()
        self.save()
        if self.backend() == 'comfy':
            # ComfyUI owns its queue; the SD job store is not part of this window.
            jobs, latest = {'queued': 0, 'dispatched': 0}, self.state['last_activity']
            release, limit = COMFY_IDLE_RELEASE, COMFY_WINDOW_LIMIT
        else:
            jobs, latest = self.jobs(), max(self.state['last_activity'], self.observed_activity())
            release, limit = IDLE_RELEASE, WINDOW_LIMIT
        idle = (not jobs['queued'] and not jobs['dispatched'] and busy is False and self.clock() - latest >= release)
        if idle or self.clock() - self.state['window_started'] >= limit:
            self.enter('image-draining')

    def on_image_draining(self):
        if not self.due():
            return
        snapshot = self.cluster.snapshot()
        self.expect(snapshot, (0,), 1, (self.closed(),))
        busy = self.image_busy()
        if self.track_probe(busy, snapshot):
            return
        self.save()
        dispatched = self.backend() == 'sd' and self.jobs()['dispatched']
        self.stable = self.stable + 1 if busy is False and not dispatched else 0
        if self.stable >= STABLE_NEEDED:
            self.write(snapshot, self.backend(), 0, 'stopping-sd')

    def on_stopping_sd(self):
        snapshot = self.cluster.snapshot()
        self.expect(snapshot, (0,), 0, (self.closed(), self.verify_gate()))
        if self.elapsed() > STOP_LIMIT:
            raise Stop('image backend did not stop')
        if getattr(snapshot, self.backend() + '_running') != 0:
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
        users = (snapshot.llama, snapshot.sd, snapshot.qwen, snapshot.comfy)
        healthy = users == (1, 0, 0, 0) and snapshot.gate.startswith('open:')
        # GPU is free, llama is down, admission is still ours: restore LLM.
        stopped = users == (0, 0, 0, 0) and snapshot.gate in (self.closed(), self.verify_gate())
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
    state, shared, comfy_shared = ROOT / 'sd-switch-state', ROOT / 'sd-switch', ROOT / 'comfy-switch'
    state.mkdir(exist_ok=True)
    shared.mkdir(exist_ok=True)
    comfy_shared.mkdir(exist_ok=True)
    try:
        lock = acquire_owner(state / 'owner.lock')
    except OSError:
        print('another switcher owns this node', file=sys.stderr, flush=True)
        return 1
    switcher = Switcher(state, shared, Cluster(FileTransport()), comfy_shared=comfy_shared)
    while lock:
        switcher.run_once()
        time.sleep(TICK)
    return 0


if __name__ == '__main__':
    sys.exit(main())
