# BOXP-570: tests/workflows の既存 unittest step で実行される。題名はすべて架空の値。
import importlib.util
import os
from pathlib import Path
import tempfile
import time
import unittest

path = Path(__file__).resolve().parents[2] / 'argoproj/codex-workspace/ai-ops-metrics/server.py'
spec = importlib.util.spec_from_file_location('ai_ops_metrics_titles', path)
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class TicketTitleTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.runs, self.tickets = root / 'runs', root / 'Tickets'
        self.runs.mkdir()
        self.tickets.mkdir()
        self.now = time.time()

    def ticket(self, tid, body=None, age_days=1):
        d = self.runs / tid
        d.mkdir()
        t = self.now - age_days * 86400
        os.utime(d, (t, t))
        if body is not None:
            (self.tickets / f'{tid}.md').write_text(body)

    def output(self):
        return server.ticket_title_metrics(str(self.runs), str(self.tickets), self.now).decode()

    def test_title_from_heading(self):
        self.ticket('BOXP-1', '---\nid: BOXP-1\n---\n\n# BOXP-1: 架空の題名\n\n## Notes\n- secret note\n')
        out = self.output()
        self.assertIn('ai_ops_ticket_title_info{ticket="BOXP-1",title="架空の題名"} 1', out)
        self.assertNotIn('secret note', out)

    def test_missing_file_or_heading_keeps_id_without_title(self):
        self.ticket('BOXP-2')
        self.ticket('BOXP-3', 'no heading here\n')
        out = self.output()
        self.assertIn('ai_ops_ticket_title_info{ticket="BOXP-2",title=""} 1', out)
        self.assertIn('ai_ops_ticket_title_info{ticket="BOXP-3",title=""} 1', out)

    def test_every_ticket_file_and_run_is_listed_regardless_of_age(self):
        # 選択欄から Langfuse の利用記録がある票を落とさない: run の新旧や有無に関係なく出す
        self.ticket('BOXP-4', '# BOXP-4: 古い run\n', age_days=400)
        (self.tickets / 'BOXP-6.md').write_text('# BOXP-6: run のない票\n')
        (self.runs / 'not-a-ticket').mkdir()
        (self.tickets / 'README.md').write_text('# x\n')
        out = self.output()
        self.assertIn('ai_ops_ticket_title_info{ticket="BOXP-4",title="古い run"} 1', out)
        self.assertIn('ai_ops_ticket_title_info{ticket="BOXP-6",title="run のない票"} 1', out)
        self.assertNotIn('not-a-ticket', out)
        self.assertNotIn('README', out)
        self.assertIn('ai_ops_ticket_title_truncated 0', out)

    def test_newest_ids_first(self):
        for tid in ('BOXP-9', 'BOXP-100', 'BOXP-20'):
            self.ticket(tid, f'# {tid}: t\n')
        order = [l.split('"')[1] for l in self.output().splitlines() if l.startswith('ai_ops_ticket_title_info{')]
        self.assertEqual(order, ['BOXP-100', 'BOXP-20', 'BOXP-9'])

    def test_label_value_is_sanitized_and_bounded(self):
        self.ticket('BOXP-5', '# BOXP-5: a"b\\c\td ' + 'x' * 100 + '\n')
        line = next(l for l in self.output().splitlines() if 'BOXP-5' in l)
        title = line.split('title="', 1)[1].rsplit('"}', 1)[0]
        self.assertNotIn('"', title)
        self.assertNotIn('\\', title)
        self.assertNotIn('\t', title)
        self.assertEqual(len(title), server.TICKET_TITLE_CHARS)

    def test_series_limit(self):
        for i in range(3):
            self.ticket(f'BOXP-{10 + i}', f'# BOXP-{10 + i}: t\n')
        old = server.TICKET_TITLE_LIMIT
        server.TICKET_TITLE_LIMIT = 2
        self.addCleanup(setattr, server, 'TICKET_TITLE_LIMIT', old)
        out = self.output()
        self.assertEqual(out.count('ai_ops_ticket_title_info{'), 2)
        self.assertIn('ai_ops_ticket_title_truncated 1', out)


if __name__ == '__main__':
    unittest.main()
