"""ComfyUI as a second image backend of the same switcher (BOXP-498)."""
import importlib.util
import json
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('sd_switch_base', Path(__file__).with_name('test_switcher.py'))
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
module = base.module

EMPTY = (200, b'{"queue_running": [], "queue_pending": []}')
RUNNING = (200, b'{"queue_running": [[0, "id", {}, {}, []]], "queue_pending": []}')
# The request got no HTTP answer at all.
SILENT = (None, b'')
PENDING = (200, b'{"queue_running": [], "queue_pending": [[1, "id", {}, {}, []]]}')


class Cluster(base.FakeCluster):
    def __init__(self):
        super().__init__()
        self.comfy = 0

    def snapshot(self):
        value = super().snapshot()
        return value._replace(comfy=self.comfy, comfy_running=self.comfy,
                              objects=dict(value.objects, comfy=value.objects['sd']))


class ComfyBase(base.Base):
    def setUp(self):
        super().setUp()
        self.comfy_shared = Path(self.temp.name, 'comfy')
        self.comfy_shared.mkdir()
        self.cluster = Cluster()
        self.queue = EMPTY
        self.sd_probes = 0
        self.switcher = self.make()

    def make(self):
        shared = getattr(self, 'comfy_shared', None)  # the base setUp builds one first
        return module.Switcher(self.state, self.shared, self.cluster, self.http, lambda: self.now,
                               lambda _: None, comfy_shared=shared)

    def http(self, url, method='GET', data=None, timeout=10):
        if url == module.COMFY + '/queue':
            return self.queue
        if url.startswith(module.SD):
            self.sd_probes += 1
        return super().http(url, method, data, timeout)

    def comfy_intent(self):
        (self.comfy_shared / 'intent.json').write_text('{}')

    def phases(self):
        return ((self.shared / 'phase').read_text().strip(), (self.comfy_shared / 'phase').read_text().strip())

    def comfy_ready(self):
        self.comfy_intent()
        self.until('image-ready')


class ComfyWindow(ComfyBase):
    def test_full_order_and_final_state(self):
        self.comfy_ready()
        self.assertEqual((self.cluster.llama, self.cluster.sd, self.cluster.comfy), (0, 0, 1))
        self.assertFalse((self.comfy_shared / 'intent.json').exists())
        self.assertEqual(self.phases(), ('busy', 'image-ready'))
        self.until('llm-open', step=30)
        token = self.switcher.state['token']
        self.assertTrue(token.startswith('comfy-'))
        self.assertEqual(self.cluster.writes, [
            ('gate', 'closed:' + token), ('llama', 0), ('comfy', 1), ('comfy', 0),
            ('gate', 'verify:' + token), ('llama', 1), ('gate', 'open:' + token)])
        self.assertEqual((self.cluster.llama, self.cluster.sd, self.cluster.comfy, self.cluster.gate),
                         (1, 0, 0, 'open:' + token))
        self.assertEqual(self.probes, 1)
        self.assertEqual(self.phases(), ('llm-open', 'llm-open'))
        self.assertEqual(self.sd_probes, 0)

    def test_the_other_gateway_never_sees_a_window_phase(self):
        self.comfy_intent()
        seen = set()
        for _ in range(400):
            self.advance(20)
            seen.add(self.phases()[0])
            if self.phase == 'llm-open' and len(seen) > 1:
                break
        self.assertEqual(seen, {'busy', 'llm-open'})

    def test_sd_window_hides_its_phases_from_comfy(self):
        self.intent()
        self.until('image-ready')
        self.assertEqual(self.phases(), ('image-ready', 'busy'))
        self.assertEqual((self.cluster.sd, self.cluster.comfy), (1, 0))

    def test_long_video_job_is_never_stopped(self):
        self.comfy_ready()
        self.queue = RUNNING
        for _ in range(400):
            self.advance(60)
        self.assertGreater(self.now - self.switcher.state['window_started'], module.COMFY_WINDOW_LIMIT)
        self.assertEqual(self.phase, 'image-draining')
        self.assertEqual(self.cluster.comfy, 1)
        self.assertNotIn(('comfy', 0), self.cluster.writes)
        self.queue = EMPTY
        self.until('llm-open', step=30)
        self.assertEqual((self.cluster.llama, self.cluster.comfy), (1, 0))

    def test_pending_prompt_keeps_the_gpu(self):
        self.comfy_ready()
        self.queue = PENDING
        for _ in range(60):
            self.advance(60)
        self.assertEqual((self.phase, self.cluster.comfy), ('image-ready', 1))

    def test_open_tab_without_a_prompt_releases_after_the_idle_time(self):
        self.comfy_ready()
        self.advance(module.COMFY_IDLE_RELEASE - 60)
        self.assertEqual(self.phase, 'image-ready')
        self.advance(120)
        self.assertEqual(self.phase, 'image-draining')

    def test_job_longer_than_the_sd_idle_time_is_not_released(self):
        self.comfy_ready()
        self.queue = RUNNING
        self.advance(module.IDLE_RELEASE * 3)
        self.queue = EMPTY
        self.advance(module.IDLE_RELEASE + 60)
        self.assertEqual(self.phase, 'image-ready')

    def test_unreadable_queue_is_not_idle_and_fails_closed(self):
        self.comfy_ready()
        for answer in ((500, b''), (200, b'not json'), (200, b'{"queue_running": []}'), (200, b'{"queue_running": 0, "queue_pending": 0}')):
            self.queue = answer
            self.assertIsNone(self.switcher.comfy_busy())
        for _ in range(100):
            self.advance(60)
        self.assertEqual(self.phase, 'failed')
        self.assertEqual(self.cluster.comfy, 1)
        self.assertTrue(self.cluster.gate.startswith('closed:'))
        self.assertEqual(self.phases(), ('failed', 'failed'))

    def test_one_unreadable_answer_among_silence_fails_closed(self):
        self.comfy_ready()
        self.queue = SILENT
        self.advance(600)
        self.queue = (500, b'')
        self.advance(60)
        self.queue = SILENT
        for _ in range(60):
            self.advance(60)
        self.assertEqual((self.phase, self.cluster.comfy), ('failed', 1))

    def test_silent_backend_is_not_idle_and_returns_the_gpu_to_llm(self):
        self.comfy_ready()
        self.queue = SILENT
        self.assertIsNone(self.switcher.comfy_busy())
        # Not idle: the window outlives the idle release while the queue is unreadable.
        self.advance(module.COMFY_IDLE_RELEASE + 120)
        self.assertEqual((self.phase, self.cluster.comfy), ('image-ready', 1))
        self.advance(module.PROBE_FAILURE_LIMIT)
        self.until('llm-open', step=30)
        token = self.switcher.state['token']
        # ComfyUI is stopped before LLM starts, and LLM answers before admission opens.
        self.assertEqual(self.cluster.writes[-4:], [
            ('comfy', 0), ('gate', 'verify:' + token), ('llama', 1), ('gate', 'open:' + token)])
        self.assertEqual((self.cluster.llama, self.cluster.comfy, self.cluster.gate), (1, 0, 'open:' + token))
        self.assertEqual(self.probes, 1)
        self.assertEqual(self.switcher.state['reason'], 'ComfyUI unobservable; window closed')
        # No new window during the cooldown, even if the button is pressed again.
        self.comfy_intent()
        writes = len(self.cluster.writes)
        self.advance(module.START_FAILURE_COOLDOWN - 300)
        self.assertEqual((self.phase, len(self.cluster.writes)), ('llm-open', writes))

    def test_silent_backend_while_draining_returns_the_gpu_to_llm(self):
        self.comfy_ready()
        self.queue = RUNNING
        self.advance(module.COMFY_WINDOW_LIMIT + 60)
        self.assertEqual(self.phase, 'image-draining')
        self.queue = SILENT
        self.advance(60)
        self.advance(module.PROBE_FAILURE_LIMIT - 120)
        self.assertEqual((self.phase, self.cluster.comfy), ('image-draining', 1))
        self.advance(240)
        self.until('llm-open', step=30)
        self.assertEqual((self.cluster.llama, self.cluster.comfy), (1, 0))

    def test_backend_restart_during_a_window_recovers(self):
        self.comfy_ready()
        self.queue = (503, b'')
        for _ in range(5):
            self.advance(60)
        self.assertEqual(self.phase, 'image-ready')
        self.queue = EMPTY
        self.until('llm-open', step=30)
        self.assertEqual((self.cluster.llama, self.cluster.comfy), (1, 0))

    def test_start_timeout_returns_to_llm_and_forgets_the_request(self):
        self.queue = (503, b'')
        self.comfy_intent()
        self.until('starting-sd')
        self.advance(module.START_LIMIT + 1)
        self.assertEqual(self.phase, 'starting-sd')
        self.advance(module.COMFY_START_LIMIT)
        self.until('llm-open', step=30)
        self.assertEqual((self.cluster.llama, self.cluster.comfy), (1, 0))
        self.assertFalse((self.comfy_shared / 'intent.json').exists())
        writes = len(self.cluster.writes)
        self.advance(module.START_FAILURE_COOLDOWN + 60)
        self.assertEqual((self.phase, len(self.cluster.writes)), ('llm-open', writes))

    def test_restart_inside_a_window_keeps_the_owner(self):
        self.comfy_ready()
        self.queue = RUNNING
        self.switcher = self.make()
        self.assertEqual(self.phases(), ('busy', 'image-ready'))
        self.advance(60)
        self.assertEqual((self.phase, self.cluster.comfy, self.cluster.sd), ('image-ready', 1, 0))
        self.queue = EMPTY
        self.until('llm-open', step=30)
        self.assertEqual(self.cluster.writes.count(('comfy', 1)), 1)


class ComfyExclusion(ComfyBase):
    def test_no_window_while_another_gpu_user_exists(self):
        for field in ('sd', 'qwen', 'comfy'):
            self.setUp()
            self.comfy_intent()
            setattr(self.cluster, field, 1)
            self.advance()
            self.assertEqual((self.phase, self.cluster.writes), ('llm-open', []), field)

    def test_sd_appearing_in_a_comfy_window_fails_closed(self):
        self.comfy_ready()
        self.cluster.sd = 1
        self.advance(60)
        self.assertEqual(self.phase, 'failed')
        self.assertNotIn(('llama', 1), self.cluster.writes)

    def test_comfy_appearing_in_an_sd_window_fails_closed(self):
        self.intent()
        self.until('image-ready')
        self.cluster.comfy = 1
        self.advance(60)
        self.assertEqual(self.phase, 'failed')

    def test_both_requests_take_turns_and_never_overlap(self):
        self.intent()
        self.comfy_intent()
        owners = []
        for _ in range(600):
            self.advance(20)
            self.assertLessEqual(self.cluster.llama + self.cluster.sd + self.cluster.comfy, 1)
            if self.phase == 'image-ready' and (not owners or owners[-1] != self.switcher.backend()):
                owners.append(self.switcher.backend())
            if len(owners) == 2 and self.phase == 'llm-open':
                break
        self.assertEqual(sorted(owners), ['comfy', 'sd'])
        self.assertEqual((self.cluster.llama, self.cluster.sd, self.cluster.comfy), (1, 0, 0))

    def test_disabled_without_a_shared_directory(self):
        switcher = module.Switcher(self.state, self.shared, self.cluster, self.http, lambda: self.now, lambda _: None)
        self.comfy_intent()
        self.assertIsNone(switcher.demand())

    def test_recover_refuses_while_comfy_is_scaled_up(self):
        self.comfy_ready()
        self.cluster.gate = 'closed:someone-else'
        self.advance(60)
        token = self.switcher.state['token']
        self.cluster.llama, self.cluster.gate = 0, 'closed:' + token
        (self.state / 'recover').write_text(token)
        self.advance()
        self.assertEqual(self.phase, 'failed')
        self.cluster.comfy = 0
        (self.state / 'recover').write_text(token)
        self.advance()
        self.until('llm-open', step=30)
        self.assertEqual((self.cluster.llama, self.cluster.comfy), (1, 0))


class Compatibility(ComfyBase):
    def test_pending_write_from_before_the_upgrade_settles(self):
        self.intent()
        self.cluster.crash_after_write = True
        self.advance()
        self.cluster.crash_after_write = False
        stored = json.loads((self.state / 'state.json').read_text())
        del stored['pending']['desired']['comfy']
        stored.pop('backend')
        (self.state / 'state.json').write_text(json.dumps(stored))
        self.switcher = self.make()
        self.advance()
        self.assertEqual(self.phase, 'draining')

    def test_old_pending_write_does_not_hide_a_running_comfy(self):
        self.intent()
        self.cluster.crash_after_write = True
        self.advance()
        self.cluster.crash_after_write = False
        stored = json.loads((self.state / 'state.json').read_text())
        del stored['pending']['desired']['comfy']
        (self.state / 'state.json').write_text(json.dumps(stored))
        self.cluster.comfy = 1
        self.switcher = self.make()
        self.advance()
        self.assertEqual(self.phase, 'failed')


if __name__ == '__main__':
    unittest.main()
