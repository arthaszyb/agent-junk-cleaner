"""The portal's static files: what they may contain, which texts they use, and that they ship in a wheel and a zipapp."""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import string
import subprocess
import sys
import tempfile
import unittest
import zipapp
from pathlib import Path

from disk_steward import i18n
from disk_steward.i18n import LOCALES, catalog
from disk_steward.portal.server import CONTENT_TYPES, read_static

SRC = Path(i18n.__file__).resolve().parent.parent
STATIC = SRC / 'portal' / 'static'
FILES = sorted(p for p in STATIC.rglob('*') if p.is_file() and p.suffix in CONTENT_TYPES)
MODULES = [p for p in FILES if p.suffix == '.mjs']
SCRIPTS = MODULES + [p for p in FILES if p.suffix == '.js']   # theme-init.js is a classic script, not a module
STYLES = STATIC / 'styles.css'
ROOT = SRC.parent.parent
SVG_NAMESPACE = 'http://www.w3.org/2000/svg'   # an identifier the browser needs, not a resource it fetches


def strip_js_comments(text):
    """Source without // and /* */ comments (string contents stay; good enough to look for forbidden calls)."""
    out, i, quote = [], 0, None
    while i < len(text):
        c = text[i]
        if quote:
            out.append(c)
            if c == '\\':
                out.append(text[i + 1])
                i += 1
            elif c == quote:
                quote = None
        elif c in '\'"`':
            quote = c
            out.append(c)
        elif text.startswith('//', i):
            while i < len(text) and text[i] != '\n':
                i += 1
            continue
        elif text.startswith('/*', i):
            i = text.index('*/', i) + 2
            continue
        else:
            out.append(c)
        i += 1
    return ''.join(out)


def call_args(text, start):
    """The top-level arguments of the call whose '(' is at text[start], as source strings."""
    depth, args, current, i, quote = 0, [], [], start, None
    while i < len(text):
        c = text[i]
        if quote:
            current.append(c)
            if c == '\\':
                current.append(text[i + 1])
                i += 1
            elif c == quote:
                quote = None
        elif c in '\'"`':
            quote = c
            current.append(c)
        elif c in '([{':
            depth += 1
            if depth > 1:
                current.append(c)
        elif c in ')]}':
            depth -= 1
            if depth == 0:
                args.append(''.join(current).strip())
                return args
            current.append(c)
        elif c == ',' and depth == 1:
            args.append(''.join(current).strip())
            current = []
        else:
            current.append(c)
        i += 1
    raise AssertionError('unbalanced call')


def object_keys(literal):
    """Keys of a simple `{a: 1, b, c: f(x)}` literal; None when it uses spread or computed keys."""
    inner = literal.strip()[1:-1]
    parts, depth, current, quote = [], 0, [], None
    for c in inner:
        if quote:
            current.append(c)
            quote = None if c == quote else quote
        elif c in '\'"`':
            quote = c
            current.append(c)
        elif c in '([{':
            depth += 1
            current.append(c)
        elif c in ')]}':
            depth -= 1
            current.append(c)
        elif c == ',' and depth == 0:
            parts.append(''.join(current))
            current = []
        else:
            current.append(c)
    parts.append(''.join(current))
    keys = set()
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if part.startswith('...') or part.startswith('['):
            return None
        m = re.match(r'^([A-Za-z_]\w*)\s*(:|$)', part)
        if not m:
            return None
        keys.add(m.group(1))
    return keys


def placeholders(text):
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def strip_css_comments(text):
    return re.sub(r'/\*.*?\*/', '', text, flags=re.S)


def css_block(text, header):
    """The text between the braces of the first block that starts with `header`."""
    start = text.index(header)
    i = text.index('{', start)
    depth, j = 0, i
    while True:
        if text[j] == '{':
            depth += 1
        elif text[j] == '}':
            depth -= 1
            if depth == 0:
                return text[i + 1:j]
        j += 1


def css_tokens(block):
    """{--name: value} of the custom properties declared directly in a block."""
    return dict(re.findall(r'(--[\w-]+)\s*:\s*([^;]+);', block))


class SourceRuleTests(unittest.TestCase):
    def test_there_are_files(self):
        names = {p.relative_to(STATIC).as_posix() for p in FILES}
        self.assertLessEqual({'index.html', 'locked.html', 'styles.css', 'app.mjs', 'api.mjs', 'i18n.mjs', 'state.mjs',
                              'format.mjs', 'shell.mjs', 'icons.mjs', 'svg.mjs', 'scale.mjs', 'charts.mjs', 'tables.mjs', 'favicon.svg',
                              'theme-init.js', 'views/overview.mjs', 'views/insights.mjs', 'views/native.mjs', 'views/browse.mjs', 'views/plan.mjs',
                              'views/run.mjs', 'views/archives.mjs', 'views/advisor.mjs', 'views/settings.mjs'}, names)

    def test_no_inline_script_style_or_handler_and_no_external_resource(self):
        for page in STATIC.glob('*.html'):
            text = page.read_text(encoding='utf-8')
            with self.subTest(page=page.name):
                for script in re.findall(r'<script\b([^>]*)>', text):
                    self.assertIn('src="/static/', script)
                self.assertNotRegex(text, r'<script[^>]*>\s*[^<\s]')   # no script body
                self.assertNotRegex(text, r'<style\b')
                self.assertNotRegex(text, r'\sstyle\s*=')
                self.assertNotRegex(text, r'\son[a-z]+\s*=')
                self.assertNotRegex(text, r'(src|href)\s*=\s*"(https?:)?//')
        for path in FILES:
            text = path.read_text(encoding='utf-8').replace(SVG_NAMESPACE, '')
            with self.subTest(file=path.name):
                self.assertNotRegex(text, r'https?://[^\s\'"`)]*\.', 'no external host')
                self.assertNotIn('@import', text)
                self.assertNotIn('url(http', text)

    def test_every_page_links_the_favicon_and_the_theme_script_before_the_modules(self):
        for page in STATIC.glob('*.html'):
            text = page.read_text(encoding='utf-8')
            with self.subTest(page=page.name):
                self.assertIn('<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">', text)
                self.assertIn('<script src="/static/theme-init.js"></script>', text)
                self.assertRegex(text, r'<title>[^<]+</title>')

    def test_dom_is_built_without_html_strings(self):
        banned = ('innerHTML', 'outerHTML', 'insertAdjacentHTML', 'document.write', 'eval(', 'new Function', 'setAttribute(\'on',
                  "setAttribute('style'", '.srcdoc', 'createContextualFragment', 'DOMParser', 'setTimeout(\'', 'setInterval(\'')
        for path in SCRIPTS:
            code = strip_js_comments(path.read_text(encoding='utf-8'))
            with self.subTest(file=path.name):
                for word in banned:
                    self.assertNotIn(word, code)
                self.assertNotRegex(code, r'\.on[a-z]+\s*=[^=]')   # element.onclick = ...
                self.assertNotRegex(code, r"setAttribute\(\s*\"style\"")   # element.style.x = ... (CSSOM) is fine, the attribute is not

    def test_relative_imports_exist_and_every_module_is_reachable_from_the_app(self):
        seen, todo = set(), [STATIC / 'app.mjs']
        while todo:
            path = todo.pop()
            if path in seen:
                continue
            seen.add(path)
            for rel in re.findall(r"from '(\.{1,2}/[^']+)'", path.read_text(encoding='utf-8')):
                target = (path.parent / rel).resolve()
                self.assertTrue(target.is_file(), f'{path.name} imports {rel}')
                todo.append(target)
        self.assertEqual({p.resolve() for p in MODULES}, {p.resolve() for p in seen})

    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_every_module_parses(self):
        for path in MODULES:
            with self.subTest(file=path.name):
                r = subprocess.run(['node', '--check', str(path)], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)


class StyleTests(unittest.TestCase):
    """The design system: tokens in both themes, the fallbacks for people who ask for less, no hue in the chrome."""

    def setUp(self):
        self.css = strip_css_comments(STYLES.read_text(encoding='utf-8'))

    def dark_scopes(self):
        media = css_block(self.css, '@media (prefers-color-scheme: dark)')
        return (css_tokens(css_block(media, ':root:not([data-theme="light"])')),
                css_tokens(css_block(self.css, ':root[data-theme="dark"]')))

    def test_both_dark_scopes_exist_and_define_the_same_tokens(self):
        by_media, by_toggle = self.dark_scopes()
        self.assertGreater(len(by_media), 40)
        self.assertEqual(by_media, by_toggle)

    def test_dark_overrides_only_what_light_defines(self):
        light = css_tokens(css_block(self.css, ':root {'))
        dark, _ = self.dark_scopes()
        self.assertEqual(sorted(set(dark) - set(light)), [])
        for name in ('--plane', '--glass', '--text', '--text-2', '--text-3', '--ink', '--c-junk', '--c-outside', '--chart-surface'):
            self.assertIn(name, dark)
            self.assertNotEqual(light[name], dark[name], name)

    def test_blur_is_prefixed_and_every_fallback_block_exists(self):
        self.assertIn('-webkit-backdrop-filter: var(--backdrop)', self.css)
        self.assertIn('backdrop-filter: var(--backdrop)', self.css)
        self.assertRegex(self.css, r'--backdrop:\s*blur\(28px\) saturate\(120%\)')
        self.assertIn('@media (prefers-reduced-motion: no-preference)', self.css)
        reduced = css_block(self.css, '@media (prefers-reduced-transparency: reduce)')
        self.assertIn('--backdrop: none', reduced)
        self.assertIn('--glass-fill: var(--glass-solid)', reduced)
        forced = css_block(self.css, '@media (forced-colors: active)')
        self.assertIn('backdrop-filter: none', forced)
        self.assertIn('Canvas', forced)

    def test_the_chrome_has_no_hue_and_the_data_colours_are_the_entities(self):
        """Greys only, except the `--c-*` tokens (the data, by entity) and the three status tokens (a dot beside a word)."""
        exempt = {'--good', '--warning', '--critical'}
        for decl in re.findall(r'(--[\w-]+|[a-z-]+)\s*:\s*([^;{}]+);', self.css):
            name, value = decl
            if name in exempt or name.startswith('--c-'):
                continue
            for hexcode in re.findall(r'#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b', value):
                if len(hexcode) == 3:
                    hexcode = ''.join(c * 2 for c in hexcode)
                r, g, b = (int(hexcode[i:i + 2], 16) for i in (0, 2, 4))
                with self.subTest(name=name, color='#' + hexcode):
                    self.assertLessEqual(max(r, g, b) - min(r, g, b), 12)
            for rgba in re.findall(r'rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)', value):
                r, g, b = (int(x) for x in rgba)
                with self.subTest(name=name, color=rgba):
                    self.assertLessEqual(max(r, g, b) - min(r, g, b), 12)
        self.assertNotIn('conic-gradient', self.css)
        light = css_tokens(css_block(self.css, ':root {'))
        dark, _ = self.dark_scopes()

        def rgb(value):
            h = value.strip().lstrip('#')
            return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))

        for scope in (light, dark):
            r, g, b = rgb(scope['--c-junk'])
            self.assertTrue(r > g + 60 and r > b + 60, 'junk is red')
            r, g, b = rgb(scope['--c-review'])
            self.assertTrue(r > b + 80 and g > b + 60 and r >= g, 'needs-a-decision is amber')
            r, g, b = rgb(scope['--c-report_only'])
            self.assertTrue(b > r + 60 and b > g, 'other tools are blue')
            r, g, b = rgb(scope['--c-free'])
            self.assertTrue(g > r + 60 and g > b + 20, 'free space is green')
            for key in ('--c-protected', '--c-outside'):
                r, g, b = rgb(scope[key])
                self.assertLessEqual(max(r, g, b) - min(r, g, b), 24, f'{key} is a neutral')
            self.assertEqual(scope['--c-up'], scope['--c-junk'], 'growth is red')
            self.assertEqual(scope['--c-freed'], scope['--c-free'], 'what a cleanup frees is green')
            self.assertEqual(scope['--c-down'], scope['--c-report_only'], 'shrinking is blue, never green: red and green fail colour-blind readers')

    def test_text_tokens_are_the_ones_the_design_names(self):
        by_media, _ = self.dark_scopes()
        self.assertEqual(by_media['--text'], '#f5f5f5')
        self.assertIn('rgba(255, 255, 255,', by_media['--text-2'])
        light = css_tokens(css_block(self.css, ':root {'))
        self.assertEqual(light['--text'], '#0b0b0c')
        self.assertEqual(light['--plane'], '#eceef0')
        self.assertEqual(css_tokens(css_block(self.css, ':root[data-theme="dark"]'))['--plane'], '#0a0a0b')

    def test_radii_follow_the_design(self):
        light = css_tokens(css_block(self.css, ':root {'))
        self.assertEqual((light['--r-panel'], light['--r-card'], light['--r-pill']), ('24px', '16px', '999px'))

    def test_a_command_pill_wraps_instead_of_cutting_the_command(self):
        pill = css_block(self.css, '.cmd-pill code')
        self.assertIn('overflow-wrap: anywhere', pill)
        self.assertIn('white-space: normal', pill)
        self.assertNotIn('ellipsis', pill)

    def test_every_class_the_views_add_to_a_button_or_pill_is_styled(self):
        for cls in ('.btn.primary.solid', '.btn.danger-fill', '.seg-ctl', 'input.switch', '.round-btn', '.badge-tier', '.chip', '.glass'):
            self.assertIn(cls, self.css)


class TextTests(unittest.TestCase):
    def uses(self):
        """(file, key, args or None) of every static t('key', {...}) and plural('key', n, {...}) call."""
        found = []
        for path in MODULES:
            code = strip_js_comments(path.read_text(encoding='utf-8'))
            for m in re.finditer(r"\b(t|plural|pluralCount)\(\s*'([^']+)'", code):
                fn = 'plural' if m.group(1).startswith('plural') else 't'
                args = call_args(code, m.end(0) - len(m.group(0)) + len(m.group(1)))
                keys = None
                literal = args[1] if fn == 't' and len(args) > 1 else (args[2] if len(args) > 2 else None)
                if literal is not None and literal.startswith('{'):
                    keys = object_keys(literal)
                    if keys is not None and fn == 'plural':
                        keys = keys | {'count'}
                elif literal is None:
                    keys = {'count'} if fn == 'plural' else set()
                found.append((path.name, fn, m.group(2), keys))
        return found

    def test_every_key_the_pages_use_exists_in_both_languages(self):
        uses = self.uses()
        self.assertGreater(len(uses), 150)
        for lang in LOCALES:
            cat = catalog(lang)
            for name, fn, key, _ in uses:
                with self.subTest(lang=lang, file=name, key=key):
                    if fn == 'plural':
                        self.assertIn(f'{key}_one', cat)
                        self.assertIn(f'{key}_other', cat)
                    else:
                        self.assertIn(key, cat)

    def test_the_values_passed_are_the_placeholders_of_the_text(self):
        checked = 0
        for name, fn, key, keys in self.uses():
            if keys is None:
                continue
            template = catalog('en')[f'{key}_other' if fn == 'plural' else key]
            with self.subTest(file=name, key=key):
                self.assertEqual(keys, placeholders(template))
            checked += 1
        self.assertGreater(checked, 150)

    def test_dynamic_keys_resolve(self):
        cat = catalog('en')
        for name in ('overview', 'insights', 'browse', 'plan', 'run', 'advisor', 'settings'):
            self.assertIn(f'portal.nav.{name}', cat)
        for tier in ('junk', 'review', 'protected', 'report_only'):
            self.assertIn(f'portal.tier.{tier}', cat)
        self.assertEqual(cat['portal.tier.report_only'], 'Managed by its tool')
        for mode in ('system', 'light', 'dark'):
            self.assertIn(f'portal.shell.theme_{mode}', cat)
        for severity in ('act', 'warn', 'review', 'tip'):
            self.assertIn(f'insights.severity.{severity}', cat)
        for action in ('delete', 'trash', 'archive'):
            for number in ('one', 'other'):
                self.assertIn(f'portal.confirm.{action}_{number}', cat)
        for status in ('idle', 'running', 'paused', 'done', 'error'):
            self.assertIn(f'portal.run.status.{status}', cat)
        for verdict in ('likely_safe', 'review', 'keep'):
            self.assertIn(f'portal.advisor.verdict.{verdict}', cat)
        for provider in ('codex', 'claude'):
            self.assertIn(f'portal.advisor.ask_{provider}', cat)
        for kind in ('include_changed', 'include_missing'):
            for number in ('one', 'other'):
                self.assertIn(f'portal.notes.{kind}_{number}', cat)
        for code in ('skipped_privacy', 'permission_denied', 'vanished', 'undecodable_name', 'other_volume', 'root_symlink',
                     'not_a_directory'):
            self.assertIn(f'cli.scan_error.{code}', cat)
            self.assertIn(f'scan.error.{code}', cat)
        for state in ('stored', 'restored', 'purged', 'discarded'):
            self.assertIn(f'cli.archive_state.{state}', cat)
        for reason in ('credentials', 'databases', 'config', 'user_content', 'current_version', 'tainted', 'unknown', 'other'):
            self.assertIn(f'cli.reason.{reason}', cat)

    def test_the_server_side_texts_exist(self):
        keys = set()
        for py in (SRC / 'portal').glob('*.py'):
            keys |= set(re.findall(r"'(portal\.(?:error|cli)\.[a-z_]+)'", py.read_text(encoding='utf-8')))
        keys |= set(re.findall(r"T\('(portal\.[a-z_.]+)'", (SRC / 'cli' / 'portal_cmd.py').read_text(encoding='utf-8')))
        self.assertGreater(len(keys), 15)
        keys |= {f'portal.preflight.{k}_{n}' for k in ('quit_app', 'in_use') for n in ('one', 'other')}
        for lang in LOCALES:
            self.assertLessEqual(keys, set(catalog(lang)), lang)

    def test_one_snapshot_is_said_in_the_singular(self):
        """The server's `warning.snapshots` reads "{count} local snapshot(s) exist"; the portal words it properly."""
        en, zh = catalog('en'), catalog('zh-CN')
        self.assertIn('snapshot(s)', en['warning.snapshots'])
        one, other = en['portal.warning.snapshots_one'], en['portal.warning.snapshots_other']
        self.assertNotIn('(s)', one + other)
        self.assertIn('snapshot exists', one)
        self.assertIn('snapshots exist', other)
        self.assertEqual(placeholders(one), {'count'})
        self.assertEqual(placeholders(other), {'count'})
        self.assertEqual(zh['portal.warning.snapshots_one'], zh['warning.snapshots'])   # Chinese keeps the text it had
        self.assertEqual(zh['portal.warning.snapshots_other'], zh['warning.snapshots'])
        ui = (STATIC / 'ui.mjs').read_text(encoding='utf-8')
        self.assertIn("pluralCount('portal.warning.snapshots', w.count)", ui)
        self.assertNotIn("t('warning.snapshots'", (STATIC / 'views' / 'overview.mjs').read_text(encoding='utf-8'))

    def test_the_compacted_note_is_a_core_text_in_both_languages(self):
        for lang in LOCALES:
            self.assertIn('plan.compacted_note', catalog(lang))
        self.assertIn("t('plan.compacted_note')", (STATIC / 'views' / 'plan.mjs').read_text(encoding='utf-8'))

    def test_portal_keys_all_live_in_the_portal_namespace(self):
        for lang in LOCALES:
            keys = json.loads((SRC / 'i18n' / lang / 'portal.json').read_text(encoding='utf-8'))
            self.assertTrue(all(k.startswith('portal.') for k in keys), lang)


class PackagingTests(unittest.TestCase):
    def test_package_data_covers_every_static_file(self):
        text = (ROOT / 'pyproject.toml').read_text(encoding='utf-8')
        patterns = re.findall(r'"([^"]+)"', re.search(r'\[tool\.setuptools\.package-data\]\s*disk_steward = \[(.*?)\]', text, re.S).group(1))
        for path in STATIC.rglob('*'):
            if path.is_file() and path.name != '.DS_Store':
                rel = path.relative_to(SRC).as_posix()
                with self.subTest(rel=rel):
                    self.assertTrue(any(fnmatch.fnmatchcase(rel, p) for p in patterns), f'{rel} is not in package-data')

    def test_assets_are_served_from_a_zipapp_and_traversal_is_refused_there_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / 'app'
            app.mkdir()
            shutil.copytree(SRC, app / 'disk_steward', ignore=shutil.ignore_patterns('__pycache__'))
            (app / '__main__.py').write_text(
                'import json\n'
                'from disk_steward.portal.server import read_static\n'
                "names = ['index.html', 'locked.html', 'app.mjs', 'views/browse.mjs', 'views/insights.mjs', 'styles.css', 'favicon.svg']\n"
                "bad = ['../server.py', '%2e%2e/server.py', 'views/', 'views', '', 'nope.js', 'views/../app.mjs', '..\\\\x.js']\n"
                'print(json.dumps({"ok": {n: len(read_static(n)[0]) for n in names},\n'
                '                  "types": read_static("app.mjs")[1], "bad": {b: read_static(b) for b in bad}}))\n')
            target = Path(tmp) / 'app.pyz'
            zipapp.create_archive(app, target)
            out = subprocess.run([sys.executable, str(target)], capture_output=True, text=True, check=True,
                                 env={'PATH': os.environ.get('PATH', '')}).stdout
        data = json.loads(out)
        self.assertEqual(data['ok']['app.mjs'], (STATIC / 'app.mjs').stat().st_size)
        self.assertEqual(data['ok']['views/browse.mjs'], (STATIC / 'views' / 'browse.mjs').stat().st_size)
        self.assertEqual(data['types'], 'text/javascript; charset=utf-8')
        self.assertEqual(data['bad'], {b: None for b in data['bad']})

    def test_read_static_reads_plain_files_only(self):
        self.assertIsNotNone(read_static('app.mjs'))
        self.assertEqual(read_static('favicon.svg')[1], 'image/svg+xml')
        self.assertEqual(read_static('theme-init.js')[1], 'text/javascript; charset=utf-8')
        self.assertIsNone(read_static('../server.py'))
        self.assertIsNone(read_static('views'))


if __name__ == '__main__':
    unittest.main()
