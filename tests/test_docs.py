"""Documentation that is generated from the code, or can be checked against it, stays in step with it."""
from __future__ import annotations

import importlib.util
import re
import unittest
from pathlib import Path

from disk_steward import __version__
from disk_steward.cli import _parser

ROOT = Path(__file__).resolve().parent.parent
DOC_FILES = [ROOT / name for name in ('README.md', 'CHANGELOG.md', 'SECURITY.md', 'CONTRIBUTING.md')] + sorted(
    (ROOT / 'docs').glob('*.md'))


def load_generator():
    spec = importlib.util.spec_from_file_location('gen_catalog_doc', ROOT / 'scripts' / 'gen_catalog_doc.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def code_segments(text):
    """Fenced blocks and inline code spans of a Markdown text."""
    fenced = re.findall(r'```[^\n]*\n(.*?)```', text, re.S)
    inline = re.findall(r'`([^`\n]+)`', re.sub(r'```.*?```', '', text, flags=re.S))
    return fenced + inline


class CatalogDocTests(unittest.TestCase):
    def test_the_committed_catalog_doc_is_what_the_generator_writes(self):
        committed = (ROOT / 'docs' / 'catalog.md').read_text(encoding='utf-8')
        self.assertEqual(committed, load_generator().render(),
                         'docs/catalog.md is stale: run `python3 scripts/gen_catalog_doc.py > docs/catalog.md`')

    def test_the_output_is_deterministic(self):
        gen = load_generator()
        self.assertEqual(gen.render(), gen.render())


class DocsTests(unittest.TestCase):
    def test_relative_links_resolve(self):
        for doc in DOC_FILES:
            for target in re.findall(r'\]\(([^)\s]+)\)', doc.read_text(encoding='utf-8')):
                if re.match(r'(https?:|mailto:|#)', target):
                    continue
                with self.subTest(doc=doc.name, target=target):
                    self.assertTrue((doc.parent / target.split('#')[0]).exists())

    def test_the_changelog_starts_with_the_package_version(self):
        headings = [line for line in (ROOT / 'CHANGELOG.md').read_text(encoding='utf-8').splitlines()
                    if line.startswith('## ')]
        self.assertIn(__version__, headings[0])

    def test_commands_shown_in_code_are_real_subcommands(self):
        found = re.search(r'\{([a-z,\-]+)\}', _parser().format_help())
        known = set(found.group(1).split(','))
        for doc in DOC_FILES:
            for segment in code_segments(doc.read_text(encoding='utf-8')):
                for command in re.findall(r'(?<![\w./-])disk-steward ([a-z][a-z-]*)', segment):
                    with self.subTest(doc=doc.name, command=command):
                        self.assertIn(command, known)


if __name__ == '__main__':
    unittest.main()
