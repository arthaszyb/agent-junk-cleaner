from __future__ import annotations

import dataclasses
import re
import string
import sys
import types
import unittest
from fnmatch import fnmatchcase
from pathlib import Path
from unittest import mock

from disk_steward import catalog as catalog_mod
from disk_steward import i18n
from disk_steward.catalog import (Catalog, ScanRoot, build_catalog, electron_cache_rules, is_report_only,
                                  load_catalog, protections, vscode_family_rules)
from disk_steward.classify import Classifier, is_protected_variant, protect_group_key, split_group_key
from disk_steward.detectors import REGISTRY
from disk_steward.i18n import LOCALES, catalog as i18n_catalog
from disk_steward.model import (Action, Domain, Kind, Rule, StatInfo, Tier, Unit)
from disk_steward.paths import Bases, match_pattern
from disk_steward.settings import Settings


SRC = Path(catalog_mod.__file__).resolve().parent.parent
STAT = StatInfo(dev=1, ino=1, mode=0o100644, nlink=1, uid=1, size=0, blocks=0, mtime_ns=0, birth_ns=0, flags=0)


def placeholders(text):
    return sorted(name for _, name, _, _ in string.Formatter().parse(text) if name)


def rule(**kw):
    base = dict(id='t.rule', provider='codex', domain=Domain.AGENT, tier=Tier.JUNK, kind=Kind.CACHE,
                patterns=('~/x/**',), actions=(Action.DELETE,), default_action=Action.DELETE)
    base.update(kw)
    return Rule(**base)


def build(*rules, roots=(), privacy=()):
    return build_catalog(rules, roots, privacy)


class LoadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cat = load_catalog(Settings())

    def test_shape(self):
        self.assertIsInstance(self.cat, Catalog)
        keys = [(-r.priority, r.id) for r in self.cat.rules]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(len({r.id for r in self.cat.rules}), len(self.cat.rules))
        self.assertRegex(self.cat.hash, r'^[0-9a-f]{64}$')
        self.assertEqual(self.cat.rule('codex.logs').id, 'codex.logs')
        with self.assertRaises(KeyError):
            self.cat.rule('nope')

    def test_roots_and_privacy(self):
        paths = {r.path for r in self.cat.roots}
        for p in ('~/.codex', '~/.claude', '~/.local/share/claude', '~/.cache/codex-runtimes', '~/.ollama',
                  '~/Library/Application Support/Claude', '~/Library/Caches/com.openai.codex', '~/.gemini'):
            self.assertIn(p, paths)
        self.assertEqual({r.domain for r in self.cat.roots}, set(Domain))
        for p in ('~/Library/Messages', '~/Library/Caches/com.apple.*', '~/.Trash', '~/Music/Music'):
            self.assertIn(p, self.cat.privacy_skip)

    def test_protections_outrank_everything(self):
        top = {r.id for r in self.cat.rules if r.priority >= 1000}
        self.assertEqual(top, {'prot.credentials', 'prot.databases', 'prot.git', 'prot.git-tree'})
        self.assertTrue(all(r.tier == Tier.PROTECTED for r in self.cat.rules if r.priority in (900, 1000)))

    def test_live_transcript_rules_carry_an_age_floor(self):
        for rule_id in ('claude.projects', 'codex.sessions', 'codex.archived-sessions', 'claude.file-history'):
            self.assertEqual(self.cat.rule(rule_id).min_age_floor_days, 1.0, rule_id)

    def test_agent_workspaces_and_artifact_runtime_are_protected_labelled_rules(self):
        for rule_id in ('claude.desktop-workspaces', 'codex.artifact-runtime', 'codex.plugin-exclusions'):
            r = self.cat.rule(rule_id)
            self.assertEqual((r.tier, r.priority, r.unit, r.actions), (Tier.PROTECTED, 900, Unit.FILE, ()), rule_id)
        self.assertEqual(self.cat.rule('claude.desktop-workspaces').patterns,
                         ('~/Library/Application Support/Claude/local-agent-mode-sessions/**',
                          '~/Library/Application Support/Claude/claude-code-sessions/**'))
        for gone in ('claude.desktop-sessions', 'codex.artifact-sessions'):
            with self.assertRaises(KeyError):
                self.cat.rule(gone)

    def test_state_tmp_outranks_state(self):
        self.assertGreater(self.cat.rule('codex.state-tmp').priority, self.cat.rule('codex.state').priority)

    def test_adding_a_module_is_one_line(self):
        extra = types.ModuleType('disk_steward.catalog.extra_test_module')
        extra.rules = lambda settings: (rule(id='extra.rule'),)
        extra.ROOTS = (ScanRoot('~/extra', Domain.APP_LIBRARY),)
        extra.PRIVACY_SKIP = ('~/Library/Extra',)
        with mock.patch.dict(sys.modules, {'disk_steward.catalog.extra_test_module': extra}), \
                mock.patch.object(catalog_mod, '_MODULES', catalog_mod._MODULES + ('extra_test_module',)):
            cat = load_catalog(Settings())
        self.assertIn('extra.rule', {r.id for r in cat.rules})
        self.assertIn('~/extra', {r.path for r in cat.roots})
        self.assertIn('~/Library/Extra', cat.privacy_skip)
        self.assertNotEqual(cat.hash, self.cat.hash)

    def test_settings_dependent_roots(self):
        cat = load_catalog(Settings(user_roots=('~/Downloads',)))
        user_roots = {r.path for r in cat.roots if r.domain == Domain.USER_FILES}
        self.assertEqual(user_roots, {'~/Downloads'})


class ValidationTests(unittest.TestCase):
    def assertRejected(self, text, *rules, **kw):
        with self.assertRaisesRegex(ValueError, text):
            build(*rules, **kw)

    def test_duplicate_ids(self):
        self.assertRejected('duplicate id', rule(), rule())

    def test_id_shape(self):
        for bad in ('Upper', 'has space', 'a:b', 'a#b', '', '.lead'):
            with self.subTest(id=bad):
                self.assertRejected('id must be', rule(id=bad))

    def test_default_action_must_be_an_action(self):
        self.assertRejected('default_action is not among', rule(default_action=Action.ARCHIVE))
        self.assertRejected('needs a default_action', rule(default_action=None))

    def test_report_only_rules_have_no_actions(self):
        self.assertRejected('cannot have actions', rule(hint='ollama list'))
        self.assertRejected('cannot have actions', rule(domain=Domain.DEV_SHARED))
        ok = rule(hint='ollama list', actions=(), default_action=None, tier=Tier.REVIEW)
        self.assertTrue(is_report_only(ok))
        build(ok)

    def test_junk_and_review_need_actions(self):
        for tier in (Tier.JUNK, Tier.REVIEW):
            with self.subTest(tier=tier):
                self.assertRejected('needs actions', rule(tier=tier, actions=(), default_action=None))

    def test_protected_rules_have_no_actions(self):
        self.assertRejected('cannot have actions', rule(tier=Tier.PROTECTED))
        build(rule(tier=Tier.PROTECTED, actions=(), default_action=None))

    def test_patterns(self):
        for pattern in ('/etc/x', 'x/y', '~', '~/', '$HOME/x', '~/a//b', '~/a/../b', '~/a/./b'):
            with self.subTest(pattern=pattern):
                self.assertRejected('pattern', rule(patterns=(pattern,)))
        self.assertRejected('no patterns', rule(patterns=()))
        build(rule(patterns=('~/a/**', '$TMPDIR/a', '$CACHEDIR/a/*')))

    def test_other_field_checks(self):
        self.assertRejected('group_depth', rule(group_depth=0))
        self.assertRejected('negative', rule(min_age_floor_days=-1))
        self.assertRejected('bad name glob', rule(name_include=('a/b',)))
        self.assertRejected('bundle rules', rule(unit=Unit.BUNDLE, name_include=('*.x',)))

    def test_roots_and_privacy(self):
        with self.assertRaises(Exception):
            build(rule(), roots=[ScanRoot('/abs', Domain.AGENT)])
        with self.assertRaisesRegex(ValueError, 'duplicate scan root'):
            build(rule(), roots=[ScanRoot('~/a', Domain.AGENT), ScanRoot('~/a', Domain.USER_FILES)])
        with self.assertRaisesRegex(ValueError, 'pattern'):
            build(rule(), privacy=['/Library/Mail'])


class HashTests(unittest.TestCase):
    def test_stable_and_order_independent(self):
        a, b = rule(id='a.one'), rule(id='b.two', patterns=('~/y/**',))
        self.assertEqual(build(a, b).hash, build(b, a).hash)
        self.assertEqual(load_catalog(Settings()).hash, load_catalog(Settings()).hash)

    def test_changes_when_anything_in_a_rule_changes(self):
        base = build(rule()).hash
        for change in (dict(min_age_floor_days=1), dict(patterns=('~/y/**',)), dict(tier=Tier.REVIEW),
                       dict(quit_apps=('x.y',)), dict(priority=5), dict(name_include=('*.a',)),
                       dict(detector='newest_dir'), dict(group_depth=2)):
            with self.subTest(change=change):
                self.assertNotEqual(build(rule(**change)).hash, base)

    def test_changes_with_roots_privacy_and_schema(self):
        base = build(rule()).hash
        self.assertNotEqual(build(rule(), roots=[ScanRoot('~/a', Domain.AGENT)]).hash, base)
        self.assertNotEqual(build(rule(), privacy=['~/Library/Mail']).hash, base)
        with mock.patch.object(catalog_mod, 'CATALOG_SCHEMA', 2):
            self.assertNotEqual(build(rule()).hash, base)

    def test_settings_that_shape_user_rules_change_the_hash_and_the_rest_do_not(self):
        base = load_catalog(Settings()).hash
        for change in (dict(idle_days=10), dict(installer_age_days=3), dict(large_file_min_bytes=1),
                       dict(idle_file_min_bytes=1), dict(user_roots=('~/Downloads',))):
            with self.subTest(change=change):
                self.assertNotEqual(load_catalog(Settings(**change)).hash, base)
        for same in (dict(locale='en'), dict(default_min_age_days=1), dict(project_idle_days=1),
                     dict(domains=('agent',))):
            with self.subTest(same=same):
                self.assertEqual(load_catalog(Settings(**same)).hash, base)


class HelperTests(unittest.TestCase):
    def test_electron_cache_rules(self):
        (r,) = electron_cache_rules('x.electron', 'codex', Domain.AGENT, '~/Library/Application Support/X', 'com.x.y')
        self.assertEqual((r.tier, r.kind, r.unit, r.actions, r.quit_apps, r.priority),
                         (Tier.JUNK, Kind.CACHE, Unit.FILE, (Action.DELETE,), ('com.x.y',), 300))
        self.assertEqual(len(r.patterns), 18)
        self.assertIn('~/Library/Application Support/X/GPUCache/**', r.patterns)
        self.assertIn('~/Library/Application Support/X/Partitions/*/Code Cache/**', r.patterns)
        self.assertEqual(electron_cache_rules('x', 'p', Domain.AGENT, '~/a', 'b', priority=99)[0].priority, 99)
        build(r)

    def test_vscode_family_rules(self):
        rules = vscode_family_rules('ed', 'ed', Domain.AGENT, '~/Library/Application Support/Ed', '~/.ed/extensions',
                                    'com.ed')
        by_id = {r.id: r for r in rules}
        self.assertEqual(set(by_id), {'ed.electron.cache', 'ed.vsix', 'ed.cached-data', 'ed.logs', 'ed.extensions'})
        self.assertEqual(by_id['ed.cached-data'].detector, 'newest_dir')
        self.assertEqual(by_id['ed.cached-data'].unit, Unit.BUNDLE)
        self.assertEqual(by_id['ed.extensions'].detector, 'editor_extensions')
        self.assertEqual(by_id['ed.extensions'].patterns, ('~/.ed/extensions/*',))
        self.assertEqual(by_id['ed.logs'].min_age_floor_days, 1)
        self.assertEqual(by_id['ed.logs'].kind, Kind.LOGS)
        self.assertTrue(all(r.quit_apps == ('com.ed',) for r in rules))
        build(*rules)


def sample_path(rule_: Rule, pattern: str) -> str:
    """A concrete logical path the pattern (and the rule's name filter) accepts."""
    segs = pattern.split('/')
    out = []
    for seg in segs:
        out.append('a' if seg == '**' else re.sub(r'[*?]', 'x', seg))
    if segs[-1] == '**':
        out.append('a')
        out.append(re.sub(r'[*?]', 'x', rule_.name_include[0]) if rule_.name_include else 'b')
    elif rule_.name_include and not any(fnmatchcase(out[-1].lower(), g.lower()) for g in rule_.name_include):
        out[-1] = re.sub(r'[*?]', 'x', rule_.name_include[0])
    return '/'.join(out)


def passes_names(r: Rule, name: str) -> bool:
    lowered = name.lower()
    if r.name_include and not any(fnmatchcase(lowered, g.lower()) for g in r.name_include):
        return False
    return not any(fnmatchcase(lowered, g.lower()) for g in r.name_exclude)


def brute_first_match(cat: Catalog, logical: str, unit: Unit, size: int = 0):
    name = logical.rpartition('/')[2]
    for r in cat.rules:
        if r.unit == unit and size >= r.min_size and passes_names(r, name) \
                and any(match_pattern(p, logical) for p in r.patterns):
            return r
    return None


def stat_for(r: Rule) -> StatInfo:
    """A file just big enough for the rule's size filter, so the sample of a size-gated rule can match it."""
    return dataclasses.replace(STAT, size=r.min_size)


class LintTests(unittest.TestCase):
    """Every pattern of every rule is exercised with a synthetic path."""

    @classmethod
    def setUpClass(cls):
        real = load_catalog(Settings())
        # Detectors may skip or protect a match depending on disk state; lint the patterns alone.
        stripped = [dataclasses.replace(r, detector=None) for r in real.rules]
        cls.cat = build_catalog(stripped, real.roots, real.privacy_skip)
        cls.real = real
        bases = Bases(Path('/h'), Path('/t'), Path('/c'))
        cls.classifier = Classifier(cls.cat, {}, bases, Settings(), sniff=False)

    def test_every_pattern_resolves_to_its_rule_or_a_higher_priority_one(self):
        for r in self.cat.rules:
            for pattern in r.patterns:
                sample = sample_path(r, pattern)
                with self.subTest(rule=r.id, pattern=pattern, sample=sample):
                    self.assertTrue(match_pattern(pattern, sample))
                    self.assertTrue(passes_names(r, sample.rpartition('/')[2]))
                    m = self.classifier.classify(sample, stat_for(r), is_dir=r.unit == Unit.BUNDLE)
                    won = self.cat.rule(m.rule_id)
                    self.assertGreaterEqual(won.priority, r.priority)

    def test_no_other_rule_of_the_same_priority_matches(self):
        for r in self.cat.rules:
            for pattern in r.patterns:
                sample = sample_path(r, pattern)
                name = sample.rpartition('/')[2]
                clash = [o.id for o in self.cat.rules
                         if o.id != r.id and o.priority == r.priority and o.unit == r.unit
                         and passes_names(o, name) and any(match_pattern(p, sample) for p in o.patterns)]
                with self.subTest(rule=r.id, sample=sample):
                    self.assertEqual(clash, [])

    def test_classifier_agrees_with_brute_force_matching(self):
        for r in self.cat.rules:
            for pattern in r.patterns:
                sample = sample_path(r, pattern)
                is_dir = r.unit == Unit.BUNDLE
                with self.subTest(rule=r.id, sample=sample):
                    expected = brute_first_match(self.cat, sample, r.unit, r.min_size)
                    got = self.classifier.classify(sample, stat_for(r), is_dir=is_dir)
                    self.assertEqual(got.rule_id, expected.id)

    def test_classifier_agrees_with_brute_force_on_many_paths(self):
        dirs = {sample_path(r, p).rpartition('/')[0] for r in self.cat.rules for p in r.patterns}
        dirs |= {'~', '~/.codex', '~/.claude/projects/p', '~/.claude/projects/p/memory/deep', '~/Library/Caches',
                 '~/.codex/sessions', '$TMPDIR/x/y', '$CACHEDIR/q', '~/Library/Application Support/Claude/Partitions/p1'}
        names = ['x', 'auth.json', 'a.log', 'a.jsonl', 'f.sqlite', 'rootfs.img', 'initrd.gz', 'CURRENT', 'LOG.old',
                 'config.toml', '..codex-global-state.json.tmp-1', 'settings.json', 'sessiondata.img', 'k.pem']
        checked = 0
        for d in sorted(dirs):
            for n in names:
                logical = f'{d}/{n}'
                expected = brute_first_match(self.cat, logical, Unit.FILE)
                got = self.classifier.classify(logical, STAT)
                self.assertEqual(got.rule_id, expected.id if expected else 'unknown', logical)
                checked += 1
        self.assertGreater(checked, 1500)

    def test_no_bundle_rule_claims_a_folder_inside_a_higher_priority_protected_area(self):
        """A bundle is walked and stored as one item; if it sat inside an area a protection rule covers, the
        protected files would end up inside a junk or review item."""
        protected = [r for r in self.cat.rules if r.tier == Tier.PROTECTED and r.unit == Unit.FILE
                     and not r.name_include and not r.name_exclude]   # whole-area rules, not name filters
        bundles = [r for r in self.cat.rules if r.unit == Unit.BUNDLE]
        self.assertTrue(protected and bundles)
        checked = 0
        for area in protected:
            for pattern in area.patterns:
                segs = pattern.split('/')
                if segs[-1] != '**':
                    continue   # a pattern that names files directly has no folders inside it
                base = [('a' if x == '**' else re.sub(r'[*?]', 'x', x)) for x in segs[:-1]]
                for dir_path in ('/'.join(base), '/'.join(base + ['a']), '/'.join(base + ['a', 'b'])):
                    for bundle in bundles:
                        if bundle.priority >= area.priority:
                            continue
                        with self.subTest(area=area.id, bundle=bundle.id, folder=dir_path):
                            checked += 1
                            self.assertFalse(any(match_pattern(p, dir_path) for p in bundle.patterns))
                            claimed = self.classifier.classify_dir(dir_path, STAT)
                            self.assertTrue(claimed is None or self.cat.rule(claimed.rule_id).priority >= area.priority)
        self.assertGreater(checked, 50)

    def test_every_detector_named_by_a_rule_is_registered(self):
        names = {r.detector for r in self.real.rules if r.detector}
        self.assertTrue(names)
        self.assertLessEqual(names, set(REGISTRY))
        self.assertNotIn('codex_bloat', REGISTRY)


class I18nCoverageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cat = load_catalog(Settings())

    def test_label_and_effect_for_every_rule_in_both_locales(self):
        for loc in LOCALES:
            texts = i18n_catalog(loc)
            for r in self.cat.rules:
                for key in (r.label_key, r.effect_key):
                    with self.subTest(locale=loc, key=key):
                        self.assertTrue(texts.get(key, '').strip())
            for key in ('rule.unknown.label', 'rule.unknown.effect'):
                self.assertIn(key, texts)

    def test_label_placeholders_match_the_group_kind(self):
        en = i18n_catalog('en')
        for r in self.cat.rules:
            with self.subTest(rule=r.id):
                expected = ['path'] if r.group_depth is not None else []
                self.assertEqual(placeholders(en[r.label_key]), expected)
                self.assertEqual(placeholders(en[r.effect_key]), [])
        self.assertEqual(placeholders(en['rule.unknown.label']), ['path'])

    def test_the_duplicates_group_has_its_own_texts(self):
        from disk_steward.detectors.codex_dupes import GROUP_KEY
        for loc in LOCALES:
            texts = i18n_catalog(loc)
            self.assertEqual(placeholders(texts[f'group.{GROUP_KEY}.label']), [])
            self.assertTrue(texts[f'group.{GROUP_KEY}.effect'].strip())
        self.assertEqual(i18n_catalog('en')[f'group.{GROUP_KEY}.label'], 'Codex sessions — legacy-ID copies')

    def test_every_provider_has_a_name(self):
        for loc in LOCALES:
            texts = i18n_catalog(loc)
            for provider in {r.provider for r in self.cat.rules}:
                self.assertIn(f'provider.{provider}', texts)

    def test_every_evidence_key_used_in_source_exists(self):
        used = set()
        for py in list((SRC / 'detectors').glob('*.py')) + [SRC / 'scanner.py', SRC / 'classify.py']:
            used.update(re.findall(r"'(evidence\.[a-z_]+)'", py.read_text(encoding='utf-8')))
        self.assertGreaterEqual(len(used), 18)
        for loc in LOCALES:
            self.assertLessEqual(used, set(i18n_catalog(loc)), loc)

    def test_every_scan_error_code_and_taint_reason_has_text(self):
        scanner_src = (SRC / 'scanner.py').read_text(encoding='utf-8')
        codes = set(re.findall(r"self\._error\([^,]+, '([a-z_]+)'\)", scanner_src))
        self.assertGreaterEqual(len(codes), 6)
        from disk_steward.fsutil import TAINT_ORDER
        for loc in LOCALES:
            texts = i18n_catalog(loc)
            for code in codes:
                self.assertIn(f'scan.error.{code}', texts)
            for reason in TAINT_ORDER:
                self.assertIn(f'taint.{reason}', texts)

    def test_evidence_args_fill_their_templates(self):
        samples = {
            'evidence.tainted': {'reason': 'git'},
            'evidence.current_version': {'version': '1', 'current': '1', 'newest': '2'},
            'evidence.not_current_version': {'current': '1', 'newest': '2'},
            'evidence.version_unverifiable': {'reason': 'missing'},
            'evidence.engine_newest': {'version': '1'}, 'evidence.engine_pinned': {'version': '1'},
            'evidence.engine_superseded': {'newest': '2'},
            'evidence.plugin_manifest_unreadable': {'reason': 'invalid'},
            'evidence.plugin_installed': {}, 'evidence.plugin_not_in_manifest': {},
            'evidence.codex_legacy_copy': {'partner': 'p'}, 'evidence.codex_both_indexed': {},
            'evidence.codex_duplicate_kept': {'partner': 'p'}, 'evidence.codex_duplicate_unresolved': {'partner': 'p'},
            'evidence.newest_dir': {'name': 'n'}, 'evidence.extension_obsolete': {'name': 'n'},
            'evidence.appeared_during_scan': {}, 'evidence.sqlite_header': {},
            'evidence.header_unreadable': {'reason': 'EACCES'},
        }
        for loc in LOCALES:
            for key, args in samples.items():
                self.assertNotEqual(i18n.t(key, loc, **args), key)

    def test_split_group_key(self):
        self.assertEqual(split_group_key('codex.logs'), ('codex.logs', ''))
        self.assertEqual(split_group_key('claude.projects#protected:~/.claude/projects/x'),
                         ('claude.projects', '~/.claude/projects/x'))
        self.assertEqual(split_group_key('claude.cli-versions#protected'), ('claude.cli-versions', ''))
        self.assertEqual(split_group_key('codex.sessions:duplicates'), ('codex.sessions', 'duplicates'))
        self.assertEqual(split_group_key('unknown:~/.codex/skills'), ('unknown', '~/.codex/skills'))
        self.assertEqual(split_group_key('prot.databases:app-library'), ('prot.databases', 'app-library'))
        # a '#' inside the path suffix belongs to the path
        self.assertEqual(split_group_key('claude.projects:~/.claude/projects/a#b'),
                         ('claude.projects', '~/.claude/projects/a#b'))

    def test_protected_variant_keys(self):
        self.assertEqual(protect_group_key('claude.cli-versions'), 'claude.cli-versions#protected')
        self.assertEqual(protect_group_key('claude.projects:~/.claude/projects/x'),
                         'claude.projects#protected:~/.claude/projects/x')
        self.assertEqual(protect_group_key('claude.projects#protected:~/x'), 'claude.projects#protected:~/x')
        self.assertTrue(is_protected_variant('claude.projects#protected:~/x'))
        self.assertTrue(is_protected_variant('claude.cli-versions#protected'))
        self.assertFalse(is_protected_variant('claude.projects:~/.claude/a#protected'))
        self.assertFalse(is_protected_variant('prot.databases:agent'))


class ProtectionsTests(unittest.TestCase):
    def test_privacy_list_is_logical_and_unique(self):
        self.assertEqual(len(set(protections.PRIVACY_SKIP)), len(protections.PRIVACY_SKIP))
        self.assertTrue(all(p.startswith('~/') for p in protections.PRIVACY_SKIP))

    def test_extended_name_lists(self):
        import fnmatch
        names = lambda globs, n: any(fnmatch.fnmatchcase(n.lower(), g.lower()) for g in globs)
        for n in ('.env', '.env.local', '.git-credentials', '.npmrc', '.pypirc', '.pgpass', 'my-creds.txt',
                  'client_secret.json', 'deploy-key', '.deploy-key', 'cert.pfx', 'x.jks', 'vault.kdbx'):
            self.assertTrue(names(protections.CREDENTIAL_NAMES, n), n)
        for n in ('state.vscdb', 'a.duckdb', 'a.mdb', 'Cookies', 'Login Data', 'History', 'Web Data', 'Favicons',
                  'Top Sites'):
            self.assertTrue(names(protections.DATABASE_NAMES, n), n)

    def test_name_lists_feed_the_protection_rules(self):
        rules = {r.id: r for r in protections.rules(Settings())}
        self.assertEqual(rules['prot.credentials'].name_include, protections.CREDENTIAL_NAMES)
        self.assertEqual(rules['prot.databases'].name_include, protections.DATABASE_NAMES)
        self.assertEqual(rules['prot.git'].unit, Unit.BUNDLE)


if __name__ == '__main__':
    unittest.main()
