from __future__ import annotations

import dataclasses
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from disk_steward import settings as S
from disk_steward.model import ErrorCode, StewardError
from disk_steward.settings import Settings, load, replace, save, settings_hash
from support import render_error


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / 'data'

    def assertInvalidSetting(self, fn, field=None):
        with self.assertRaises(StewardError) as cm:
            fn()
        self.assertEqual(cm.exception.code, ErrorCode.INVALID)
        self.assertEqual(cm.exception.message_key, 'error.invalid_setting')
        if field:
            self.assertEqual(cm.exception.details['field'], field)
        render_error(cm.exception)

    def test_defaults(self):
        s = Settings()
        self.assertIsNone(s.locale)
        self.assertEqual(s.domains, ('agent', 'app-library'))
        self.assertEqual(s.default_min_age_days, 14)
        self.assertEqual(s.large_file_min_bytes, 500 * 1024 * 1024)
        self.assertEqual(s.idle_file_min_bytes, 50 * 1024 * 1024)
        self.assertEqual((s.idle_days, s.installer_age_days, s.project_idle_days), (180, 30, 60))
        self.assertEqual(s.user_roots, ('~/Downloads', '~/Desktop', '~/Documents', '~/Movies'))
        self.assertFalse(s.advisor_enabled)
        self.assertIsNone(s.advisor_provider)

    def test_frozen(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            Settings().idle_days = 1

    def test_load_missing_file_gives_defaults(self):
        self.assertEqual(load(self.dir), Settings())

    def test_save_load_round_trip_and_file_mode(self):
        s = replace(Settings(), locale='zh-CN', domains=['agent', 'user-files'], idle_days=90.5,
                    user_roots=['~/Movies'], advisor_enabled=True, advisor_provider='codex')
        save(self.dir, s)
        self.assertEqual(load(self.dir), s)
        path = self.dir / 'settings.json'
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(json.loads(path.read_text())['domains'], ['agent', 'user-files'])
        self.assertEqual(sorted(os.listdir(self.dir)), ['settings.json'])

    def test_save_overwrites_atomically(self):
        save(self.dir, Settings())
        new = replace(Settings(), idle_days=7)
        with mock.patch.object(S.os, 'replace', side_effect=OSError('boom')):
            with self.assertRaises(OSError):
                save(self.dir, new)
        self.assertEqual(load(self.dir), Settings())            # old file untouched
        self.assertEqual(sorted(os.listdir(self.dir)), ['settings.json'])  # temp file cleaned up
        save(self.dir, new)
        self.assertEqual(load(self.dir).idle_days, 7)

    def test_replace_validation(self):
        base = Settings()
        for key, bad in (('locale', 'fr'), ('locale', 5), ('domains', 'agent'), ('domains', ['nope']),
                         ('domains', [1]), ('default_min_age_days', -1), ('default_min_age_days', True),
                         ('default_min_age_days', '14'), ('default_min_age_days', float('nan')),
                         ('default_min_age_days', float('inf')), ('idle_days', None),
                         ('large_file_min_bytes', 1.5), ('large_file_min_bytes', -1),
                         ('large_file_min_bytes', True), ('idle_file_min_bytes', '5'),
                         ('installer_age_days', []), ('project_idle_days', {}),
                         ('user_roots', '~/a'), ('user_roots', ['/abs']), ('user_roots', ['~/a/']),
                         ('user_roots', ['~/../x']), ('user_roots', ['relative']), ('user_roots', [None]),
                         ('advisor_enabled', 1), ('advisor_enabled', 'yes'), ('advisor_provider', ''),
                         ('advisor_provider', 3)):
            with self.subTest(key=key, bad=bad):
                self.assertInvalidSetting(lambda: replace(base, **{key: bad}), key)

    def test_replace_unknown_key(self):
        self.assertInvalidSetting(lambda: replace(Settings(), nope=1), 'nope')

    def test_direct_construction_is_validated_too(self):
        self.assertInvalidSetting(lambda: Settings(idle_days=-5), 'idle_days')

    def test_replace_coerces_and_normalises(self):
        s = replace(Settings(), domains=['agent'], user_roots=['~/a'], idle_days=30.0)
        self.assertEqual(s.domains, ('agent',))
        self.assertEqual(s.user_roots, ('~/a',))
        self.assertEqual(s.idle_days, 30)
        self.assertIsInstance(s.idle_days, int)
        self.assertEqual(replace(Settings(), idle_days=0.5).idle_days, 0.5)

    def test_replace_does_not_mutate_the_original(self):
        base = Settings()
        replace(base, idle_days=1)
        self.assertEqual(base.idle_days, 180)

    def test_valid_edge_values(self):
        s = replace(Settings(), domains=[], user_roots=['~', '$TMPDIR/x'], locale='en', default_min_age_days=0)
        self.assertEqual(s.domains, ())
        self.assertEqual(s.locale, 'en')

    def test_load_corrupt_file(self):
        self.dir.mkdir()
        for body in ('{not json', '[1, 2]', '"x"'):
            (self.dir / 'settings.json').write_text(body)
            with self.subTest(body=body):
                with self.assertRaises(StewardError) as cm:
                    load(self.dir)
                self.assertEqual(cm.exception.message_key, 'error.settings_corrupt')
                render_error(cm.exception)

    def test_load_rejects_unknown_and_invalid_fields(self):
        self.dir.mkdir()
        (self.dir / 'settings.json').write_text('{"future_option": true}')
        self.assertInvalidSetting(lambda: load(self.dir), 'future_option')
        (self.dir / 'settings.json').write_text('{"idle_days": "soon"}')
        self.assertInvalidSetting(lambda: load(self.dir), 'idle_days')

    def test_load_partial_file_fills_defaults(self):
        self.dir.mkdir()
        (self.dir / 'settings.json').write_text('{"idle_days": 10}')
        self.assertEqual(load(self.dir), replace(Settings(), idle_days=10))


class HashTests(unittest.TestCase):
    def test_stable_and_hex(self):
        h = settings_hash(Settings())
        self.assertEqual(h, settings_hash(Settings()))
        self.assertRegex(h, r'^[0-9a-f]{64}$')

    def test_classification_fields_change_the_hash(self):
        base = settings_hash(Settings())
        for patch in (dict(domains=['agent']), dict(domains=['agent', 'app-library', 'user-files']),
                      dict(large_file_min_bytes=1), dict(idle_file_min_bytes=1),
                      dict(idle_days=1), dict(installer_age_days=1), dict(project_idle_days=1),
                      dict(user_roots=['~/Downloads'])):
            with self.subTest(patch=patch):
                self.assertNotEqual(settings_hash(replace(Settings(), **patch)), base)

    def test_presentation_and_advisor_fields_do_not(self):
        base = settings_hash(Settings())
        # default_min_age_days only pre-fills the selection filter; plans carry their own thresholds.
        for patch in (dict(locale='zh-CN'), dict(locale='en'), dict(advisor_enabled=True),
                      dict(advisor_provider='claude'), dict(default_min_age_days=7)):
            with self.subTest(patch=patch):
                self.assertEqual(settings_hash(replace(Settings(), **patch)), base)

    def test_order_and_number_type_are_irrelevant(self):
        a = replace(Settings(), domains=['app-library', 'agent'], user_roots=['~/b', '~/a'])
        b = replace(Settings(), domains=['agent', 'app-library'], user_roots=['~/a', '~/b'])
        self.assertEqual(settings_hash(a), settings_hash(b))
        self.assertEqual(settings_hash(replace(Settings(), idle_days=180.0)), settings_hash(Settings()))


if __name__ == '__main__':
    unittest.main()
