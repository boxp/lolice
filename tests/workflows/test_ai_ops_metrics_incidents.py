# BOXP-769: 障害記録 → lolice_incident_* の判定（仕様: Obsidian Projects/lolice-incident-recovery/spec.md 2 章の例）。
# 期待値は仕様の例から書いたもの。記録はすべて架空。
import importlib.util
from pathlib import Path
import re
import tempfile
import unittest

path = Path(__file__).resolve().parents[2] / 'argoproj/codex-workspace/ai-ops-metrics/server.py'
spec = importlib.util.spec_from_file_location('ai_ops_metrics_incidents', path)
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)

NOW = 1795910400  # 2026-11-29T00:00:00Z（固定の「今」）


def fm(**kv):
    lines = ['---']
    for k, v in kv.items():
        lines.append(f'{k}: {v}')
    lines += ['---', '', '# INC-x: 架空の題名', '', '## Notes', '- 根拠の本文は出さない secret-body']
    return '\n'.join(lines) + '\n'


SAMPLE = r'^[a-z_]+(\{([a-z_]+="(?:[^"\\\n]|\\.)*",?)*\})? -?\d+(\.\d+)?$'

V1 = {'schema': 'incident/v1', 'type': 'incident', 'service': 'svc-a'}


class IncidentMetricsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / 'Tickets').mkdir()
        (self.root / 'Synthetic').mkdir()

    def write(self, name, **kv):
        sub = 'Synthetic' if name.startswith('SYN-') else 'Tickets'
        (self.root / sub / f'{name}.md').write_text(fm(**kv))

    def out(self):
        out = server.incident_metrics(str(self.root), NOW).decode()
        # どの出力も Prometheus のテキスト形式として読めること（1 行でも壊れるとスクレイプ全体が失敗する）
        for line in out.splitlines():
            if line.startswith("#"):
                continue
            self.assertRegex(line, SAMPLE)
        return out

    def value(self, out, metric, **labels):
        for line in out.splitlines():
            if not line.startswith(metric + '{'):
                continue
            got = dict(re.findall(r'(\w+)="([^"]*)"', line.split('}')[0]))
            if all(got.get(k) == v for k, v in labels.items()):
                return float(line.rsplit(' ', 1)[1])
        return None

    def info(self, out, inc):
        for line in out.splitlines():
            if line.startswith('lolice_incident_info{') and f'incident="{inc}"' in line:
                return dict(re.findall(r'(\w+)="([^"]*)"', line.split('}')[0]))
        return None

    # 例 1: 配備起因、rollback、確認つき解消 → 一般 30 分、DORA 40 分
    def test_deployment_caused_recovered(self):
        self.write('INC-10', **V1, started='2026-11-28T10:00:00Z', detected='2026-11-28T10:05:00Z',
                   cause='deployment', cause_app='app-x', cause_revision='abc1234',
                   cause_deployed_at='2026-11-28T09:50:00Z', cause_evidence='"argocd history id=12"',
                   recovery='rollback', recovery_at='2026-11-28T10:20:00Z', recovery_evidence='"argocd rollback"',
                   impact_resolved='2026-11-28T10:30:00Z', impact_resolved_evidence='"外形監視 200 を 3 回"')
        out = self.out()
        i = self.info(out, 'INC-10')
        self.assertEqual(i['state'], 'counted')
        self.assertEqual(i['recovery_state'], 'recovered')
        self.assertEqual(i['cause_state'], 'deployment')
        self.assertEqual(i['recovery'], 'rollback')
        self.assertEqual(i['redeploy'], 'true')
        self.assertEqual(i['synthetic'], 'false')
        self.assertEqual(self.value(out, 'lolice_incident_restore_seconds', incident='INC-10'), 1800)
        self.assertEqual(self.value(out, 'lolice_incident_failed_deployment_recovery_seconds',
                                    incident='INC-10', app='app-x'), 2400)
        self.assertEqual(self.value(out, 'lolice_incident_timestamp_seconds', incident='INC-10', point='cause_deployed'),
                         server.parse_incident_time('2026-11-28T09:50:00Z'))
        self.assertNotIn('secret-body', out)
        self.assertNotIn('外形監視', out)

    # 例 2: 配備によらない障害、再配備なしの再起動 → 一般 15 分、DORA は対象外
    def test_non_deployment_restart(self):
        self.write('INC-11', **V1, started='2026-11-28 10:00', cause='non_deployment', cause_evidence='"node 故障"',
                   recovery='restart', recovery_at='2026-11-28T10:10:00Z',
                   impact_resolved='2026-11-28T10:15:00Z', impact_resolved_evidence='"疎通確認"')
        out = self.out()
        i = self.info(out, 'INC-11')
        self.assertEqual((i['recovery_state'], i['cause_state'], i['redeploy']), ('recovered', 'non_deployment', 'false'))
        self.assertEqual(self.value(out, 'lolice_incident_restore_seconds', incident='INC-11'), 900)
        self.assertIsNone(self.value(out, 'lolice_incident_failed_deployment_recovery_seconds', incident='INC-11'))

    # 例 3: 原因 = 配備だが根拠なし → deployment_unconfirmed、DORA を出さない
    def test_deployment_without_evidence(self):
        self.write('INC-12', **V1, started='2026-11-28T10:00:00Z', cause='deployment', cause_app='app-x',
                   cause_deployed_at='2026-11-28T09:00:00Z',
                   impact_resolved='2026-11-28T11:00:00Z', impact_resolved_evidence='"確認"')
        out = self.out()
        self.assertEqual(self.info(out, 'INC-12')['cause_state'], 'deployment_unconfirmed')
        self.assertEqual(self.value(out, 'lolice_incident_restore_seconds', incident='INC-12'), 3600)
        self.assertIsNone(self.value(out, 'lolice_incident_failed_deployment_recovery_seconds', incident='INC-12'))
        self.assertEqual(self.value(out, 'lolice_incident_record_problem', incident='INC-12', problem='cause_incomplete'), 1)

    # 例 4: アラートが resolved になっただけ（impact_resolved 空）→ 未復旧
    def test_alert_resolved_only_is_unrecovered(self):
        self.write('INC-13', **V1, started='2026-11-28T10:00:00Z', recovery='self_healed',
                   recovery_at='2026-11-28T10:05:00Z', recovery_evidence='"alert resolved"')
        out = self.out()
        i = self.info(out, 'INC-13')
        self.assertEqual((i['recovery_state'], i['cause_state']), ('unrecovered', 'unknown'))
        self.assertIsNone(self.value(out, 'lolice_incident_restore_seconds', incident='INC-13'))

    # 例 5: 解消時刻だけで根拠が空 → unconfirmed
    def test_resolved_without_evidence(self):
        self.write('INC-14', **V1, started='2026-11-28T10:00:00Z', impact_resolved='2026-11-28T10:30:00Z')
        out = self.out()
        self.assertEqual(self.info(out, 'INC-14')['recovery_state'], 'unconfirmed')
        self.assertIsNone(self.value(out, 'lolice_incident_restore_seconds', incident='INC-14'))
        self.assertEqual(self.value(out, 'lolice_incident_record_problem', incident='INC-14',
                                    problem='resolved_without_evidence'), 1)

    # 例 6: 重複 → duplicate、数えない。先の記録が無ければ invalid
    def test_duplicate(self):
        self.write('INC-15', **V1, started='2026-11-28T10:00:00Z')
        self.write('INC-16', **V1, started='2026-11-28T10:01:00Z', duplicate_of='INC-15',
                   impact_resolved='2026-11-28T10:30:00Z', impact_resolved_evidence='"確認"')
        self.write('INC-17', **V1, duplicate_of='INC-99')
        out = self.out()
        self.assertEqual(self.info(out, 'INC-16')['state'], 'duplicate')
        self.assertIsNone(self.value(out, 'lolice_incident_restore_seconds', incident='INC-16'))
        # 重複先が無い → 除外の根拠が無いので数えたまま問題を付ける（fail-closed）
        self.assertEqual(self.info(out, 'INC-17')['state'], 'counted')
        self.assertEqual(self.value(out, 'lolice_incident_record_problem', incident='INC-17',
                                    problem='duplicate_target_missing'), 1)
        self.assertEqual(self.value(out, 'lolice_incident_records', state='duplicate'), 1)

    # 例 6 の続き: 重複先がそれ自体重複（循環・連鎖）→ どちらも数えたまま duplicate_target_invalid
    def test_duplicate_cycle_and_chain_are_counted(self):
        done = dict(started='2026-11-28T10:00:00Z', impact_resolved='2026-11-28T10:30:00Z',
                    impact_resolved_evidence='"確認"')
        self.write('INC-30', **V1, **done, duplicate_of='INC-31')
        self.write('INC-31', **V1, **done, duplicate_of='INC-30')
        self.write('INC-32', **V1, **done)
        self.write('INC-33', **V1, **done, duplicate_of='INC-32')
        self.write('INC-34', **V1, **done, duplicate_of='INC-33')
        self.write('INC-35', **V1, **done, duplicate_of='INC-35')
        out = self.out()
        for inc in ('INC-30', 'INC-31', 'INC-34', 'INC-35'):
            self.assertEqual(self.info(out, inc)['state'], 'counted', inc)
            self.assertEqual(self.value(out, 'lolice_incident_record_problem', incident=inc,
                                        problem='duplicate_target_invalid'), 1, inc)
            self.assertEqual(self.value(out, 'lolice_incident_restore_seconds', incident=inc), 1800, inc)
        self.assertEqual(self.info(out, 'INC-33')['state'], 'duplicate')
        self.assertEqual(self.value(out, 'lolice_incident_records', state='duplicate'), 1)

    # 例 6 の続き: 重複先が数える対象でない（本番→合成、合成→本番、invalid、legacy）→ 除外の根拠が無いので数えたまま
    def test_duplicate_target_must_be_countable_same_population(self):
        done = dict(started='2026-11-28T10:00:00Z', impact_resolved='2026-11-28T10:30:00Z',
                    impact_resolved_evidence='"確認"')
        self.write('SYN-50', **V1, **done)
        self.write('INC-51', **V1, **done)
        self.write('INC-52', **V1, started='bad-time')
        self.write('INC-1', type='incident', status='resolved')
        self.write('INC-53', **V1, **done, synthetic='true')
        self.write('INC-60', **V1, **done, duplicate_of='SYN-50')   # 本番 → 合成
        self.write('SYN-61', **V1, **done, duplicate_of='INC-51')   # 合成 → 本番
        self.write('INC-62', **V1, **done, duplicate_of='INC-52')   # → invalid
        self.write('INC-63', **V1, **done, duplicate_of='INC-1')    # → legacy
        self.write('INC-64', **V1, **done, duplicate_of='INC-53')   # → synthetic=true の INC
        self.write('INC-65', **V1, **done, duplicate_of='INC-51')   # 正しい重複
        out = self.out()
        for inc in ('INC-60', 'SYN-61', 'INC-62', 'INC-63', 'INC-64'):
            self.assertEqual(self.info(out, inc)['state'], 'counted', inc)
            self.assertEqual(self.value(out, 'lolice_incident_record_problem', incident=inc,
                                        problem='duplicate_target_invalid'), 1, inc)
            self.assertEqual(self.value(out, 'lolice_incident_restore_seconds', incident=inc), 1800, inc)
        self.assertEqual(self.info(out, 'INC-65')['state'], 'duplicate')
        self.assertEqual(self.value(out, 'lolice_incident_records', state='duplicate'), 1)

    # 2 章 0: YAML の空値（'' / "" / null / ~ / コメントだけ / 何も無い）は空欄。根拠が無いので復旧確認済みにしない
    def test_yaml_empty_values_are_blank(self):
        empties = ["''", '""', 'null', 'Null', 'NULL', '~', '# 未確認', '']
        for n, v in enumerate(empties):
            self.write(f'INC-{70 + n}', **V1, started='2026-11-28T10:00:00Z', impact_resolved='2026-11-28T10:30:00Z',
                       impact_resolved_evidence=v, cause='deployment', cause_app='app-x',
                       cause_deployed_at='2026-11-28T09:00:00Z', cause_evidence=v, duplicate_of=v)
        out = self.out()
        for n, v in enumerate(empties):
            inc = f'INC-{70 + n}'
            i = self.info(out, inc)
            self.assertEqual((i['state'], i['recovery_state'], i['cause_state']),
                             ('counted', 'unconfirmed', 'deployment_unconfirmed'), v)
            self.assertIsNone(self.value(out, 'lolice_incident_restore_seconds', incident=inc), v)
            self.assertIsNone(self.value(out, 'lolice_incident_failed_deployment_recovery_seconds', incident=inc), v)
            self.assertIsNone(self.value(out, 'lolice_incident_record_problem', incident=inc,
                                         problem='duplicate_target_missing'), v)

    # 2 章 0: 単引用符の文字列と行末コメントは YAML どおりに読む
    def test_yaml_single_quote_and_comment(self):
        self.write('INC-80', **V1, started="'2026-11-28T10:00:00Z'", impact_resolved='2026-11-28T10:30:00Z # 確認',
                   impact_resolved_evidence="'外形監視 ''200'' を確認'")
        out = self.out()
        self.assertEqual(self.info(out, 'INC-80')['recovery_state'], 'recovered')
        self.assertEqual(self.value(out, 'lolice_incident_restore_seconds', incident='INC-80'), 1800)

    # 2 章 0: 解釈しない YAML の書き方（複数行・リスト・辞書・アンカー・タグ・閉じない引用符）は空と見なさず不正な値
    def test_yaml_unsupported_syntax_is_bad_value(self):
        odd = ['|', '>-', '[a]', '{a: b}', '&x foo', '*x', '!!str x', "'閉じない", '"閉じない']
        for n, v in enumerate(odd):
            self.write(f'INC-{90 + n}', **V1, started='2026-11-28T10:00:00Z', impact_resolved='2026-11-28T10:30:00Z',
                       impact_resolved_evidence=v)
        out = self.out()
        for n, v in enumerate(odd):
            inc = f'INC-{90 + n}'
            self.assertEqual(self.info(out, inc)['state'], 'invalid', v)
            self.assertEqual(self.value(out, 'lolice_incident_record_problem', incident=inc, problem='bad_value'), 1, v)
            self.assertIsNone(self.value(out, 'lolice_incident_restore_seconds', incident=inc), v)

    # 例 7: schema 無し（INC-1〜5 のような既存記録）→ legacy、数えない
    def test_legacy(self):
        self.write('INC-1', type='incident', status='resolved', started='2026-08-19 02:10', resolved='2026-08-19 11:30')
        out = self.out()
        i = self.info(out, 'INC-1')
        self.assertEqual(i['state'], 'legacy')
        self.assertIsNone(self.value(out, 'lolice_incident_restore_seconds', incident='INC-1'))
        self.assertEqual(self.value(out, 'lolice_incident_records', state='legacy'), 1)

    # 例 7 の続き: legacy でも列挙外の cause/recovery はそのままラベルに出さない（"" を含むとスクレイプ全体が壊れる）
    def test_legacy_out_of_enum_values_are_not_emitted(self):
        self.write('INC-2', type='incident', recovery='"a\\"b"', cause='x"y')
        out = self.out()
        i = self.info(out, 'INC-2')
        self.assertEqual(i['state'], 'legacy')
        self.assertEqual((i['recovery'], i['cause_state'], i['redeploy']), ('unknown', 'legacy', 'unknown'))
        line = next(l for l in out.splitlines() if l.startswith('lolice_incident_info{') and 'INC-2' in l)
        labels = line[line.index('{') + 1:line.rindex('}')]
        self.assertRegex(labels, r'^(\w+="[^"\\\n]*",)*\w+="[^"\\\n]*"$')

    # 2 章の 2: 読めない時刻・列挙外の値 → invalid
    def test_invalid_values(self):
        self.write('INC-18', **V1, started='きのう')
        self.write('INC-19', **V1, cause='maybe')
        out = self.out()
        self.assertEqual(self.info(out, 'INC-18')['state'], 'invalid')
        self.assertEqual(self.value(out, 'lolice_incident_record_problem', incident='INC-18', problem='bad_time'), 1)
        self.assertEqual(self.info(out, 'INC-19')['state'], 'invalid')
        self.assertEqual(self.value(out, 'lolice_incident_record_problem', incident='INC-19', problem='bad_value'), 1)

    # 2 章の 4: 解消が影響開始・原因配備より前 → invalid（時刻の順）
    def test_time_order(self):
        self.write('INC-20', **V1, started='2026-11-28T10:00:00Z',
                   impact_resolved='2026-11-28T09:00:00Z', impact_resolved_evidence='"確認"')
        out = self.out()
        self.assertEqual(self.info(out, 'INC-20')['state'], 'invalid')
        self.assertEqual(self.value(out, 'lolice_incident_record_problem', incident='INC-20', problem='time_order'), 1)
        self.assertIsNone(self.value(out, 'lolice_incident_restore_seconds', incident='INC-20'))

    # 2 章の 7: 同じ dedupe_key で期間が重なる → 後の記録に possible_duplicate（数えはする）
    def test_possible_duplicate(self):
        self.write('INC-21', **V1, dedupe_key='svc-a', started='2026-11-28T10:00:00Z')
        self.write('INC-22', **V1, dedupe_key='svc-a', started='2026-11-28T10:10:00Z')
        self.write('INC-23', **V1, dedupe_key='svc-b', started='2026-11-28T10:10:00Z')
        self.write('INC-24', **V1, dedupe_key='svc-c', started='2026-11-27T10:00:00Z',
                   impact_resolved='2026-11-27T11:00:00Z', impact_resolved_evidence='"確認"')
        self.write('INC-25', **V1, dedupe_key='svc-c', started='2026-11-28T10:00:00Z')
        out = self.out()
        self.assertEqual(self.value(out, 'lolice_incident_record_problem', incident='INC-22', problem='possible_duplicate'), 1)
        self.assertIsNone(self.value(out, 'lolice_incident_record_problem', incident='INC-21', problem='possible_duplicate'))
        self.assertIsNone(self.value(out, 'lolice_incident_record_problem', incident='INC-23', problem='possible_duplicate'))
        self.assertIsNone(self.value(out, 'lolice_incident_record_problem', incident='INC-25', problem='possible_duplicate'))
        self.assertEqual(self.info(out, 'INC-22')['state'], 'counted')

    # 合成事例は Synthetic/SYN-N.md、synthetic=true
    def test_synthetic(self):
        self.write('SYN-1', **V1, started='2026-11-28T10:00:00Z', cause='deployment', cause_app='app-x',
                   cause_deployed_at='2026-11-28T09:55:00Z', cause_evidence='"合成"',
                   impact_resolved='2026-11-28T10:10:00Z', impact_resolved_evidence='"合成"')
        out = self.out()
        self.assertEqual(self.info(out, 'SYN-1')['synthetic'], 'true')
        self.assertEqual(self.value(out, 'lolice_incident_failed_deployment_recovery_seconds',
                                    incident='SYN-1', synthetic='true'), 900)

    def test_dir_missing(self):
        out = server.incident_metrics(str(self.root / 'nope'), NOW).decode()
        self.assertIn('lolice_incident_dir_present 0', out)
        out = self.out()
        self.assertIn('lolice_incident_dir_present 1', out)
        self.assertIn('lolice_incident_metrics_generated_timestamp_seconds', out)

    def test_tickets_unreadable_is_not_present(self):
        # 合成事例だけ読めても本番の Tickets が読めなければ収集失敗
        (self.root / 'Tickets').rmdir()
        self.write('SYN-1', **V1, started='2026-11-28T10:00:00Z')
        self.assertIn('lolice_incident_dir_present 0', self.out())

    def test_unreadable_record_is_not_silent(self):
        # 仕様 4 章: 本番の記録が 1 件でも読めなければ収集失敗（件数は「不明」）。黙って除外しない。
        self.write('INC-10', **V1, started='2026-11-28T10:00:00Z')
        (self.root / 'Tickets' / 'INC-11.md').mkdir()  # 読めない記録（open で OSError）
        out = self.out()
        self.assertIn('lolice_incident_dir_present 0', out)
        self.assertEqual(self.value(out, 'lolice_incident_unreadable_records', synthetic='false'), 1)
        self.assertEqual(self.value(out, 'lolice_incident_record_problem', incident='INC-11', problem='unreadable'), 1)

    def test_unreadable_synthetic_keeps_production_present(self):
        # 合成事例の読み取り失敗は問題として出すが、本番の収集成否は変えない
        (self.root / 'Synthetic' / 'SYN-2.md').mkdir()
        out = self.out()
        self.assertIn('lolice_incident_dir_present 1', out)
        self.assertEqual(self.value(out, 'lolice_incident_unreadable_records', synthetic='true'), 1)
        self.assertEqual(self.value(out, 'lolice_incident_unreadable_records', synthetic='false'), 0)


    # 仕様 4 章 fail-closed: 本番の記録を正しく数えられたと言えないときは理由にかかわらず dir_present 0
    def test_unterminated_frontmatter_is_incomplete(self):
        (self.root / "Tickets" / "INC-12.md").write_text("---\nschema: incident/v1\nservice: svc-a\n")
        out = self.out()
        self.assertIn("lolice_incident_dir_present 0", out)
        self.assertEqual(self.value(out, "lolice_incident_incomplete", reason="unterminated", synthetic="false"), 1)

    def test_unterminated_synthetic_keeps_production_present(self):
        (self.root / "Synthetic" / "SYN-3.md").write_text("---\nschema: incident/v1\n")
        out = self.out()
        self.assertIn("lolice_incident_dir_present 1", out)
        self.assertEqual(self.value(out, "lolice_incident_incomplete", reason="unterminated", synthetic="true"), 1)

    # 4 章: frontmatter の開始区切りが無い記録は legacy と見なさず収集失敗（破損で黙って 0 件にしない）
    def test_missing_frontmatter_is_incomplete(self):
        (self.root / 'Tickets' / 'INC-40.md').write_text('# INC-40: 区切りが消えた\nschema: incident/v1\n')
        (self.root / 'Tickets' / 'INC-41.md').write_text('')
        out = self.out()
        self.assertEqual(self.value(out, 'lolice_incident_incomplete', reason='no_frontmatter', synthetic='false'), 2)
        self.assertIn('lolice_incident_dir_present 0', out)
        self.assertNotEqual(self.info(out, 'INC-40')['state'], 'legacy')
        self.assertEqual(self.value(out, 'lolice_incident_records', state='legacy'), 0)

    def test_missing_frontmatter_synthetic_keeps_production_present(self):
        (self.root / 'Synthetic' / 'SYN-9.md').write_text('壊れた合成事例\n')
        out = self.out()
        self.assertEqual(self.value(out, 'lolice_incident_incomplete', reason='no_frontmatter', synthetic='true'), 1)
        self.assertIn('lolice_incident_dir_present 1', out)

    def test_bom_frontmatter_is_read(self):
        (self.root / 'Tickets' / 'INC-42.md').write_text('\ufeff' + fm(type='incident', status='resolved'))
        out = self.out()
        self.assertEqual(self.info(out, 'INC-42')['state'], 'legacy')
        self.assertIn('lolice_incident_dir_present 1', out)

    def test_bad_name_is_incomplete(self):
        (self.root / "Tickets" / "INC-1234567.md").write_text(fm(**V1))
        (self.root / "Tickets" / "README.md").write_text("# 置き場の説明\n")
        out = self.out()
        self.assertIn("lolice_incident_dir_present 0", out)
        self.assertEqual(self.value(out, "lolice_incident_incomplete", reason="bad_name", synthetic="false"), 1)

    def test_readme_alone_keeps_present(self):
        (self.root / "Tickets" / "README.md").write_text("# 置き場の説明\n")
        self.assertIn("lolice_incident_dir_present 1", self.out())

    def test_over_limit_is_incomplete_and_counts_all(self):
        old = server.INCIDENT_LIMIT
        server.INCIDENT_LIMIT = 2
        self.addCleanup(setattr, server, "INCIDENT_LIMIT", old)
        for i in (1, 2, 3):
            self.write(f"INC-{i}", **V1, started="2026-11-28T10:00:00Z")
        out = self.out()
        self.assertIn("lolice_incident_dir_present 0", out)
        self.assertEqual(self.value(out, "lolice_incident_incomplete", reason="over_limit", synthetic="false"), 1)
        self.assertEqual(self.value(out, "lolice_incident_records", state="counted"), 3)

    def test_label_values_are_escaped(self):
        self.assertEqual(server.label_value("a\"b\\c\nd"), "a\\\"b\\\\c\\nd")

    def test_complete_reports_zero_incomplete(self):
        self.write("INC-1", **V1, started="2026-11-28T10:00:00Z")
        out = self.out()
        self.assertIn("lolice_incident_dir_present 1", out)
        self.assertEqual(self.value(out, "lolice_incident_incomplete", reason="over_limit", synthetic="false"), 0)


if __name__ == '__main__':
    unittest.main()
