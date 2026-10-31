from __future__ import annotations

import json
import os
import re
import string
import subprocess
import sys
import tempfile
import unittest
import zipapp
from pathlib import Path
from unittest import mock

from disk_steward import i18n
from disk_steward.i18n import LOCALES, NAMESPACES, catalog, detect_locale, t
from disk_steward.model import (Action, AgeBasis, DecisionState, Domain, EntryState, ErrorCode, Kind, PlanState,
                                StewardError, Tier, Unit)

SRC = Path(i18n.__file__).resolve().parent.parent
PREFIXES = {Tier: 'tier', Domain: 'domain', Kind: 'kind', Unit: 'unit', Action: 'action', AgeBasis: 'age_basis',
            DecisionState: 'decision', EntryState: 'entry', PlanState: 'plan', ErrorCode: 'error'}
REASONS = ('protected', 'tainted', 'domain_off', 'no_gain', 'excluded', 'unselected', 'unsupported_action',
           'report_only', 'too_recent_floor', 'too_recent', 'selected_group', 'selected_explicit')


def placeholders(text):
    return sorted(name for _, name, _, _ in string.Formatter().parse(text) if name)


def tearDownModule():
    # t() records every key it could not resolve; nothing in this run may have hit one.
    if i18n.MISSING:
        raise AssertionError(f'untranslated keys: {sorted(i18n.MISSING)}')


class CatalogTests(unittest.TestCase):
    def test_namespace_files_are_known_and_match_across_locales(self):
        stems = {loc: {p.stem for p in (SRC / 'i18n' / loc).glob('*.json')} for loc in LOCALES}
        self.assertEqual(stems['en'], stems['zh-CN'])
        self.assertLessEqual(stems['en'], set(NAMESPACES))
        self.assertIn('core', stems['en'])

    def test_key_parity_in_every_namespace(self):
        for ns in NAMESPACES:
            en = i18n._read_namespace('en', ns)
            zh = i18n._read_namespace('zh-CN', ns)
            with self.subTest(namespace=ns):
                self.assertEqual(en is None, zh is None)
                if en is None:
                    continue
                self.assertEqual(sorted(en), sorted(zh))
                for key in en:
                    self.assertEqual(placeholders(en[key]), placeholders(zh[key]), key)
                    self.assertIsInstance(en[key], str)
                    self.assertTrue(en[key].strip() and zh[key].strip(), key)

    def test_every_enum_value_has_a_key(self):
        for loc in LOCALES:
            cat = catalog(loc)
            for enum_cls, prefix in PREFIXES.items():
                for member in enum_cls:
                    with self.subTest(locale=loc, key=f'{prefix}.{member.value}'):
                        self.assertIn(f'{prefix}.{member.value}', cat)

    def test_decision_reason_keys(self):
        for loc in LOCALES:
            for r in REASONS:
                self.assertIn(f'reason.{r}', catalog(loc))

    def test_every_error_message_key_used_in_source_exists(self):
        used = set()
        for py in list(SRC.glob('*.py')) + list(SRC.glob('cli/*.py')):
            used.update(re.findall(r"'(error\.[A-Za-z_]+)'", py.read_text(encoding='utf-8')))
        self.assertTrue(used)
        for loc in LOCALES:
            self.assertLessEqual(used, set(catalog(loc)), loc)

    def test_catalog_is_a_copy(self):
        catalog('en')['tier.junk'] = 'changed'
        self.assertEqual(t('tier.junk'), 'Junk')

    def test_unknown_locale_raises(self):
        with self.assertRaises(StewardError) as cm:
            t('tier.junk', 'fr')
        self.assertEqual(cm.exception.code, ErrorCode.INVALID)
        self.assertEqual(cm.exception.message_key, 'error.invalid_locale')


class TranslateTests(unittest.TestCase):
    def test_lookup_and_locale(self):
        self.assertEqual(t('tier.junk'), 'Junk')
        self.assertEqual(t('tier.junk', 'zh-CN'), '垃圾')

    def test_format_args(self):
        self.assertEqual(t('reason.too_recent', 'en', age=3.25, threshold=14),
                         'Changed 3.25 days ago, which is newer than the 14-day threshold.')
        self.assertIn('0.5', t('reason.too_recent_floor', 'zh-CN', age=2, floor=0.5))

    def test_missing_args_fail_loudly(self):
        with self.assertRaises(KeyError):
            t('reason.too_recent', 'en', age=1)

    def test_missing_key_returns_key_and_is_recorded(self):
        key = 'no.such.key.for.test'
        try:
            self.assertEqual(t(key), key)
            self.assertIn(('en', key), i18n.MISSING)
            self.assertEqual(t(key, 'zh-CN'), key)
            self.assertIn(('zh-CN', key), i18n.MISSING)
        finally:
            i18n.MISSING.discard(('en', key))
            i18n.MISSING.discard(('zh-CN', key))


class DetectLocaleTests(unittest.TestCase):
    def test_env_lang(self):
        for env, expected in (('zh_CN.UTF-8', 'zh-CN'), ('zh_TW.UTF-8', 'zh-CN'), ('zh-Hans', 'zh-CN'),
                              ('zh', 'zh-CN'), ('ZH_cn', 'zh-CN'), ('en_US.UTF-8', 'en'), ('C', 'en'),
                              ('POSIX', 'en'), ('fr_FR.UTF-8', 'en'), ('', 'en')):
            with self.subTest(env=env):
                self.assertEqual(detect_locale(env), expected)

    def test_accept_language(self):
        for header, expected in (('zh-CN,zh;q=0.9,en;q=0.8', 'zh-CN'), ('en-US,en;q=0.9,zh-CN;q=0.8', 'en'),
                                 ('zh-TW', 'zh-CN'), ('fr,zh;q=0.5', 'zh-CN'), ('en;q=0.5,zh;q=0.9', 'zh-CN'),
                                 ('zh;q=0,en', 'en'), ('fr', 'en'), ('*', 'en')):
            with self.subTest(header=header):
                self.assertEqual(detect_locale(None, header), expected)

    def test_accept_language_beats_env_lang(self):
        self.assertEqual(detect_locale('zh_CN.UTF-8', 'en-US'), 'en')
        self.assertEqual(detect_locale('en_US.UTF-8', 'zh-CN'), 'zh-CN')

    def test_unsupported_accept_language_falls_back_to_env_lang(self):
        self.assertEqual(detect_locale('zh_CN.UTF-8', 'fr'), 'zh-CN')

    def test_process_environment_when_nothing_given(self):
        with mock.patch.dict(os.environ, {'LC_ALL': '', 'LC_MESSAGES': '', 'LANG': 'zh_CN.UTF-8'}):
            self.assertEqual(detect_locale(), 'zh-CN')
        with mock.patch.dict(os.environ, {'LC_ALL': 'en_US.UTF-8', 'LANG': 'zh_CN.UTF-8'}):
            self.assertEqual(detect_locale(), 'en')
        with mock.patch.dict(os.environ, {'LC_ALL': '', 'LC_MESSAGES': '', 'LANG': ''}):
            self.assertEqual(detect_locale(), 'en')


class ZipappTests(unittest.TestCase):
    def test_catalog_loads_from_a_zipapp(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / 'app'
            app.mkdir()
            import shutil
            shutil.copytree(SRC, app / 'disk_steward', ignore=shutil.ignore_patterns('__pycache__'))
            (app / '__main__.py').write_text(
                'import json\n'
                'from disk_steward import i18n\n'
                "print(json.dumps([i18n.t('tier.junk', 'zh-CN'), len(i18n.catalog('en')), sorted(i18n.MISSING)]))\n")
            target = Path(tmp) / 'app.pyz'
            zipapp.create_archive(app, target)
            out = subprocess.run([sys.executable, str(target)], capture_output=True, text=True, check=True,
                                 env={'PATH': os.environ.get('PATH', '')}).stdout
        junk, count, missing = json.loads(out)
        self.assertEqual(junk, '垃圾')
        self.assertEqual(count, len(catalog('en')))
        self.assertEqual(missing, [])


if __name__ == '__main__':
    unittest.main()
