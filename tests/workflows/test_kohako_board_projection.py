"""BOXP-329: Kohako G2 read-only Task Board projection (codex-workspace Dashboard sidecar).

Loads ``kohako_board.py`` from the Dashboard ConfigMap and checks the returned
fields, limits, masking and lane / assignee mismatch flags on a fixture vault.

Requires Mike Farah yq v4 on PATH, as the workflow's preparation steps do.
"""
from datetime import datetime, timedelta, timezone
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
CONFIGMAP = ROOT / 'argoproj/codex-workspace/dashboard-configmap.yaml'
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
ALLOWED_ITEM_KEYS = {
    'id', 'title', 'lane', 'status', 'laneStatusMismatch', 'assignee', 'ticketAssignee',
    'assigneeMismatch', 'priority', 'ticketUpdatedAt', 'latestNoteAt', 'latestNoteSource',
    'progress', 'blocker', 'decision', 'readError',
}


def configmap_value(key):
    binary = shutil.which('yq')
    if binary is None:
        raise RuntimeError('Mike Farah yq v4 is required on PATH; this guard must not be skipped')
    result = subprocess.run([binary, '-r', f'.data["{key}"]', str(CONFIGMAP)],
                            capture_output=True, text=True, check=True)
    return result.stdout


def load_module(tmp):
    path = Path(tmp) / 'kohako_board.py'
    path.write_text(configmap_value('kohako_board.py'))
    spec = importlib.util.spec_from_file_location('kohako_board', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ticket(status, assignee, notes):
    return (f'---\nid: X\nstatus: {status}\npriority: high\nassignee: {assignee}\n---\n\n'
            '# title\n\n## Summary\n\nSECRET BODY must not leak\n\n## Notes\n\n' + '\n'.join(notes) + '\n')


class KohakoBoardProjectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.kb = load_module(self.tmp.name)
        self.vault = Path(self.tmp.name) / 'vault'
        (self.vault / 'Boards').mkdir(parents=True)
        (self.vault / 'Tickets').mkdir()

    def write_ticket(self, ticket_id, text, age=timedelta(hours=1)):
        path = self.vault / 'Tickets' / f'{ticket_id}.md'
        path.write_text(text)
        stamp = (NOW - age).timestamp()
        os.utime(path, (stamp, stamp))

    def write_board(self, lanes):
        body = ['---', '', 'kanban-plugin: board', '', '---', '', '## Task Board', '',
                '- [ ] [[Tickets/BOXP-1|BOXP-1: rules lane is ignored]] #ticket', '']
        for lane, cards in lanes.items():
            body += [f'## {lane}', ''] + cards + ['']
        (self.vault / 'Boards' / 'Task Board.md').write_text('\n'.join(body))

    def card(self, n, title, lane_status, assignee='codex'):
        return f'- [ ] [[Tickets/BOXP-{n}|BOXP-{n}: {title}]] #ticket status::{lane_status} priority::high assignee::{assignee} repo::boxp/lolice'

    def project(self):
        return self.kb.build_projection(self.vault, now=NOW)

    def test_returns_only_fixed_fields_and_latest_note_excerpt(self):
        self.write_board({'In Progress': [self.card(10, 'Work', 'in-progress')]})
        self.write_ticket('BOXP-10', ticket('in-progress', 'codex', [
            '- 2026-10-26: older note',
            '- 2026-10-27 05:24:51 UTC [hermes-agent]: latest ' + 'x' * 200,
        ]))
        result = self.project()
        self.assertEqual(result['schema'], 'kohako-board/v1')
        self.assertEqual(set(result), {'schema', 'generatedAt', 'boardUpdatedAt', 'total', 'limit', 'truncated', 'items'})
        [item] = result['items']
        self.assertEqual(set(item), ALLOWED_ITEM_KEYS)
        self.assertEqual(item['id'], 'BOXP-10')
        self.assertEqual(item['title'], 'Work')
        self.assertEqual(item['latestNoteAt'], '2026-10-27T05:24:51Z')
        self.assertEqual(item['latestNoteSource'], 'hermes-agent')
        self.assertTrue(item['progress'].startswith('latest '))
        self.assertLessEqual(len(item['progress']), 80)
        self.assertNotIn('SECRET BODY', str(result))
        self.assertNotIn(str(self.vault), str(result))

    def test_flags_lane_status_and_assignee_mismatch(self):
        self.write_board({'Blocked': [self.card(11, 'Mismatch', 'blocked', 'claude-opus')]})
        self.write_ticket('BOXP-11', ticket('done', 'boxp', ['- 2026-10-27: done?']))
        [item] = self.project()['items']
        self.assertEqual((item['lane'], item['status']), ('Blocked', 'done'))
        self.assertTrue(item['laneStatusMismatch'])
        self.assertEqual((item['assignee'], item['ticketAssignee']), ('claude-opus', 'boxp'))
        self.assertTrue(item['assigneeMismatch'])

    def test_retired_review_lane_is_not_projected(self):
        self.write_board({
            'Review': [self.card(16, 'Left behind', 'review')],
            'Ready': [self.card(17, 'Current', 'ready')],
        })
        self.write_ticket('BOXP-16', ticket('review', 'codex', []))
        self.write_ticket('BOXP-17', ticket('ready', 'codex', []))
        self.assertEqual([item['id'] for item in self.project()['items']], ['BOXP-17'])

    def test_blocker_and_decision_come_from_recent_notes(self):
        self.write_board({'Blocked': [self.card(12, 'Blocked one', 'blocked')]})
        self.write_ticket('BOXP-12', ticket('blocked', 'codex', [
            '- 2026-10-27 01:00:00 UTC [codex]: blocked by missing permission',
            '- 2026-10-27 02:00:00 UTC [codex]: Owner の判断が必要: どちらにするか',
            '- 2026-10-27 03:00:00 UTC [codex]: progress update',
        ]))
        [item] = self.project()['items']
        self.assertIn('missing permission', item['blocker'])
        self.assertIn('判断が必要', item['decision'])
        self.assertEqual(item['progress'], 'progress update')

    def test_latest_note_time_source_and_excerpt_come_from_the_same_entry(self):
        self.write_board({'Blocked': [self.card(15, 'Out of order', 'blocked')]})
        self.write_ticket('BOXP-15', ticket('blocked', 'codex', [
            '- 2026-10-27 09:00:00 UTC [hermes-agent]: newest by time',
            '- 2026-10-27 08:00:00 UTC [codex]: appended later but older',
        ]))
        [item] = self.project()['items']
        self.assertEqual(item['latestNoteAt'], '2026-10-27T09:00:00Z')
        self.assertEqual(item['latestNoteSource'], 'hermes-agent')
        self.assertEqual(item['progress'], 'newest by time')

    def test_masks_urls_emails_secrets_and_long_tokens(self):
        self.write_board({'Ready': [self.card(13, 'Mask', 'ready')]})
        self.write_ticket('BOXP-13', ticket('ready', 'codex', [
            '- 2026-10-27: see https://example.com/path?token=abc owner@example.com api_key=hunter2 '
            'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
        ]))
        progress = self.project()['items'][0]['progress']
        for leaked in ('token=abc', '/path', 'owner@example.com', 'hunter2', 'A' * 32):
            self.assertNotIn(leaked, progress)
        self.assertIn('example.com/…', progress)

    def test_orders_lanes_limits_items_and_skips_old_done(self):
        cards = {'Ready': [], 'In Progress': [], 'Done': []}
        for n in range(100, 140):
            cards['Ready'].append(self.card(n, f'R{n}', 'ready'))
            self.write_ticket(f'BOXP-{n}', ticket('ready', 'codex', []), age=timedelta(minutes=n))
        cards['In Progress'].append(self.card(200, 'Active', 'in-progress'))
        self.write_ticket('BOXP-200', ticket('in-progress', 'codex', []))
        cards['Done'] += [self.card(300, 'Recent done', 'done'), self.card(301, 'Old done', 'done')]
        self.write_ticket('BOXP-300', ticket('done', 'codex', []), age=timedelta(hours=2))
        self.write_ticket('BOXP-301', ticket('done', 'codex', []), age=timedelta(hours=49))
        self.write_board(cards)
        result = self.project()
        self.assertEqual(result['total'], 42)
        self.assertEqual(len(result['items']), 30)
        self.assertTrue(result['truncated'])
        self.assertEqual(result['items'][0]['id'], 'BOXP-200')
        # Within a lane the most recently updated ticket comes first.
        self.assertEqual(result['items'][1]['id'], 'BOXP-100')
        ids = {item['id'] for item in result['items']}
        self.assertNotIn('BOXP-301', ids)
        self.assertNotIn('BOXP-1', ids)

    def test_missing_ticket_is_reported_not_hidden(self):
        self.write_board({'In Progress': [self.card(14, 'Gone', 'in-progress')]})
        [item] = self.project()['items']
        self.assertEqual(item['readError'], 'ticket_missing')

    def test_rejects_ids_that_escape_the_tickets_directory(self):
        self.write_board({'Ready': ['- [ ] [[Tickets/../Boards/Task Board|x]] #ticket']})
        self.assertEqual(self.project()['items'], [])


class KohakoResultsTest(KohakoBoardProjectionTest):
    def setUp(self):
        super().setUp()
        self.runs = Path(self.tmp.name) / 'runs'
        self.runs.mkdir()

    def run_file(self, ticket_id, run_id, result='done', finished='2026-10-27T11:00:00Z', last=''):
        path = self.runs / ticket_id / run_id
        path.mkdir(parents=True)
        (path / 'summary.edn').write_text(f'{{:result "{result}" :started-at "2026-10-27T08:00:00Z" :finished-at "{finished}"}}')
        (path / 'last-message.md').write_text(last)

    def test_done_only_and_manual_without_writes(self):
        self.write_board({'Done': [self.card(1, 'Done', 'done'), self.card(2, 'Mismatch', 'done')],
                          'Blocked': [self.card(3, 'Blocked', 'blocked')], 'Ready': [self.card(4, 'Ready', 'ready')]})
        for n, status in [(1, 'done'), (2, 'blocked'), (3, 'done'), (4, 'done')]:
            self.write_ticket(f'BOXP-{n}', ticket(status, 'codex', []))
        before = {p: p.read_bytes() for p in self.vault.rglob('*') if p.is_file()}
        result = self.kb.build_results(self.vault, self.runs, [f'BOXP-{n}' for n in range(1, 5)])
        self.assertEqual([x['id'] for x in result['items']], ['BOXP-1'])
        self.assertTrue(result['items'][0]['manual'])
        self.assertIsNone(result['items'][0]['summary'])
        self.assertEqual(before, {p: p.read_bytes() for p in self.vault.rglob('*') if p.is_file()})
        with self.assertRaises(ValueError):
            self.kb.build_results(self.vault, self.runs, ['../BOXP-1'])

    def test_latest_done_owner_priority_mask_and_fallback(self):
        self.write_board({'Done': [self.card(1, 'Result', 'done'), self.card(2, 'Other', 'done')]})
        self.write_ticket('BOXP-1', ticket('done', 'codex', [
            '- 2026-10-27 09:00:00 UTC [codex]: Owner向け要約: 完了 /home/boxp/private api_key=secret 未確認',
            '- 2026-10-27 10:00:00 UTC [codex]: another note']))
        self.write_ticket('BOXP-2', ticket('done', 'codex', ['- 2026-10-27 10:00:00 UTC [codex]: fallback note']))
        self.run_file('BOXP-1', '20261027T080000Z-old', finished='2026-10-27T09:00:00Z')
        self.run_file('BOXP-1', '20261027T080000Z-new', last='Long paragraph ' * 10)
        self.run_file('BOXP-1', '20261027T080000Z-failed', result='failed', finished='2026-10-27T12:00:00Z')
        self.run_file('BOXP-2', '20261027T080000Z-test', last='short')
        items = self.kb.build_results(self.vault, self.runs, ['BOXP-1', 'BOXP-2'])['items']
        self.assertEqual(items[0]['runId'], '20261027T080000Z-new')
        self.assertIn('完了', items[0]['summary'])
        self.assertNotIn('/home/', str(items))
        self.assertNotIn('secret', str(items))
        self.assertIn('未確認', items[0]['unverified'])
        self.assertEqual(items[1]['summary'], 'fallback note')

    def test_summary_reads_past_a_pointer_paragraph_and_stops_at_a_heading(self):
        self.write_board({'Done': [self.card(1, 'Result', 'done')]})
        self.write_ticket('BOXP-1', ticket('done', 'codex', []))
        self.run_file('BOXP-1', '20261027T080000Z-a', last=(
            '調査結果を [ノート](/home/boxp/x.md) に保存し、Notes に未確認事項を追記しました。\n\n'
            '現行の設定では一律停止の規則はありません。\n\n## 未確認事項\n- 実機の動作\n\nTASK_BOARD_RESULT: done\n'))
        item = self.kb.build_results(self.vault, self.runs, ['BOXP-1'])['items'][0]
        self.assertEqual(item['summary'], '調査結果を ノート に保存し、Notes に未確認事項を追記しました。 現行の設定では一律停止の規則はありません。')
        self.assertEqual(item['unverified'], '実機の動作')

    def test_unverified_takes_the_sentence_and_skips_pointers(self):
        self.assertEqual(self.kb.find_unverified('作業は終えた。本番では未確認です。次へ進む。'), '本番では未確認です。')
        self.assertEqual(self.kb.find_unverified('Notes に未確認事項を追記しました。'), None)
        self.assertEqual(self.kb.find_unverified('本番では未確認'), '本番では未確認')
        self.assertEqual(self.kb.find_unverified('**未確認・残っている点**\n- 実機の往復'), '実機の往復')
        self.assertEqual(self.kb.find_unverified(''), None)

    def test_no_summary_without_any_source(self):
        self.write_board({'Done': [self.card(1, 'Result', 'done')]})
        self.write_ticket('BOXP-1', ticket('done', 'codex', ['- 2026-10-27 10:00:00 UTC [kohako-event]: handled']))
        self.run_file('BOXP-1', '20261027T080000Z-a', last='短文\n\nTASK_BOARD_RESULT: done')
        self.assertIsNone(self.kb.build_results(self.vault, self.runs, ['BOXP-1'])['items'][0]['summary'])



class KohakoBoardListenerTest(unittest.TestCase):
    def test_results_http_validates_ids_and_methods(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            module = load_module(tmp)
            path = Path(tmp) / 'server.py'
            path.write_text(configmap_value('server.py'))
            spec = importlib.util.spec_from_file_location('dashboard_server', path)
            server = importlib.util.module_from_spec(spec)
            with patch.dict('sys.modules', {'kohako_board': module}):
                spec.loader.exec_module(server)
            handler = object.__new__(server.KohakoBoardHandler)
            sent = []
            handler.send_json = lambda payload, status=200: sent.append((status, payload))
            handler.path = '/api/kohako/results?ids=BOXP-1,../BOXP-2'
            handler.do_GET()
            self.assertEqual(sent[-1][0], 400)
            handler.path = '/api/kohako/results?ids=' + ','.join(['BOXP-1'] * 51)
            handler.do_GET()
            self.assertEqual(sent[-1][0], 400)
            handler.path = '/api/kohako/results?ids=BOXP-1'
            with patch.object(module, 'build_results', side_effect=RuntimeError('secret')):
                handler.do_GET()
            self.assertEqual(sent[-1], (500, {'error': 'results failed'}))
            errors = []
            handler.send_error = errors.append
            handler.do_POST()
            self.assertEqual(errors, [405])

    def test_server_serves_only_projection_on_the_kohako_port(self):
        server = configmap_value('server.py')
        self.assertIn('class KohakoBoardHandler', server)
        handler = server[server.index('class KohakoBoardHandler'):server.index('if __name__')]
        self.assertIn('"/api/kohako/board"', handler)
        self.assertNotIn('/api/runs', handler)
        self.assertNotIn('send_static', handler)


if __name__ == '__main__':
    unittest.main()
