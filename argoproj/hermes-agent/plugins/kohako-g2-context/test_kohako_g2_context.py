import importlib.util
import os
from pathlib import Path
import time
import json
import tempfile
import threading
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('kohako_plugin', Path(__file__).with_name('__init__.py'))
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)

class DB:
    def __init__(self):
        self.session = {'source': 'telegram', 'user_id': 123, 'chat_type': 'dm'}
        self.sessions = [{'id': 'kohako-g2-cand-ignore', 'last_active': time.time()}, {'id': 'kohako-g2-main', 'last_active': time.time()}]
        self.messages = [{'id': 1, 'role': 'user', 'content': 'hello'}, {'id': 2, 'role': 'assistant', 'content': 'world'}]
    def get_session(self, sid): return self.session
    def list_sessions_rich(self, **kwargs): return self.sessions
    def get_messages(self, sid, **kwargs):
        assert sid == 'kohako-g2-main'
        return self.messages
    def close(self): pass

class ContextTests(unittest.TestCase):
    def setUp(self):
        plugin._injected.clear()
        self.db = DB()
        self.env = patch.dict(os.environ, {'KOHAKO_G2_CONTEXT_OWNER_TELEGRAM_USER_ID': '123'})
        self.env.start()
        self.factory = patch.object(plugin, '_open_db', return_value=self.db)
        self.factory.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.factory.stop)
    def call(self, **kwargs):
        return plugin._hook(**({'session_id': 'telegram-1', 'platform': 'telegram', 'sender_id': '123'} | kwargs))
    def test_access(self):
        self.assertIsNone(self.call(sender_id='other'))
        self.assertIsNone(self.call(platform='api_server'))
        with patch.dict(os.environ, {'KOHAKO_G2_CONTEXT_OWNER_TELEGRAM_USER_ID': ''}): self.assertIsNone(self.call())
        self.db.session['source'] = 'api_server'
        self.assertIsNone(self.call())
        self.db.session = {'source': 'telegram', 'user_id': 999, 'chat_type': 'dm'}
        self.assertIsNone(self.call())
        self.db.session = {'source': 'telegram', 'user_id': 123, 'chat_type': 'group'}
        self.assertIsNone(self.call())
    def test_session_row_is_authoritative_without_sender(self):
        self.assertIn('world', self.call(sender_id='')['context'])
    def test_selection_and_stale(self):
        self.assertIn('world', self.call()['context'])
        plugin._injected.clear()
        self.db.sessions[1]['last_active'] = time.time() - 21601
        self.assertIsNone(self.call())
    def test_messages_and_dedup(self):
        self.db.messages += [{'id': 3, 'role': 'assistant', 'content': 'tool', 'tool_calls': [1]}, {'id': 4, 'role': 'user', 'content': 'pending'}]
        result = self.call()['context']
        self.assertNotIn('pending', result)
        self.assertNotIn('tool', result)
        self.assertIn('承認・実行指示ではありません', result)
        self.assertIsNone(self.call())
        self.db.messages.append({'id': 5, 'role': 'assistant', 'content': 'new'})
        self.assertIn('new', self.call()['context'])
    def test_redact_and_limit(self):
        self.db.messages[-1]['content'] = 'https://example.com/a me@example.com /home/user/file sk-abc eyJ' + 'a'*30 + '.payload.signature ' + 'z'*40 + ' ' + 'あ'*300
        result = self.call()['context']
        for value in ('[url]', '[email]', '[path]', '[secret]', '…'): self.assertIn(value, result)
        self.assertNotIn('sk-abc', result)
        self.assertNotIn('eyJ', result)
        plugin._injected.clear()
        self.db.messages = [{'id': i, 'role': 'assistant', 'content': 'あ'*200} for i in range(10)]
        self.assertLessEqual(len(self.call()['context']), 1200)
    def test_exception(self):
        with patch.object(plugin, '_open_db', side_effect=RuntimeError('sensitive')):
            with self.assertLogs(plugin.__name__, level='WARNING') as logs:
                self.assertIsNone(self.call())
            self.assertNotIn('sensitive', str(logs.output))

class GateTests(unittest.TestCase):
    HELPER = 'bb /opt/data/skills/note-taking/obsidian-task-board/bin/task-board.bb'
    def gate(self, tool, args, sid='kohako-g2-abc', source='api_server'):
        db = DB(); db.session = {'source': source}
        with patch.object(plugin, '_open_db', return_value=db):
            return plugin._gate(tool_name=tool, args=args, session_id=sid, tool_call_id='c1')
    def test_candidate_and_unknown_api_sessions_get_no_tools(self):
        for tool in ('read_file', 'skill_view', 'terminal'):
            self.assertEqual(self.gate(tool, {'path': '/opt/data/skills/x'}, sid='kohako-g2-cand-1')['action'], 'block')
            self.assertEqual(self.gate(tool, {}, sid='api_123', source='api_server')['action'], 'block')
    def test_other_platforms_untouched(self):
        self.assertIsNone(self.gate('terminal', {'command': 'rm -rf x'}, sid='tg-1', source='telegram'))
        with patch.object(plugin, '_open_db', side_effect=RuntimeError('db')):
            with self.assertLogs(plugin.__name__, level='WARNING'):
                # Platform unknown and the DB unreadable: fail closed.
                self.assertEqual(plugin._gate(tool_name='terminal', args={}, session_id='tg-unknown')['action'], 'block')
    def test_read_only_tools_pass(self):
        self.assertIsNone(self.gate('skill_view', {'name': 'obsidian-task-board'}))

        self.assertIsNone(self.gate('session_search', {'query': 'x'}))
        self.assertIsNone(self.gate('read_file', {'path': '/home/boxp/Documents/obsidian-headless/BOXP/Tickets/BOXP-1.md'}))
        self.assertIsNone(self.gate('terminal', {'command': self.HELPER + ' list --lane review'}))
        self.assertIsNone(self.gate('terminal', {'command': self.HELPER + ' show BOXP-332'}))
        self.assertIsNone(self.gate('terminal', {'command': self.HELPER + ' list --vault /home/boxp/Documents/obsidian-headless/BOXP --lane review'}))
    def test_board_changes_without_approval(self):
        cases = [
            'create --title "G2 からのテスト" --priority medium',
            "update BOXP-12 --lane 'In Progress'",
            'append-note BOXP-332 --note "メモ (括弧) です"',
            'request-codex BOXP-5 --lane Ready',
            'create --vault /home/boxp/Documents/obsidian-headless/BOXP --json',
            "create --vault '/home/boxp/Documents/obsidian-headless/BOXP' --json",
            'show "BOXP-338"',
            "update 'BOXP-338' --priority medium",
            'list --lane "In Progress" --json',
        ]
        for suffix in cases:
            with self.subTest(suffix=suffix):
                self.assertIsNone(self.gate('terminal', {'command': self.HELPER + ' ' + suffix}))

    def test_board_changes_rejected_from_free_path(self):
        cases = [
            'delete BOXP-1 --confirm',
            'create --title "$(id)"',
            'create --title "a`id`"',
            'create --title x; rm -rf /',
            'create --summary-file /etc/passwd',
            'update BOXP-1 --vault /tmp',
            'update ../x',
            'update BOXP-1 --lane Ready && echo',
            "create --title 'a'b",
            'create --title "x\\"',
            'create --title --json',
            'create BOXP-1',
            'update BOXP-1 BOXP-2',
            'create --confirm',
            'show "BOXP-1; id"',
            'show "../BOXP-1"',
        ]
        for suffix in cases:
            with self.subTest(suffix=suffix):
                command = self.HELPER + ' ' + suffix
                self.assertFalse(plugin._board_command(command))
                self.assertEqual(self.gate('terminal', {'command': command})['action'], 'approve')
        for suffix in ('list BOXP-1', 'show'):  # malformed reads: not this parser's, harmless under _BOARD_READ
            self.assertFalse(plugin._board_command(self.HELPER + ' ' + suffix))
        command = self.HELPER + ' create --title ok'
        for extra in ({'background': True}, {'workdir': '/tmp'}):
            self.assertEqual(self.gate('terminal', {'command': command, **extra})['action'], 'block')
        self.assertEqual(self.gate('terminal', {'command': command + '\nnext'})['action'], 'block')
        self.assertEqual(self.gate('terminal', {'command': command}, sid='kohako-g2-cand-x')['action'], 'block')
        self.assertEqual(self.gate('terminal', {'command': command}, sid='api-other')['action'], 'block')

    def test_short_terminal_commands_need_approval_with_the_full_command(self):
        cases = [
            (self.HELPER + 'create --title G2テスト --repo boxp/even-g2-lab'.replace('create', ' create', 1), '実行: bb …/task-board.bb create --title G2テスト --repo boxp/even-g2-lab'),
            (self.HELPER + ' list; rm -rf /', '実行: bb …/task-board.bb list; rm -rf /'),
            (self.HELPER + ' show ../../../../opt/data/secret', '実行: bb …/task-board.bb show ../../../../opt/data/secret'),
            (self.HELPER + ' list --vault /tmp/other', '実行: bb …/task-board.bb list --vault /tmp/other'),
            ('ls', '実行: ls'),
            ("bb /opt/data/skills/note-taking/obsidian-task-board/bin/task-board.bb create --title '[試験] Kohako G2 指示入口 M7 の E2E 確認（削除可）' --repo boxp/even-g2-lab --priority low", "実行: bb …/task-board.bb create --title '[試験] Kohako G2 指示入口 M7 の E2E 確認（削除可）' --repo boxp/even-g2-lab --priority low"),
            ('task-board show BOXP-332', '実行: task-board show BOXP-332'),
            ("printf 'a   b'", "実行: printf 'a   b'"),
        ]
        keys = set()
        for command, message in cases:
            result = self.gate('terminal', {'command': command})
            self.assertEqual(result['action'], 'approve', command)
            self.assertEqual(result['message'], message)
            self.assertTrue(result['rule_key'].startswith('kohako-g2:terminal:'))
            keys.add(result['rule_key'])
        self.assertEqual(len(keys), len(cases))
    def test_what_cannot_be_shown_in_full_is_refused(self):
        refused = [
            ('terminal', {'command': 'echo ' + 'a' * 150 + ' && rm -rf /'}),
            ('terminal', {'command': './task-board.bb create --title x', 'workdir': '/opt/data/skills/note-taking/obsidian-task-board/bin'}),
            ('terminal', {'command': 'ls', 'pty': True}),
            ('terminal', {'command': 'echo ok\nrm -rf /'}),
            ('terminal', {'command': 'echo ok\trm'}),
            ('terminal', {'command': self.HELPER + ' show x', 'background': True}),
            ('read_file', {'path': '/opt/data/.env'}),
            ('read_file', {'path': '/home/boxp/Documents/obsidian-headless/BOXP/../../../../opt/data/.env'}),
            ('read_file', {'path': 'relative.md'}),
            ('read_file', {'path': '/opt/data/cache/spillover/../../.env'}),
            ('read_file', {'path': '/opt/data/cache/spillover/other.txt'}),
            ('search_files', {'path': '/opt/data/cache/spillover/call_0a00551c780545b0add7042ac7b37191.txt'}),
            ('write_file', {'path': '/home/boxp/Documents/obsidian-headless/BOXP/x.md', 'content': 'x'}),
            ('patch', {'path': '/opt/data/SOUL.md'}),
            ('skill_manage', {'action': 'create'}),
            ('process_manage', {}),
        ]
        for tool, args in refused:
            self.assertEqual(self.gate(tool, args)['action'], 'block', (tool, args))
    def test_spillover_only_of_own_tool_calls(self):
        own, other = 'call_' + 'a' * 32, 'call_' + 'b' * 32
        spill = lambda c: {'path': f'/opt/data/cache/spillover/{c}.txt'}
        self.assertIsNone(plugin._gate(tool_name='skill_view', args={'name': 'x'}, session_id='kohako-g2-s1', tool_call_id=own))
        self.assertIsNone(plugin._gate(tool_name='read_file', args=spill(own), session_id='kohako-g2-s1', tool_call_id='call_' + 'c' * 32))
        self.assertEqual(plugin._gate(tool_name='read_file', args=spill(own), session_id='kohako-g2-s2', tool_call_id='call_' + 'd' * 32)['action'], 'block')
        self.assertEqual(plugin._gate(tool_name='read_file', args=spill(other), session_id='kohako-g2-s1', tool_call_id='call_' + 'e' * 32)['action'], 'block')
        self.assertEqual(plugin._gate(tool_name='search_files', args=spill(own), session_id='kohako-g2-s1', tool_call_id='call_' + 'f' * 32)['action'], 'block')
    def test_api_session_blocked_from_pre_llm_platform_even_if_db_fails(self):
        plugin._hook(session_id='api_9', platform='api_server', sender_id='')
        with patch.object(plugin, '_open_db', side_effect=RuntimeError('db')):
            self.assertEqual(plugin._gate(tool_name='terminal', args={}, session_id='api_9')['action'], 'block')
        plugin._hook(session_id='tg-9', platform='telegram', sender_id='other')
        with patch.object(plugin, '_open_db', side_effect=RuntimeError('db')):
            self.assertIsNone(plugin._gate(tool_name='terminal', args={}, session_id='tg-9'))
    def test_gate_error_fails_closed_for_g2(self):
        with patch.object(plugin, '_decide', side_effect=RuntimeError('x')):
            with self.assertLogs(plugin.__name__, level='WARNING'):
                self.assertEqual(plugin._gate(tool_name='terminal', args={}, session_id='kohako-g2-1')['action'], 'block')

class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {'KOHAKO_DECISION_STORE': self.tmp.name, 'KOHAKO_G2_CONTEXT_OWNER_TELEGRAM_USER_ID': '123'})
        self.env.start(); self.addCleanup(self.env.stop)
        self.db = DB()
        self.factory = patch.object(plugin, '_open_db', return_value=self.db)
        self.factory.start(); self.addCleanup(self.factory.stop)
        plugin._pending.clear(); plugin._injected.clear()
        self.delivery = {'role': 'assistant', 'content': '[Webhook delivery: task-board]\n【Task Board 通知】BOXP-1 event=old event=event-1'}
        self.record('tg')
    def record(self, sid, since=None):
        # The turn record of a session that has been running since before these turns.
        path = plugin._turns_path(sid); os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f: json.dump({'since': time.time() - 1000 if since is None else since, 'seen': []}, f)
    def cur(self, content='はい'):
        self.stamp = getattr(self, 'stamp', time.time() - 300) + 1
        return {'role': 'user', 'content': content, 'timestamp': self.stamp}
    def tg(self, history=None, sid='tg', turn='1', **kwargs):
        return plugin._hook(session_id=sid, platform='telegram', sender_id=kwargs.get('sender_id', '123'), user_message='はい', turn_id=turn, conversation_history=history if history is not None else [self.delivery, self.cur()])
    def g2(self, message='[kohako-decision] reply decision=dec-event-1 operation=12345678', sid='kohako-g2-main'):
        return plugin._hook(session_id=sid, platform='api_server', user_message=message)
    def gate(self, sid, command='update BOXP-1 --lane Ready', call='call_' + 'a' * 32, status='ok'):
        result = plugin._gate(session_id=sid, tool_name='terminal', args={'command': GateTests.HELPER + ' ' + command}, tool_call_id=call)
        if status and not (result and result.get('action') == 'block'):
            plugin._after(session_id=sid, tool_name='terminal', tool_call_id=call, status=status)
        return result
    def data(self):
        with open(plugin._path('dec-event-1')) as f: return json.load(f)
    def test_claim_transform_and_settle(self):
        self.tg(); self.assertIsNone(self.gate('tg')); self.assertIsNone(self.gate('tg'))
        self.assertEqual(self.data()['state'], 'claimed')
        self.assertEqual(self.data()['message'], plugin._message_hash('はい'))
        self.assertIn('[kohako-decision] done decision=dec-event-1', plugin._transform(session_id='tg', response_text='ok'))
        plugin._post(session_id='tg'); self.assertEqual(self.data()['state'], 'done')
    def test_denied_or_blocked_releases_claim(self):
        self.g2(); self.assertIsNone(self.gate('kohako-g2-main', status='blocked'))
        self.assertNotIn('message', self.data())
        self.assertEqual(self.data()['state'], 'claimed')
        self.assertIsNone(plugin._transform(session_id='kohako-g2-main', response_text='ok'))
        plugin._post(session_id='kohako-g2-main'); self.assertFalse(os.path.exists(plugin._path('dec-event-1')))
        self.assertNotIn('既に対応済み', str(self.tg())); self.assertIsNone(self.gate('tg'))
        plugin._post(session_id='tg'); self.assertEqual(self.data()['state'], 'done'); self.assertEqual(self.data()['via'], 'telegram')
    def test_release_after_other_turn_started_allows_claim(self):
        # G2 claims, Telegram's turn starts (sees G2 as owner), then G2's operations are all denied.
        self.g2(); self.assertIsNone(self.gate('kohako-g2-main', status='blocked'))
        self.assertIn('既に対応済み', str(self.tg())); self.assertEqual(self.gate('tg')['action'], 'block')
        plugin._post(session_id='kohako-g2-main'); self.assertFalse(os.path.exists(plugin._path('dec-event-1')))
        self.assertIsNone(self.gate('tg', call='call_' + 'c' * 32))
        plugin._post(session_id='tg'); self.assertEqual(self.data()['state'], 'done'); self.assertEqual(self.data()['via'], 'telegram')
    def test_error_stays_claimed_and_is_not_rerun(self):
        # An error may follow a partial change or a timed-out external success: result unknown.
        self.tg(); self.assertIsNone(self.gate('tg', status='error'))
        self.assertIsNone(plugin._transform(session_id='tg', response_text='ok'))
        plugin._post(session_id='tg'); self.assertEqual(self.data()['state'], 'claimed')
        self.assertIn('結果不明', self.g2()['context']); self.assertEqual(self.gate('kohako-g2-main')['action'], 'block')
        plugin._post(session_id='kohako-g2-main'); self.assertEqual(self.data()['via'], 'telegram')
        self.tg(turn='2'); self.assertEqual(self.gate('tg')['action'], 'block')
        self.assertIsNone(self.gate('tg', 'show BOXP-1'))
    def test_error_blocks_retry_in_same_turn(self):
        self.g2(); self.assertIsNone(self.gate('kohako-g2-main', status='error'))
        self.assertIn('結果不明', self.gate('kohako-g2-main', call='call_' + 'b' * 32)['message'])
        self.assertIsNone(plugin._transform(session_id='kohako-g2-main', response_text='ok'))
        plugin._post(session_id='kohako-g2-main'); self.assertEqual(self.data()['state'], 'claimed')
    def test_inflight_blocks_parallel_operation_in_same_turn(self):
        # The first operation has not reported back (status=None): a second one must not run alongside it.
        self.tg(); self.assertIsNone(self.gate('tg', status=None))
        self.assertIn('先行操作', self.gate('tg', call='call_' + 'b' * 32, status=None)['message'])
        self.assertIsNone(self.gate('tg', 'show BOXP-1'))
        plugin._after(session_id='tg', tool_call_id='call_' + 'a' * 32, status='ok')
        self.assertIsNone(self.gate('tg', call='call_' + 'c' * 32))
        plugin._post(session_id='tg'); self.assertEqual(self.data()['state'], 'done')
    def test_ok_wins_and_unknown_stays_claimed(self):
        self.tg(); self.gate('tg', status='blocked'); self.gate('tg', call='call_' + 'b' * 32)
        plugin._post(session_id='tg'); self.assertEqual(self.data()['state'], 'done')
        os.unlink(plugin._path('dec-event-1'))
        self.g2(); self.gate('kohako-g2-main', status=None)  # no post_tool_call: result unknown
        self.assertIsNone(plugin._transform(session_id='kohako-g2-main', response_text='ok'))
        plugin._post(session_id='kohako-g2-main'); self.assertEqual(self.data()['state'], 'claimed')
        self.tg(); self.assertEqual(self.gate('tg')['action'], 'block')
        os.unlink(plugin._path('dec-event-1'))
        self.g2(); plugin._gate(session_id='kohako-g2-main', tool_name='terminal', args={'command': GateTests.HELPER + ' update BOXP-1 --lane Ready'})
        plugin._post(session_id='kohako-g2-main'); self.assertEqual(self.data()['state'], 'claimed')
    def test_success_then_unknown_stays_claimed(self):
        # One call succeeded, a later one errored (e.g. timed out after an external success):
        # not 'done' (which would expire and allow a re-run), no done marker.
        self.tg(); self.assertIsNone(self.gate('tg', status='ok')); self.assertIsNone(self.gate('tg', call='call_' + 'b' * 32, status='error'))
        self.assertIsNone(plugin._transform(session_id='tg', response_text='ok'))
        plugin._post(session_id='tg'); self.assertEqual(self.data()['state'], 'claimed')
        self.g2(); self.assertEqual(self.gate('kohako-g2-main')['action'], 'block')
    def test_detection_error_fails_closed(self):
        self.g2(); self.gate('kohako-g2-main'); plugin._post(session_id='kohako-g2-main')
        with patch.object(plugin, '_owner', side_effect=RuntimeError('db')):
            with self.assertLogs(plugin.__name__, level='WARNING'):
                self.tg()
        self.assertEqual(self.gate('tg')['action'], 'block')
        self.assertIsNone(self.gate('tg', 'show BOXP-1'))
        with patch.object(plugin, '_owner', side_effect=RuntimeError('db')):
            with self.assertLogs(plugin.__name__, level='WARNING'):
                self.tg(history=[{'role': 'user', 'content': 'hello'}])
        self.assertIn('確認できない', self.gate('tg')['message'])
    def test_both_orders_and_read_only(self):
        for first, second in (('g2', 'tg'), ('tg', 'g2')):
            with self.subTest(first=first):
                if os.path.exists(plugin._path('dec-event-1')): os.unlink(plugin._path('dec-event-1'))
                plugin._pending.clear()
                getattr(self, first)(); self.assertIsNone(self.gate('kohako-g2-main' if first == 'g2' else 'tg'))
                result = getattr(self, second)(); self.assertIn('既に対応済み', result['context'])
                sid = 'kohako-g2-main' if second == 'g2' else 'tg'
                self.assertIsNone(self.gate(sid, 'show BOXP-1'))
                self.assertIsNone(plugin._gate(session_id=sid, tool_name='read_file', args={'path': '/home/boxp/Documents/obsidian-headless/BOXP/a'}))
                self.assertEqual(self.gate(sid)['action'], 'block')
    def test_notice_format_matches_bff(self):
        # Every notification the BFF lists for a G2 reply must be claimable from Telegram too.
        for content in ('[Webhook delivery: task-board]\n【Task Board 通知】 BOXP-1 ...\nevent=event-1',
                        '  [Webhook delivery: task-board] 【Task Board 通知】\u3000BOXP-1 event=event-1',
                        '[Webhook delivery: task-board]\n【Task Board 通知】ABC-1 event=old\nevent=event-1'):
            with self.subTest(content=content):
                if os.path.exists(plugin._path('dec-event-1')): os.unlink(plugin._path('dec-event-1'))
                plugin._pending.clear()
                self.tg(history=[{'role': 'assistant', 'content': content}, self.cur()]); self.assertIsNone(self.gate('tg'))
                self.assertIn('既に対応済み', self.g2()['context']); self.assertEqual(self.gate('kohako-g2-main')['action'], 'block')
        for content in ('[Webhook delivery: task-board]\n【Task Board 通知】BOXP-1 xevent=event-1',
                        '[Webhook delivery: task-board]\n【Task Board 通知】BOXP-1x event=event-1',
                        '[Webhook delivery: task-board]\n[SILENT] 【Task Board 通知】BOXP-1 event=event-1'):
            with self.subTest(ignored=content):
                self.assertIsNone(plugin._notice_event(content))
    def test_race(self):
        pending = lambda via: {'decisionId': 'dec-event-1', 'eventId': 'event-1', 'via': via, 'operationId': via,
                               'message': plugin._message_hash('はい'), 'session': plugin._session_key('tg')}
        results = []
        threads = [threading.Thread(target=lambda v=v: results.append(plugin._claim(pending(v)))) for v in ('g2', 'telegram')]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(results.count(None), 1)
    def test_gc_cannot_delete_a_fresh_claim(self):
        # GC reads an expired 'done' file; a concurrent caller must not slip a new claim in
        # before GC unlinks, or GC would delete it and both entry points could claim.
        pending = lambda via: {'decisionId': 'dec-event-1', 'eventId': 'event-1', 'via': via, 'operationId': via,
                               'message': plugin._message_hash('はい'), 'session': plugin._session_key('tg')}
        path = plugin._path('dec-event-1'); os.makedirs(os.path.dirname(path))
        with open(path, 'w') as f: json.dump({'state': 'done', 'via': 'telegram', 'at': time.time() - 8 * 86400}, f)
        real_unlink, results, threads = os.unlink, [], []
        def unlink(target, *args, **kwargs):
            if target == path and not threads:
                threads.append(threading.Thread(target=lambda: results.append(plugin._claim(pending('telegram')))))
                threads[0].start(); threads[0].join(0.3)
            return real_unlink(target, *args, **kwargs)
        with patch.object(plugin.os, 'unlink', side_effect=unlink):
            results.append(plugin._claim(pending('g2')))
            threads[0].join()
        self.assertEqual(sorted(map(str, results)), ['None', 'g2'])
        self.assertEqual(self.data()['state'], 'claimed'); self.assertEqual(self.data()['via'], 'g2')
    def test_read_only_then_other_claims(self):
        self.tg(); self.assertIsNone(self.gate('tg', 'show BOXP-1'))
        self.assertFalse(os.path.exists(plugin._path('dec-event-1')))
        self.g2(); self.assertIsNone(self.gate('kohako-g2-main'))
    def test_old_redelivery_and_restart(self):
        self.tg([self.delivery, {'role': 'user', 'content': 'previous'}, self.cur()])
        self.assertNotIn(('tg', '1'), plugin._pending)
        self.assertIsNone(self.gate('tg'))
        self.tg([self.delivery, self.delivery, self.cur()]); self.assertIsNone(self.gate('tg'))
        plugin._pending.clear(); self.g2(); self.assertEqual(self.gate('kohako-g2-main')['action'], 'block')
    def test_unparsable_and_invalid(self):
        path = plugin._path('dec-event-1'); os.makedirs(os.path.dirname(path))
        with open(path, 'w') as f: f.write('{')
        self.tg(); self.assertEqual(self.gate('tg')['action'], 'block')
        for message, sid in (('hello', 'kohako-g2-main'), ('[kohako-decision] bad', 'kohako-g2-main'), ('[kohako-decision] reply decision=dec-event-1 operation=12345678', 'kohako-g2-cand-x')):
            self.g2(message, sid); self.assertNotIn((sid, None), plugin._pending)
    def test_current_message_stored_differently(self):
        self.tg([self.delivery, self.cur('[note] はい')]); self.assertIn(('tg', '1'), plugin._pending)
        self.tg([self.delivery]); self.assertIn(('tg', '1'), plugin._pending)
    def test_history_without_current_message(self):
        # The previous Owner message ends the walk; it is not taken for the current message.
        self.tg([self.delivery, {'role': 'user', 'content': 'previous'}]); self.assertNotIn(('tg', '1'), plugin._pending)
        self.tg([self.delivery, {'role': 'user', 'content': 'previous'}, self.cur()]); self.assertNotIn(('tg', '1'), plugin._pending)
        self.tg([self.delivery, self.cur()]); self.assertIn(('tg', '1'), plugin._pending)
    def test_multimodal_owner_message_ends_the_walk(self):
        # An image message after the notification is an Owner turn; a later instruction is not a reply.
        image = {'role': 'user', 'content': [{'type': 'text', 'text': 'これ見て'}, {'type': 'image_url', 'image_url': {'url': 'x'}}]}
        self.tg([self.delivery, image, self.cur()]); self.assertNotIn(('tg', '1'), plugin._pending)
        self.assertIsNone(self.gate('tg')); self.assertFalse(os.path.exists(plugin._path('dec-event-1')))
        self.tg([self.delivery, {'role': 'assistant', 'content': [{'type': 'text', 'text': 'x'}]}, self.cur()])
        self.assertIn(('tg', '1'), plugin._pending)
    def test_multimodal_last_message_is_ambiguous(self):
        # The history ends with an image message: it may be a past one (current message missing)
        # or the current one stored multimodal. A notification beyond it is never claimed and
        # side effects are blocked.
        image = {'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': 'x'}}], 'timestamp': time.time()}
        self.tg([self.delivery, image]); self.assertFalse(plugin._pending[('tg', '1')]['claimed'])
        self.assertEqual(self.gate('tg')['action'], 'block'); self.assertFalse(os.path.exists(plugin._path('dec-event-1')))
        self.assertIsNone(self.gate('tg', 'show BOXP-1'))
        self.tg([{'role': 'user', 'content': 'hello'}, image]); self.assertNotIn(('tg', '1'), plugin._pending)
    def test_repeated_identical_message_is_not_taken_for_current(self):
        # "はい" twice: a history without the current message ends with the previous "はい",
        # whose timestamp anchored an earlier turn, so the old notification is not connected.
        previous = self.cur()
        self.tg([{'role': 'user', 'content': 'hello'}, previous]); self.assertNotIn(('tg', '1'), plugin._pending)
        with self.assertLogs(plugin.__name__, level='WARNING'):
            self.tg([self.delivery, previous])
        self.assertEqual(plugin._pending[('tg', '1')]['lost'], 'error'); self.assertIn('確認できない', self.gate('tg')['message'])
        self.assertFalse(os.path.exists(plugin._path('dec-event-1')))
        self.tg([{'role': 'user', 'content': 'hello'}, previous]); self.assertNotIn(('tg', '1'), plugin._pending)  # no notification: unaffected
        self.tg([self.delivery, previous, {'role': 'assistant', 'content': 'ok'}, self.cur()]); self.assertNotIn(('tg', '1'), plugin._pending)
        self.tg([self.delivery, self.cur()]); self.assertIn(('tg', '1'), plugin._pending)
        with self.assertLogs(plugin.__name__, level='WARNING'):
            self.tg([self.delivery, {'role': 'user', 'content': 'はい'}])  # unstamped: cannot be identified
        self.assertEqual(plugin._pending[('tg', '1')]['lost'], 'error')
    def test_unrecorded_old_message_is_not_taken_for_current(self):
        # Without the current message, the history may end with an identical "はい" that was
        # never recorded (before the plugin, or pushed out of the record): not provably current.
        for stamp in (time.time() - 3600, time.time() + 3600):
            with self.subTest(stamp=stamp):
                with self.assertLogs(plugin.__name__, level='WARNING'):
                    self.tg([self.delivery, {'role': 'user', 'content': 'はい', 'timestamp': stamp}])
                self.assertEqual(plugin._pending[('tg', '1')]['lost'], 'error'); self.assertFalse(os.path.exists(plugin._path('dec-event-1')))
        self.tg([{'role': 'user', 'content': 'hello'}, self.cur()]); later = self.stamp
        with self.assertLogs(plugin.__name__, level='WARNING'):  # unrecorded but older than a recorded turn
            self.tg([self.delivery, {'role': 'user', 'content': 'はい', 'timestamp': later - 0.5}])
        self.assertEqual(plugin._pending[('tg', '1')]['lost'], 'error')
        self.tg([self.delivery, self.cur()]); self.assertIsNone(self.gate('tg'))
    def test_unrecorded_recent_message_before_record_is_not_current(self):
        # A fresh "はい" sent just before the plugin started recording turns is not provably
        # current: the first turn of a new record never connects to a notification.
        os.unlink(plugin._turns_path('tg'))
        old = {'role': 'user', 'content': 'はい', 'timestamp': time.time() - 120}
        with self.assertLogs(plugin.__name__, level='WARNING'):
            self.tg([self.delivery, old])
        self.assertEqual(plugin._pending[('tg', '1')]['lost'], 'error'); self.assertEqual(self.gate('tg')['action'], 'block')
        self.assertFalse(os.path.exists(plugin._path('dec-event-1')))
        self.stamp = time.time()  # the next Owner message, sent after the record began
        self.tg([self.delivery, old, {'role': 'assistant', 'content': 'ok'}, self.delivery, self.cur()], turn='2')
        self.assertIsNone(plugin._pending[('tg', '2')]['lost'])
    def test_concurrent_turns_of_one_session_keep_their_own_state(self):
        # A second turn starting while the first one (answering a notification) still runs
        # must neither drop the first turn's claim nor let it run unclaimed.
        call = lambda turn, c, status='ok', command='update BOXP-1 --lane Ready': self._turn_gate(turn, c, status, command)
        self.tg(turn='t1'); self.assertIsNone(call('t1', 'a'))
        plugin._hook(session_id='tg', platform='telegram', sender_id='123', user_message='hello', turn_id='t2',
                     conversation_history=[self.delivery, self.cur(), {'role': 'assistant', 'content': 'ok'}, self.cur('hello')])
        self.assertIn(('tg', 't1'), plugin._pending)
        self.assertIsNone(call('t2', 'b'))  # not a reply: no claim involved
        self.assertIsNone(call('t1', 'c'))  # still claimed by t1
        self.tg(turn='t3'); self.assertEqual(call('t3', 'd')['action'], 'block')  # same notification again
        self.assertIn('[kohako-decision] done', plugin._transform(session_id='tg', turn_id='t1', response_text='ok'))
        self.assertIsNone(plugin._transform(session_id='tg', turn_id='t3', response_text='ok'))
        plugin._post(session_id='tg', turn_id='t2'); self.assertEqual(self.data()['state'], 'claimed')
        plugin._post(session_id='tg', turn_id='t1'); self.assertEqual(self.data()['state'], 'done')
        plugin._post(session_id='tg', turn_id='t3'); self.assertEqual(self.data()['via'], 'telegram')
        # Hooks without a turn id cannot be attributed while two turns are open: fail closed.
        self.tg(turn='t4'); self.tg(turn='t5')
        self.assertEqual(plugin._gate(session_id='tg', tool_name='terminal', args={'command': GateTests.HELPER + ' update BOXP-1 --lane Ready'}, tool_call_id='call_' + 'e' * 32)['action'], 'block')
    def _turn_gate(self, turn, c, status, command):
        call = 'call_' + c * 32
        result = plugin._gate(session_id='tg', tool_name='terminal', args={'command': GateTests.HELPER + ' ' + command}, tool_call_id=call, turn_id=turn)
        if status and not (result and result.get('action') == 'block'):
            plugin._after(session_id='tg', tool_name='terminal', tool_call_id=call, status=status, turn_id=turn)
        return result
    def test_full_pending_does_not_evict_open_turns(self):
        # An open turn's decision is never dropped for a newer one; the newer one fails closed.
        self.g2(); self.assertIsNone(self.gate('kohako-g2-main'))  # G2 claims first
        self.tg(turn='t0')
        with patch.object(plugin, '_PENDING_MAX', 2):
            self.tg(turn='t1')
            self.assertIn('確認できない', self.tg(turn='t2')['context'])
        self.assertIn(('tg', 't0'), plugin._pending)
        self.assertEqual(self._turn_gate('t0', 'a', 'ok', 'update BOXP-1 --lane Ready')['action'], 'block')
        self.assertEqual(self._turn_gate('t2', 'b', 'ok', 'update BOXP-1 --lane Ready')['action'], 'block')
        self.assertEqual(self.data()['via'], 'g2')
        # Turns that never reached post_llm_call expire after the TTL and free their slot.
        for value in plugin._pending.values(): value['at'] -= plugin._PENDING_TTL + 1
        with patch.object(plugin, '_PENDING_MAX', 2):
            self.assertNotIn('確認できない', str(self.tg(turn='t3')))
        self.assertEqual(list(plugin._pending), [('tg', 't3')])
    def test_expired_done_does_not_block_its_notification(self):
        path = plugin._path('dec-event-1'); os.makedirs(os.path.dirname(path))
        with open(path, 'w') as f: json.dump({'state': 'done', 'via': 'telegram', 'at': time.time() - 8 * 86400}, f)
        self.assertIsNone(self.g2()); self.assertIsNone(self.gate('kohako-g2-main'))
        plugin._post(session_id='kohako-g2-main'); self.assertEqual(self.data()['via'], 'g2'); self.assertEqual(self.data()['state'], 'done')
        self.assertIn('既に対応済み', self.tg()['context'])
    def test_webhook_turn_is_not_a_reply(self):
        plugin._hook(session_id='tg', platform='telegram', sender_id='123', user_message=self.delivery['content'], turn_id='w', conversation_history=[{'role': 'user', 'content': self.delivery['content'], 'timestamp': 1.0}])
        self.assertNotIn(('tg', '1'), plugin._pending)
    def test_inspection_commands_do_not_claim(self):
        self.tg()
        for command in ('date', 'git status', 'git log --oneline -5', 'gh pr view 929', 'gh pr checks 929 --repo boxp/lolice', 'ls -la', 'kubectl get pods -n hermes-agent -o wide'):
            with self.subTest(command=command):
                self.assertIsNone(plugin._gate(session_id='tg', tool_name='terminal', args={'command': command}, tool_call_id='call_' + 'c' * 32))
        self.assertFalse(os.path.exists(plugin._path('dec-event-1')))
        for command in ('date; touch x', 'git diff --output=x', 'git push', 'gh pr merge 929', 'cat a > b', 'ls $(rm x)', 'rg --pre=sh x', 'gh api -X POST x'):
            with self.subTest(command=command):
                self.assertFalse(plugin._read_only_command(command))
        self.g2(); self.assertIsNone(self.gate('kohako-g2-main'))
    def mirror(self, event='event-1'):
        # How Hermes really mirrors a delivery into the Telegram session (gateway/platforms/webhook.py).
        return {'role': 'user', 'content': f'[Webhook delivery: task-board]\n【Task Board 通知】BOXP-1 event={event}'}
    def claimed_elsewhere(self, via='telegram', **extra):
        path = plugin._path('dec-event-1'); os.makedirs(os.path.dirname(path), exist_ok=True)
        data = {'state': 'done', 'via': via, 'operationId': 'tg-x', 'at': time.time() - 100,
                'session': plugin._session_key('tg'), 'message': plugin._message_hash('以前の返答')} | extra
        if data.get('session') is None: data.pop('session')
        with open(path, 'w') as f: json.dump(data, f)
    def new_request(self, history, turn='n', message='新しい不具合の修復依頼を登録して'):
        self.last_context = plugin._hook(session_id='tg', platform='telegram', sender_id='123', user_message=message, turn_id=turn, conversation_history=history)
        return plugin._gate(session_id='tg', tool_name='terminal', args={'command': GateTests.HELPER + ' create --title x'}, tool_call_id='call_' + 'f' * 32, turn_id=turn)
    def test_mirrored_user_delivery_is_claimable(self):
        self.tg([self.mirror(), self.cur()]); self.assertIsNone(self.gate('tg'))
        plugin._post(session_id='tg'); self.assertEqual(self.data()['state'], 'done')
    def test_compaction_summary(self):
        # BOXP-362: compaction folded the Owner's reply (and the turn it started) into a summary; the
        # protected head still ends with the old notification. A new instruction is not its reply.
        self.claimed_elsewhere(at=time.time() - 400)
        summaries = (
            {'role': 'assistant', 'content': '[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted ...\n\n--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---'},
            {'role': 'assistant', 'content': '[PRIOR CONTEXT — for reference only; not a new message]\n対応しました\n\n[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]\n\nsummary\n\n--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---'},
            {'role': 'assistant', 'content': [{'type': 'text', 'text': 'summary'}], '_compressed_summary': True},
        )
        for summary in summaries:
            with self.subTest(summary=str(summary)[:40]):
                history = [{'role': 'user', 'content': 'hello'}, self.mirror(), summary, {'role': 'assistant', 'content': '反映しました'}, self.cur('新しい不具合の修復依頼を登録して')]
                self.assertIsNone(self.new_request(history)); self.assertNotIn(('tg', 'n'), plugin._pending)
                self.assertIn('既に対応済み', self.last_context['context'])
        self.assertEqual(self.data()['via'], 'telegram')
        # A summary merged into the current message follows the same rule.
        merged = self.cur('[PRIOR CONTEXT — for reference only; not a new message]\n新しい不具合の修復依頼を登録して\n\nsummary')
        self.assertIsNone(self.new_request([self.mirror(), merged])); self.assertNotIn(('tg', 'n'), plugin._pending)
        path = plugin._path('dec-event-1')
        for summary in (dict(summaries[0], role='user'), summaries[0]):
            with self.subTest(unclaimed=summary['role']):
                plugin._pending.clear(); os.unlink(path)
                self.assertIsNone(self.new_request([self.mirror(), summary, self.cur('はい')], message='はい'))
                self.assertEqual(self.data()['via'], 'telegram')
        for claim in ({'session': None}, {'session': plugin._session_key('another')},
                      {'operationId': 'tg-n'}, {'message': None}, {'via': 'g2'},
                      {'state': 'claimed'}, {'message': plugin._message_hash('はい')}):
            with self.subTest(claim=claim):
                plugin._pending.clear(); os.unlink(path)
                self.claimed_elsewhere(**claim)
                self.assertEqual(self.new_request([self.mirror(), summaries[0], self.cur('はい')], message='はい')['action'], 'block')
        plugin._pending.clear(); os.unlink(path)
        self.claimed_elsewhere()
        # A notification after the summary is still answerable, and still guarded when handled.
        self.assertEqual(self.new_request([summaries[0], self.mirror(), self.cur('新しい不具合の修復依頼を登録して')])['action'], 'block')
    def test_rephrased_reply_beyond_summary_gets_soft_warning(self):
        plugin._hook(session_id='tg', platform='telegram', sender_id='123', user_message='Ready に移動',
                     turn_id='r', conversation_history=[self.mirror(), self.cur('Ready に移動')])
        self.assertIsNone(self._turn_gate('r', 'a', 'ok', 'update BOXP-1 --lane Ready'))
        plugin._post(session_id='tg', turn_id='r')
        self.assertEqual(self.data()['session'], plugin._session_key('tg'))
        summary = {'role': 'assistant', 'content': '[CONTEXT COMPACTION — REFERENCE ONLY] summary'}
        message = 'Ready にしてください'
        self.stamp = time.time()
        self.assertIsNone(self.new_request([self.mirror(), summary, self.cur(message)], message=message))
        self.assertNotIn(('tg', 'n'), plugin._pending)
        self.assertIn('既に対応済み', self.last_context['context'])

    def test_new_claim_concurrent_turn_beyond_summary_stays_blocked(self):
        now = time.time()
        summary = {'role': 'assistant', 'content': '[CONTEXT COMPACTION — REFERENCE ONLY] summary'}
        message = '新しい不具合の修復依頼を登録して'
        self.claimed_elsewhere(at=now - 10)
        current = {'role': 'user', 'content': message, 'timestamp': now - 20}
        self.assertEqual(self.new_request([self.mirror(), summary, current], message=message)['action'], 'block')

    def test_legacy_claim_requires_exclusive_session_evidence(self):
        now = time.time(); at = now - 30
        message = '新しい不具合の修復依頼を登録して'
        summary = {'role': 'assistant', 'content': '[CONTEXT COMPACTION — REFERENCE ONLY] summary'}
        path = plugin._path('dec-event-1'); os.makedirs(os.path.dirname(path), exist_ok=True)
        other = plugin._turns_path('other')
        for name, seen, released in (('recent', [repr(at - 5)], False),
                                     ('unreadable', None, False),
                                     ('full', [repr(now - 1 - i / 10) for i in range(64)], False),
                                     ('old', [repr(at - 3601)], True)):
            with self.subTest(name=name):
                plugin._pending.clear()
                with open(path, 'w') as f:
                    json.dump({'state': 'done', 'via': 'telegram', 'operationId': 'tg-old', 'at': at}, f)
                with open(plugin._turns_path('tg'), 'w') as f:
                    json.dump({'since': now - 1000, 'seen': [repr(at - 10)]}, f)
                with open(other, 'w') as f:
                    if seen is None: f.write('{')
                    else: json.dump({'since': now - 5000, 'seen': seen}, f)
                current = {'role': 'user', 'content': message, 'timestamp': now - 10}
                result = self.new_request([self.mirror(), summary, current], message=message)
                if released:
                    self.assertIsNone(result)
                    self.assertIn('既に対応済み', self.last_context['context'])
                else:
                    self.assertEqual(result['action'], 'block')

    def test_legacy_claim_beyond_summary_requires_earlier_session_turn(self):
        now = time.time()
        at = now - 30
        message = '新しい不具合の修復依頼を登録して'
        summary = {'role': 'assistant', 'content': '[CONTEXT COMPACTION — REFERENCE ONLY] summary'}
        cases = (
            ('matching turn', {}, at - 5, True, True, True),
            ('old turn', {}, at - 601, True, True, False),
            ('claimed', {'state': 'claimed'}, at - 5, True, True, False),
            ('no summary', {}, at - 5, False, True, False),
            ('unanchored', {}, at - 5, True, False, False),
            ('g2', {'via': 'g2'}, at - 5, True, True, False),
            ('same operation', {'operationId': 'tg-n'}, at - 5, True, True, False),
        )
        for name, changes, earlier, compacted, anchored, released in cases:
            with self.subTest(name=name):
                plugin._pending.clear()
                claim = {'state': 'done', 'via': 'telegram', 'operationId': 'tg-old', 'at': at} | changes
                path = plugin._path('dec-event-1')
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, 'w') as f: json.dump(claim, f)
                before = Path(path).read_bytes()
                with open(plugin._turns_path('tg'), 'w') as f:
                    json.dump({'since': now - 1000, 'seen': [repr(earlier)]}, f)
                current = {'role': 'user', 'content': message}
                if anchored: current['timestamp'] = now - 10
                history = [self.mirror()] + ([summary] if compacted else []) + [current]
                result = self.new_request(history, message=message)
                if released:
                    self.assertIsNone(result)
                    self.assertNotIn(('tg', 'n'), plugin._pending)
                    self.assertIn('既に対応済み', self.last_context['context'])
                else:
                    self.assertEqual(result['action'], 'block')
                self.assertEqual(Path(path).read_bytes(), before)

    def test_earlier_owner_reply_without_compaction_is_not_bound(self):
        message = 'Ready にしてください'
        history = [self.mirror(), {'role': 'user', 'content': 'はい'},
                   {'role': 'assistant', 'content': '対応しました'}, self.cur(message)]
        self.assertIsNone(self.new_request(history, message=message))
        self.assertNotIn(('tg', 'n'), plugin._pending)
        self.assertNotIn('既に対応済み', str(self.last_context))

    def test_marker_text_elsewhere_is_not_compaction(self):
        # Only a summary row (user/assistant, starting with the compaction header) marks compaction:
        # marker text inside a notification or a tool result does not drop the guard.
        end = '--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---'
        mirror = {'role': 'user', 'content': self.mirror()['content'] + ' ' + end}
        tool = {'role': 'tool', 'tool_call_id': 'x', 'content': '[CONTEXT COMPACTION — REFERENCE ONLY] ' + end}
        quoted = {'role': 'assistant', 'content': '要約の例: ' + end}
        for history in ([mirror, self.cur()], [self.mirror(), tool, quoted, self.cur()]):
            with self.subTest(history=str(history)[:60]):
                plugin._pending.clear()
                if os.path.exists(plugin._path('dec-event-1')): os.unlink(plugin._path('dec-event-1'))
                self.tg(history); self.assertIsNone(self.gate('tg')); self.assertEqual(self.data()['via'], 'telegram')
                self.assertIn('既に対応済み', self.g2()['context'])
                self.assertEqual(self.gate('kohako-g2-main')['action'], 'block')
    def test_unreadable_claim_shape_blocks(self):
        path = plugin._path('dec-event-1'); os.makedirs(os.path.dirname(path))
        with open(path, 'w') as f: json.dump([1], f)
        self.tg([self.mirror(), self.cur()]); self.assertEqual(self.gate('tg')['action'], 'block')
    def test_resend_after_done_is_still_blocked(self):
        self.tg([self.mirror(), self.cur()], turn='r')
        self.assertIsNone(self._turn_gate('r', 'a', 'ok', 'update BOXP-1 --lane Ready'))
        plugin._post(session_id='tg', turn_id='r'); self.assertEqual(self.data()['state'], 'done')
        self.stamp = time.time()  # sent after the reply's turn finished
        self.assertEqual(self.new_request([self.mirror(), self.cur('Ready に移動')], message='Ready に移動')['action'], 'block')
        self.assertEqual(self.new_request([self.mirror(), {'role': 'assistant', 'content': '対応しました'}, self.cur('Ready に移動')], message='Ready に移動')['action'], 'block')
    def test_result_unknown_still_blocks_other_routes(self):
        self.tg([self.mirror(), self.cur()], turn='r'); self._turn_gate('r', 'a', 'error', 'update BOXP-1 --lane Ready')
        plugin._post(session_id='tg', turn_id='r'); self.assertEqual(self.data()['state'], 'claimed')
        self.assertIn('結果不明', self.g2()['context']); self.assertEqual(self.gate('kohako-g2-main')['action'], 'block')
    def test_non_owner_and_group(self):
        self.tg(sender_id='other'); self.assertNotIn(('tg', '1'), plugin._pending)
        self.db.session['chat_type'] = 'group'; self.tg(); self.assertNotIn(('tg', '1'), plugin._pending)

if __name__ == '__main__': unittest.main()
