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
    def tg(self, history=None, sid='tg', turn='1', **kwargs):
        return plugin._hook(session_id=sid, platform='telegram', sender_id=kwargs.get('sender_id', '123'), user_message='はい', turn_id=turn, conversation_history=history if history is not None else [self.delivery, {'role': 'user', 'content': 'はい'}])
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
        self.assertIn('[kohako-decision] done decision=dec-event-1', plugin._transform(session_id='tg', response_text='ok'))
        plugin._post(session_id='tg'); self.assertEqual(self.data()['state'], 'done')
    def test_denied_or_blocked_releases_claim(self):
        self.g2(); self.assertIsNone(self.gate('kohako-g2-main', status='blocked'))
        self.assertEqual(self.data()['state'], 'claimed')
        self.assertIsNone(plugin._transform(session_id='kohako-g2-main', response_text='ok'))
        plugin._post(session_id='kohako-g2-main'); self.assertFalse(os.path.exists(plugin._path('dec-event-1')))
        self.assertNotIn('既に対応済み', str(self.tg())); self.assertIsNone(self.gate('tg'))
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
    def test_error_then_ok_is_done(self):
        self.g2(); self.gate('kohako-g2-main', status='error'); self.gate('kohako-g2-main', call='call_' + 'b' * 32)
        plugin._post(session_id='kohako-g2-main'); self.assertEqual(self.data()['state'], 'done')
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
    def test_race(self):
        pending = lambda via: {'decisionId': 'dec-event-1', 'eventId': 'event-1', 'via': via, 'operationId': via}
        results = []
        threads = [threading.Thread(target=lambda v=v: results.append(plugin._claim(pending(v)))) for v in ('g2', 'telegram')]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(results.count(None), 1)
    def test_read_only_then_other_claims(self):
        self.tg(); self.assertIsNone(self.gate('tg', 'show BOXP-1'))
        self.assertFalse(os.path.exists(plugin._path('dec-event-1')))
        self.g2(); self.assertIsNone(self.gate('kohako-g2-main'))
    def test_old_redelivery_and_restart(self):
        self.tg([self.delivery, {'role': 'user', 'content': 'previous'}, {'role': 'user', 'content': 'はい'}])
        self.assertNotIn('tg', plugin._pending)
        self.assertIsNone(self.gate('tg'))
        self.tg([self.delivery, self.delivery, {'role': 'user', 'content': 'はい'}]); self.assertIsNone(self.gate('tg'))
        plugin._pending.clear(); self.g2(); self.assertEqual(self.gate('kohako-g2-main')['action'], 'block')
    def test_unparsable_and_invalid(self):
        path = plugin._path('dec-event-1'); os.makedirs(os.path.dirname(path))
        with open(path, 'w') as f: f.write('{')
        self.tg(); self.assertEqual(self.gate('tg')['action'], 'block')
        for message, sid in (('hello', 'kohako-g2-main'), ('[kohako-decision] bad', 'kohako-g2-main'), ('[kohako-decision] reply decision=dec-event-1 operation=12345678', 'kohako-g2-cand-x')):
            self.g2(message, sid); self.assertNotIn(sid, plugin._pending)
    def test_current_message_stored_differently(self):
        self.tg([self.delivery, {'role': 'user', 'content': '[note] はい'}]); self.assertIn('tg', plugin._pending)
        self.tg([self.delivery]); self.assertIn('tg', plugin._pending)
    def test_history_without_current_message(self):
        # The previous Owner message ends the walk; it is not taken for the current message.
        self.tg([self.delivery, {'role': 'user', 'content': 'previous'}]); self.assertNotIn('tg', plugin._pending)
        self.tg([self.delivery, {'role': 'user', 'content': 'previous'}, {'role': 'user', 'content': 'はい'}]); self.assertNotIn('tg', plugin._pending)
        self.tg([self.delivery, {'role': 'user', 'content': 'はい'}]); self.assertIn('tg', plugin._pending)
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
    def test_non_owner_and_group(self):
        self.tg(sender_id='other'); self.assertNotIn('tg', plugin._pending)
        self.db.session['chat_type'] = 'group'; self.tg(); self.assertNotIn('tg', plugin._pending)

if __name__ == '__main__': unittest.main()
