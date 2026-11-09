"""State machine tests with fakes: no sleep, cluster, GPU or network."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

path = Path(__file__).resolve().parents[2] / 'argoproj/local-llm/runtime_switch_worker.py'
spec = importlib.util.spec_from_file_location('runtime_switch_worker', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

IDLE = (200, json.dumps({'progress': 0, 'state': {'job': '', 'job_count': 0}}).encode())
BUSY = (200, json.dumps({'progress': .5, 'state': {'job': 'txt2img', 'job_count': 1}}).encode())


class FakeCluster:
    def __init__(self):
        self.llama, self.sd, self.qwen, self.gate = 1, 0, 0, 'open:base'
        self.writes = []
        self.fail_with = None
        self.crash_after_write = False
        self.lagging = False

    def snapshot(self):
        objects = {name: {'metadata': {'uid': name, 'resourceVersion': '1'}, 'spec': {'replicas': 0}}
                   for name in ('llama', 'sd', 'qwen', 'gate')}
        running = (0 if self.lagging is False and self.llama == 0 else self.llama, self.sd)
        return module.Snapshot(self.llama, self.sd, self.qwen, self.gate, *running, objects)

    def patch(self, field, snapshot, value):
        if self.fail_with:
            raise self.fail_with
        self.writes.append((field, value))
        setattr(self, field, value)
        if self.crash_after_write:
            raise module.Unavailable('observer lost after write')


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state, self.shared = Path(self.temp.name, 'state'), Path(self.temp.name, 'shared')
        self.state.mkdir()
        self.shared.mkdir()
        self.now = 1000.0
        self.cluster = FakeCluster()
        self.progress = IDLE
        self.active = 0
        self.processing = False
        self.slots_status = 200
        self.probes = 0
        self.probe_ok = True
        self.switcher = self.make()

    def make(self):
        return module.Switcher(self.state, self.shared, self.cluster, self.http, lambda: self.now, lambda _: None)

    def http(self, url, method='GET', data=None, timeout=10):
        if url.endswith('/__board_pilot__/admission'):
            return 200, (self.cluster.gate + '\n').encode()
        if url.endswith('/stats?format=json'):
            return 200, json.dumps({'stats': [
                {'name': 'cluster.llama_server.upstream_rq_active', 'value': self.active},
                {'name': 'cluster.llama_server.upstream_rq_pending_active', 'value': 0}]}).encode()
        if url.endswith('/__board_pilot__/models'):
            return 200, b'{"data":[{"id":"gemma4-26b","status":{"value":"loaded"}}]}'
        if '/__board_pilot__/slots' in url:
            return self.slots_status, json.dumps([{'is_processing': self.processing}]).encode()
        if url.endswith('/sdapi/v1/progress'):
            return self.progress
        if url.endswith('/health'):
            return 200, b'ok'
        if url.endswith('/restore-probe'):
            self.probes += 1
            if self.probe_ok:
                return 200, b'{"choices":[{"message":{"content":"OK"}}]}'
            return 503, b'{}'
        raise AssertionError(url)

    @property
    def phase(self):
        return self.switcher.state['phase']

    def intent(self):
        (self.shared / 'intent.json').write_text('{}')

    def job(self, identity, state):
        directory = self.shared / 'jobs' / (identity * 32)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'status.json').write_text(json.dumps({'state': state}))

    def advance(self, seconds=20):
        self.now += seconds
        self.switcher.run_once()

    def until(self, phase, limit=200, step=20):
        for _ in range(limit):
            if self.phase == phase:
                return
            self.advance(step)
        self.fail('stuck in ' + self.phase + ' waiting for ' + phase)


class HappyPath(Base):
    def test_full_order_and_final_state(self):
        self.intent()
        self.until('image-ready')
        self.assertEqual((self.cluster.llama, self.cluster.sd), (0, 1))
        self.assertFalse((self.shared / 'intent.json').exists())
        self.assertEqual((self.shared / 'phase').read_text().strip(), 'image-ready')
        self.until('llm-open', step=30)
        token = self.switcher.state['token']
        self.assertEqual(self.cluster.writes, [
            ('gate', 'closed:' + token), ('llama', 0), ('sd', 1), ('sd', 0),
            ('gate', 'verify:' + token), ('llama', 1), ('gate', 'open:' + token)])
        self.assertEqual((self.cluster.llama, self.cluster.sd, self.cluster.gate), (1, 0, 'open:' + token))
        self.assertEqual(self.probes, 1)

    def test_no_demand_no_api_calls(self):
        self.cluster.snapshot = lambda: self.fail('observed without demand')
        self.advance()

    def test_failed_finished_jobs_are_not_demand(self):
        self.job('a', 'unknown')
        self.job('b', 'done')
        self.advance()
        self.assertEqual(self.phase, 'llm-open')

    def test_queued_job_starts_window_after_cooldown(self):
        self.switcher.state['cooldown_until'] = self.now + 100
        self.job('a', 'queued')
        self.advance(1)
        self.assertEqual(self.cluster.writes, [])
        self.advance(100)
        self.assertEqual(self.phase, 'draining')


class Exclusion(Base):
    def test_foreign_gate_or_replicas_refuse(self):
        self.intent()
        for gate, llama, qwen in (('closed:other', 1, 0), ('verify:other', 1, 0), ('open:x', 0, 0), ('open:x', 1, 1)):
            self.cluster.gate, self.cluster.llama, self.cluster.qwen = gate, llama, qwen
            self.advance()
            self.assertEqual((self.phase, self.cluster.writes), ('llm-open', []))

    def test_gate_conflict_stays_open_without_failing(self):
        self.intent()
        self.cluster.fail_with = module.Conflict('gate')
        self.advance()
        self.assertEqual(self.phase, 'llm-open')
        self.assertIsNone(self.switcher.state['pending'])

    def test_second_owner_refused(self):
        first = module.acquire_owner(self.state / 'owner.lock')
        self.addCleanup(first.close)
        with self.assertRaises(OSError):
            module.acquire_owner(self.state / 'owner.lock')


class Drain(Base):
    def test_active_request_never_stopped(self):
        self.intent()
        self.active = 1
        for _ in range(40):
            self.advance(30)
        self.assertEqual(self.phase, 'draining')
        self.assertEqual(self.cluster.llama, 1)

    def test_unobservable_slots_do_not_count_as_idle(self):
        self.slots_status = 503
        self.intent()
        self.until('draining')
        for _ in range(12):
            self.advance(20)
        self.assertEqual(self.phase, 'draining')
        self.assertEqual(self.cluster.llama, 1)
        self.slots_status = 200
        self.until('stopping-llm', step=20)

    def test_processing_slot_resets_stability(self):
        self.intent()
        self.advance()
        self.advance()
        self.processing = True
        self.advance()
        self.processing = False
        self.advance()
        self.assertEqual(self.phase, 'draining')

    def test_drain_timeout_reopens_without_stopping_llama(self):
        self.intent()
        self.active = 1
        self.advance()
        self.advance(module.DRAIN_LIMIT + 1)
        self.until('llm-open')
        self.assertEqual(self.cluster.llama, 1)
        self.assertNotIn(('llama', 0), self.cluster.writes)
        self.assertGreaterEqual(self.switcher.state['cooldown_until'] - self.now, module.COOLDOWN)
        self.assertTrue((self.shared / 'intent.json').exists())


class Window(Base):
    def ready(self):
        self.intent()
        self.until('image-ready')

    def test_busy_generation_is_not_stopped(self):
        self.ready()
        self.progress = BUSY
        for _ in range(60):
            self.advance(60)
        self.assertEqual(self.cluster.sd, 1)
        self.assertNotIn(('sd', 0), self.cluster.writes)
        self.assertEqual(self.phase, 'image-draining')

    def test_probe_failure_is_not_idle(self):
        self.ready()
        self.progress = (500, b'')
        for _ in range(10):
            self.advance(60)
        self.assertEqual(self.cluster.sd, 1)

    def test_unobservable_backend_fails_closed(self):
        self.ready()
        self.progress = (500, b'')
        for _ in range(100):
            self.advance(60)
        self.assertEqual(self.phase, 'failed')
        self.assertEqual(self.cluster.sd, 1)
        self.assertTrue(self.cluster.gate.startswith('closed:'))

    def test_missing_job_count_is_not_idle(self):
        self.ready()
        self.progress = (200, b'{"progress":0,"state":{}}')
        self.advance(module.IDLE_RELEASE + 60)
        self.assertEqual(self.phase, 'image-ready')

    def test_window_cap_keeps_queued_jobs(self):
        self.ready()
        self.job('a', 'queued')
        self.advance(module.WINDOW_LIMIT + 1)
        self.assertEqual(self.phase, 'image-draining')
        self.assertEqual((self.shared / 'phase').read_text().strip(), 'image-draining')
        self.until('llm-open', step=30)
        self.assertEqual(self.switcher.jobs()['queued'], 1)
        self.assertGreater(self.switcher.state['cooldown_until'], self.now)

    def test_dispatched_job_blocks_stop(self):
        self.ready()
        self.job('a', 'dispatched')
        self.advance(module.WINDOW_LIMIT + 1)
        for _ in range(10):
            self.advance(20)
        self.assertEqual(self.cluster.sd, 1)
        (self.shared / 'jobs' / ('a' * 32) / 'status.json').write_text('{"state":"unknown"}')
        self.until('llm-open', step=30)

    def test_start_timeout_returns_to_llm_with_long_cooldown(self):
        self.progress = (503, b'')
        self.intent()
        self.until('starting-sd')
        self.advance(module.START_LIMIT + 1)
        self.until('llm-open', step=30)
        self.assertEqual((self.cluster.llama, self.cluster.sd), (1, 0))
        self.assertGreater(self.switcher.state['cooldown_until'] - self.now, module.COOLDOWN)

    def test_llm_restore_failure_stays_closed(self):
        self.probe_ok = False
        self.ready()
        self.until('verifying', step=30)
        self.until('failed', step=60)
        self.assertTrue(self.cluster.gate.startswith('verify:'))
        self.assertLessEqual(self.probes, module.START_LIMIT // module.PROBE_SPACING + 1)


class Progress(Base):
    def test_residual_progress_after_a_job_is_idle(self):
        self.progress = (200, json.dumps({'progress': 1.0, 'state': {'job': '', 'job_count': 0, 'sampling_step': 20}}).encode())
        self.assertIs(self.switcher.sd_busy(), False)

    def test_running_job_is_busy(self):
        self.progress = (200, json.dumps({'progress': 0, 'state': {'job': 'txt2img', 'job_count': 1}}).encode())
        self.assertIs(self.switcher.sd_busy(), True)


class Recovery(Base):
    def test_every_pending_write_is_persisted_before_the_api_call(self):
        seen = []
        original = self.cluster.patch

        def patch(field, snapshot, value):
            stored = json.loads((self.state / 'state.json').read_text())
            seen.append((stored['pending']['field'], stored['pending']['value'], field, value))
            original(field, snapshot, value)
        self.cluster.patch = patch
        self.intent()
        self.until('image-ready')
        self.until('llm-open', step=30)
        self.assertEqual(len(seen), 7)
        self.assertTrue(all(a == c and b == d for a, b, c, d in seen))

    def test_restart_with_applied_write_settles(self):
        self.intent()
        self.cluster.crash_after_write = True
        self.advance()
        self.cluster.crash_after_write = False
        self.assertEqual(self.switcher.state['pending']['next'], 'draining')
        count = len(self.cluster.writes)
        self.switcher = self.make()
        self.advance()
        self.assertEqual(self.phase, 'draining')
        self.assertIsNone(self.switcher.state['pending'])
        self.assertEqual(len(self.cluster.writes), count)

    def test_restart_with_unapplied_write_fails_and_never_replays(self):
        self.intent()
        self.cluster.fail_with = module.Unavailable('crash before write')
        self.advance()
        self.cluster.fail_with = None
        self.switcher = self.make()
        self.advance()
        self.assertEqual(self.phase, 'failed')
        self.assertEqual(self.cluster.writes, [])
        self.assertEqual(self.cluster.gate, 'open:base')

    def test_unknown_write_is_failed_and_sticky(self):
        self.intent()
        self.cluster.fail_with = module.Stop('write outcome unknown')
        self.advance()
        self.assertEqual(self.phase, 'failed')
        self.cluster.fail_with = None
        for _ in range(10):
            self.advance()
        self.assertEqual((self.phase, self.cluster.writes), ('failed', []))
        self.assertEqual((self.shared / 'phase').read_text().strip(), 'failed')

    def test_drift_fails_closed(self):
        self.intent()
        self.until('draining')
        self.cluster.gate = 'closed:someone-else'
        self.advance()
        self.assertEqual(self.phase, 'failed')

    def test_observation_outage_changes_nothing(self):
        self.intent()
        self.until('draining')

        def unavailable():
            raise module.Unavailable('api')
        original, self.cluster.snapshot = self.cluster.snapshot, unavailable
        self.advance()
        self.cluster.snapshot = original
        self.assertEqual(self.phase, 'draining')

    def failed_with_token(self):
        self.intent()
        self.until('draining')
        self.cluster.gate = 'closed:someone-else'
        self.advance()
        token = self.switcher.state['token']
        self.assertEqual(self.phase, 'failed')
        return token

    def test_recover_requires_exact_token_and_safe_state(self):
        token = self.failed_with_token()
        (self.state / 'recover').write_text('wrong')
        self.advance()
        self.assertEqual(self.phase, 'failed')
        self.assertFalse((self.state / 'recover').exists())
        (self.state / 'recover').write_text(token)
        self.advance()
        self.assertEqual(self.phase, 'failed')  # foreign gate is not safe
        self.cluster.gate = 'open:restored'
        (self.state / 'recover').write_text(token)
        self.advance()
        self.assertEqual(self.phase, 'llm-open')

    def test_recover_can_resume_llm_restore_only_when_gpu_is_free(self):
        token = self.failed_with_token()
        self.cluster.llama, self.cluster.sd, self.cluster.gate = 0, 1, 'closed:' + token
        (self.state / 'recover').write_text(token)
        self.advance()
        self.assertEqual(self.phase, 'failed')
        self.cluster.sd = 0
        (self.state / 'recover').write_text(token)
        self.advance()
        self.assertEqual(self.phase, 'stopping-sd')
        self.until('llm-open', step=30)
        self.assertEqual(self.cluster.llama, 1)


if __name__ == '__main__':
    unittest.main()
