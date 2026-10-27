import importlib.util
import os
from pathlib import Path
import time
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

if __name__ == '__main__': unittest.main()
