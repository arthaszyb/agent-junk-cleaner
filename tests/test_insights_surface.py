"""Insights through its three doors: App.insights, GET /api/insights and `disk-steward insights`, plus the cost of
building it and the promise that it only reads."""
from __future__ import annotations

import contextlib
import io
import json
import time
import unittest
from urllib.parse import quote

import m2_fixture as fx
from disk_steward import i18n
from disk_steward.cli import main
from disk_steward.model import ErrorCode, StewardError
from disk_steward.portal.server import PortalServer
from disk_steward.report import fmt_bytes
from disk_steward.settings import Settings
from insights_support import GIB, MIB
from m3_support import NOW, make_app, make_mac
from portal_support import Client
from support import Clock, FakeHome, render_error
from test_insights import InsightsCase

TOP_KEYS = {'generated_at', 'window_days', 'environment', 'kpis', 'composition', 'history', 'growth', 'agents',
            'todos'}


def tearDownModule():
    if i18n.MISSING:
        raise AssertionError(f'untranslated keys: {sorted(i18n.MISSING)}')


class Seeded(InsightsCase):
    """History with something in every section of the page."""

    def seed(self):
        self.mac.running = {'com.openai.codex'}
        self.synth.scan(40, {'codex.logs': 9 * GIB, 'codex.runtimes': 2 * GIB, 'ollama.models': 4 * GIB,
                             'claude.projects:~/.claude/projects/x': 100 * MIB}, free=120 * 10 ** 9)
        self.synth.scan(12, {'codex.logs': 5 * GIB, 'codex.runtimes': 2 * GIB, 'dev.homebrew': GIB,
                             'claude.projects:~/.claude/projects/x': 400 * MIB}, free=170 * 10 ** 9)
        self.synth.scan(0, {'codex.logs': 4 * GIB, 'codex.browser-cache': 30 * MIB, 'codex.runtimes': 3 * GIB,
                            'codex.sessions:duplicates': 8 * MIB, 'dev.homebrew': GIB, 'codex.state': GIB,
                            'claude.projects:~/.claude/projects/x': 2 * GIB, 'unknown:~/.gemini/tmp': 5 * MIB},
                        free=200 * 10 ** 9)
        self.cleanup('p1', [('done', 'delete', 6 * GIB, 6 * GIB), ('done', 'trash', MIB, 0),
                            ('skipped', 'delete', 10, 0)], measured=2 * GIB, estimated=6 * GIB, at_days_ago=11)


class ServiceTests(Seeded):
    def test_the_answer_has_exactly_the_documented_sections(self):
        self.seed()
        data = self.build()
        self.assertEqual(set(data), TOP_KEYS)
        self.assertEqual((data['generated_at'], data['window_days']), (NOW, 30))
        self.assertEqual(set(data['kpis']), {'free', 'scanned', 'freed_total', 'safe_to_clean', 'growth_per_day'})
        self.assertEqual(set(data['history']), {'scans', 'cleanups'})
        self.assertEqual(set(data['growth']), {'up', 'down'})
        self.assertEqual(set(data['kpis']['free']), {'value', 'delta', 'series'})
        self.assertEqual(set(data['kpis']['scanned']), {'value', 'delta', 'series'})
        self.assertEqual(set(data['kpis']['freed_total']), {'value', 'count', 'since'})
        self.assertEqual(set(data['kpis']['safe_to_clean']), {'bytes', 'items', 'days'})
        self.assertEqual(set(data['history']['scans'][0]), {'id', 't', 'used', 'free', 'scanned', 'junk', 'review',
                                                            'protected', 'report_only', 'backfilled'})
        self.assertEqual(set(data['growth']['up'][0]), {'group', 'label', 'domain', 'tier', 'delta', 'per_day',
                                                        'now', 'before'})
        self.assertEqual(set(data['agents'][0]), {'provider', 'label', 'bytes', 'delta'})
        self.assertEqual(set(data['composition'][0]), {'key', 'bytes', 'label'})
        json.dumps(data)   # nothing but JSON types

    def test_the_locale_defaults_to_the_apps_and_can_be_chosen(self):
        self.seed()
        self.assertEqual(self.app.insights(30)['composition'][0]['label'], 'Free space')
        self.assertEqual(self.app.insights(30, 'zh-CN')['composition'][0]['label'], '可用空间')
        self.app.locale = 'zh-CN'
        self.assertEqual(self.app.insights(30)['composition'][0]['label'], '可用空间')

    def test_a_bad_window_or_locale_is_refused(self):
        for window in (True, -1, '30', 1.5, None):
            with self.subTest(window=window):
                with self.assertRaises(StewardError) as cm:
                    self.app.insights(window)
                self.assertEqual(cm.exception.code, ErrorCode.INVALID)
                render_error(cm.exception)
        with self.assertRaises(StewardError) as cm:
            self.app.insights(30, 'fr')
        self.assertEqual(cm.exception.message_key, 'error.invalid_locale')

    def test_it_only_reads(self):
        self.seed()
        conn = self.app.store._conn()

        def state():
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
            return {t: conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in tables}, \
                [tuple(r) for r in conn.execute('SELECT * FROM meta ORDER BY key')]
        before = state()
        for window in (7, 30, 0):
            self.build(window)
        self.assertEqual(state(), before)
        self.assertEqual(before[0]['specs'], 0)          # not even the "safe to clean" selection is saved
        self.assertFalse([c for c in self.mac.calls if c[0] in ('open_files', 'last_used', 'installed_apps')])

    def test_the_numbers_on_the_page_are_the_overviews(self):
        fh = FakeHome(Clock()).__enter__()
        self.addCleanup(fh.cleanup)
        fx.build(fh)
        app = make_app(fh, mac=make_mac(snapshot_names=['com.apple.1']), settings=Settings(domains=('agent', 'app-library')))
        self.addCleanup(app.close)
        app.scan()
        data, overview = app.insights(30, 'en'), app.overview()
        offered = overview['offered']
        self.assertEqual(data['kpis']['safe_to_clean'], {'bytes': offered['alloc'], 'items': offered['items'],
                                                         'days': offered['min_age_days']})
        self.assertGreater(offered['alloc'], 0)
        self.assertEqual(data['environment']['last_scan']['items'], overview['scan']['items'])
        by = {c['key']: c['bytes'] for c in data['composition']}
        nope = overview['not_included']
        self.assertEqual(by['review'], nope['review']['alloc'])
        self.assertEqual(by['report_only'], nope['report_only']['alloc'])
        self.assertEqual(by['protected'], nope['protected']['alloc'])
        self.assertEqual(by['junk'], overview['totals']['by_tier']['junk']['alloc'])
        self.assertEqual(sum(by.values()), overview['volume']['total'])
        self.assertEqual(data['kpis']['scanned']['value'], overview['volume']['attributed'])


class ApiTests(Seeded):
    def setUp(self):
        super().setUp()
        self.server = PortalServer(self.app, 0)
        self.server.start()
        self.addCleanup(self.server.stop)
        self.c = Client(self.server).login()
        self.seed()

    def error(self, resp, field):
        self.assertEqual(resp[0], 400, resp[1])
        err = resp[1]['error']
        self.assertEqual((err['code'], err['message_key'], err['details']), ('INVALID', 'portal.error.bad_field',
                                                                            {'field': field}))
        self.assertTrue(err['message'])

    def test_the_four_windows_are_served(self):
        for window in (0, 7, 30, 90):
            with self.subTest(window=window):
                status, body = self.c.get(f'/api/insights?window={window}')
                self.assertEqual(status, 200, body)
                self.assertEqual((body['window_days'], set(body)), (window, TOP_KEYS))
        self.assertEqual(self.c.get('/api/insights')[1]['window_days'], 30)      # the default

    def test_the_window_decides_what_is_in_it(self):
        counts = {w: len(self.c.get(f'/api/insights?window={w}')[1]['history']['scans']) for w in (7, 30, 90, 0)}
        self.assertEqual(counts, {7: 1, 30: 2, 90: 3, 0: 3})

    def test_anything_else_is_a_bad_field(self):
        for value in ('5', '', 'abc', '-1', '30.0', '1e1', '3000', '0x10', 'None', '7;8'):
            with self.subTest(window=value):
                self.error(self.c.get('/api/insights?window=' + quote(value)), 'window')

    def test_the_texts_follow_the_requests_language(self):
        en = self.c.get('/api/insights')[1]
        zh = self.c.get('/api/insights', headers={'Accept-Language': 'zh-CN,zh;q=0.9'})[1]
        self.assertEqual([c['label'] for c in en['composition']][:2], ['Free space', 'Not scanned'])
        self.assertEqual([c['label'] for c in zh['composition']][:2], ['可用空间', '未扫描'])
        self.assertEqual([t['id'] for t in en['todos']], [t['id'] for t in zh['todos']])
        self.assertTrue(all(t['title'] != u['title'] for t, u in zip(en['todos'], zh['todos'])))
        self.assertEqual(self.c.get('/api/insights', headers={'Accept-Language': 'fr'})[1]['composition'][0]['label'],
                         'Free space')
        self.app.update_settings({'locale': 'zh-CN'})             # the user's own choice wins over the header
        chosen = self.c.get('/api/insights', headers={'Accept-Language': 'en'})[1]
        self.assertEqual(chosen['composition'][0]['label'], '可用空间')

    def test_it_needs_the_session_and_only_answers_get(self):
        self.assertEqual(self.c.get('/api/insights', token=False)[0], 401)
        self.assertEqual(self.c.post('/api/insights', {})[0], 405)

    def test_the_answer_is_the_services(self):
        body = self.c.get('/api/insights?window=30')[1]
        direct = self.app.insights(30, 'en')
        for key in ('kpis', 'composition', 'history', 'growth', 'agents', 'todos', 'generated_at'):
            self.assertEqual(body[key], json.loads(json.dumps(direct[key])), key)


class CliTests(Seeded):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main(list(argv), app_factory=lambda: self.app)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_english_text_has_every_section_and_the_commands(self):
        self.seed()
        code, out, err = self.run_cli('insights', '--lang', 'en')
        self.assertEqual((code, err), (0, ''))
        for text in ('Disk Steward insights, last 30 days', 'Environment', 'Key numbers', 'Where the space is',
                     'Scan history', 'Cleanups', 'Growing', 'Shrinking', 'AI agents', 'To do',
                     'Free space', 'Not scanned', 'Junk', 'Weekly scan: not installed', 'Local snapshots: 2',
                     'Tools: codex 0.160.0, claude 2.1.299, ollama not found', 'Run: disk-steward schedule install',
                     'Run: brew cleanup --prune=all', '[Act]', '[Review]', '[Tip]', 'Codex'):
            self.assertIn(text, out)
        self.assertIn(fmt_bytes(4 * GIB), out)
        self.assertIn('Quit Codex to clean 30.00 MiB more', out)

    def test_chinese_text(self):
        self.seed()
        code, out, err = self.run_cli('insights', '--lang', 'zh-CN')
        self.assertEqual((code, err), (0, ''))
        for text in ('Disk Steward 洞察，最近 30 天', '环境', '关键数字', '空间分布', '扫描历史', '清理记录', '增长', '缩小',
                     'AI 智能体', '待办', '可用空间', '未扫描', '每周扫描：未安装', '本地快照：2 个',
                     '运行：disk-steward schedule install', '[立即处理]', '[待审阅]', '[建议]', '退出 Codex，可多清理 30.00 MiB'):
            self.assertIn(text, out)
        self.assertNotIn('Where the space is', out)

    def test_the_window_option_and_all_history(self):
        self.seed()
        self.assertIn('last 7 days', self.run_cli('insights', '--window', '7')[1])
        self.assertIn('all history', self.run_cli('insights', '--window', '0')[1])
        week = self.run_cli('insights', '--window', '7', '--json')[1]
        self.assertEqual(len(json.loads(week)['history']['scans']), 1)

    def test_json_is_the_portals_answer(self):
        self.seed()
        code, out, err = self.run_cli('insights', '--json', '--lang', 'zh-CN')
        self.assertEqual((code, err), (0, ''))
        data = json.loads(out)
        self.assertEqual(set(data), TOP_KEYS)
        self.assertEqual(data['composition'][0]['label'], '可用空间')
        self.assertEqual(data['todos'], json.loads(json.dumps(self.app.insights(30, 'zh-CN')['todos'])))

    def test_an_empty_mac_still_prints(self):
        for lang, text in (('en', 'Last scan: none yet'), ('zh-CN', '上次扫描：还没有')):
            code, out, err = self.run_cli('insights', '--lang', lang)
            self.assertEqual((code, err), (0, ''))
            self.assertIn(text, out)
        self.assertIn('Run the first scan', self.run_cli('insights', '--lang', 'en')[1])

    def test_a_negative_window_is_an_error(self):
        code, out, err = self.run_cli('insights', '--window', '-3')
        self.assertEqual((code, out), (2, ''))
        self.assertIn('window', err)

    def test_estimated_history_is_marked_in_the_table(self):
        self.seed()
        self.app.store._conn().execute('UPDATE scan_totals SET backfilled = 1 WHERE scan_id = 1')
        out = self.run_cli('insights', '--lang', 'en', '--window', '0')[1]
        self.assertIn('~', out)
        self.assertIn('approximate', out)


class SpeedTests(InsightsCase):
    def test_a_store_of_150k_items_builds_in_under_a_second(self):
        keys = {f'app.caches:~/Library/Caches/c{i:03d}': (GIB, 750) for i in range(200)}
        self.synth.scan(10, {k: (MIB, 5) for k in keys})
        self.mac.running = {'com.openai.codex'}
        self.synth.scan(0, keys)
        self.assertEqual(self.app.store.count_items(self.app.store.latest_scan()['id']), 150_000)
        self.build()                                           # tool versions and the page cache are warm now
        best = min(self._timed() for _ in range(3))
        self.assertLess(best, 1.0, f'insights took {best:.2f} s on 150k items')

    def _timed(self):
        start = time.perf_counter()
        data = self.build()
        took = time.perf_counter() - start
        self.assertEqual(data['kpis']['safe_to_clean']['items'], 150_000)
        return took


if __name__ == '__main__':
    unittest.main()
