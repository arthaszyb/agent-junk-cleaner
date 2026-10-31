"""Classifier: turns a logical path plus its stat into a Match, using the catalog and detector facts.

Trying every rule against every file is far too slow for 10^6 files. Instead, for each directory the
classifier works out once which rules can still match something directly below it (an NFA over the
pattern segments, advanced one directory name at a time), together with a compiled matcher for the last
path segment. A file is then tested against that short list only."""
from __future__ import annotations

import errno
import os
import re
import stat
from fnmatch import translate
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Pattern, Set, Tuple

from .catalog import Catalog
from .catalog.protections import DATABASE_MARKER_NAMES, LABELLED_PRIORITY, LEVELDB_NAMES, PROTECTION_PRIORITY
from .detectors import REGISTRY, facts_digest
from .fsutil import SF_DATALESS, name_regex
from .model import Evidence
from .model import Domain, Kind, Match, Refinement, Rule, StatInfo, Tier, Unit
from .paths import Bases, match_pattern
from .settings import Settings

# Marker on the rule side of a group key for items protected on top of an unprotected rule:
# 'claude.cli-versions#protected', 'claude.projects#protected:~/.claude/projects/x'. Rule ids cannot hold '#'.
PROTECTED_SUFFIX = '#protected'
UNKNOWN = 'unknown'
SQLITE_MAGIC = b'SQLite format 3\0'
_GLOB_CHARS = '*?['
_LIBRARY_CONTAINERS = ('Application Support', 'Caches', 'Logs')

# A pattern segment: (kind, text, regex). kind 0 = '**', 1 = literal, 2 = glob.
_Seg = Tuple[int, str, Optional[Pattern[str]]]
_Entry = Tuple[int, Tuple[_Seg, ...], FrozenSet[int]]   # (rule index, pattern segments, NFA states)


def split_group_key(key: str) -> Tuple[str, str]:
    """(rule id, suffix) of a group key. 'claude.projects#protected:~/.claude/projects/x' ->
    ('claude.projects', '~/.claude/projects/x'); the protected marker is not part of either."""
    head, _, suffix = key.partition(':')
    return head.partition('#')[0], suffix


def protect_group_key(key: str) -> str:
    """The key of the protected variant of a group: the marker goes right after the rule id."""
    head, sep, rest = key.partition(':')
    if head.endswith(PROTECTED_SUFFIX):
        return key
    return head + PROTECTED_SUFFIX + sep + rest


def is_protected_variant(key: str) -> bool:
    return key.partition(':')[0].endswith(PROTECTED_SUFFIX)


def _compile_pattern(pattern: str) -> Tuple[_Seg, ...]:
    out: List[_Seg] = []
    for seg in pattern.split('/'):
        if seg == '**':
            out.append((0, seg, None))
        elif not any(c in seg for c in _GLOB_CHARS):
            out.append((1, seg, None))
        else:
            out.append((2, seg, re.compile(translate(seg))))
    return tuple(out)


def _close(psegs: Tuple[_Seg, ...], states: Set[int]) -> Set[int]:
    """Add the states reachable by letting '**' match zero segments."""
    stack = list(states)
    while stack:
        j = stack.pop()
        if j < len(psegs) and psegs[j][0] == 0 and j + 1 not in states:
            states.add(j + 1)
            stack.append(j + 1)
    return states


def _step(psegs: Tuple[_Seg, ...], states: FrozenSet[int], segment: str) -> FrozenSet[int]:
    n = len(psegs)
    out: Set[int] = set()
    for j in states:
        kind, text, regex = psegs[j]
        if kind == 0:
            out.add(j)
        elif kind == 1:
            if segment == text:
                out.add(j + 1)
        elif regex.match(segment):  # type: ignore[union-attr]
            out.add(j + 1)
    _close(psegs, out)
    out.discard(n)  # fully matched above this level: nothing below can match
    return frozenset(out)


def _last_segment_globs(psegs: Tuple[_Seg, ...], states: FrozenSet[int]) -> Optional[Set[str]]:
    """Globs the final path segment must satisfy; None means any name (trailing '**')."""
    globs: Set[str] = set()
    for j in states:
        if psegs[j][0] == 0:
            if all(s[0] == 0 for s in psegs[j:]):
                return None
        elif all(s[0] == 0 for s in psegs[j + 1:]):
            globs.add(psegs[j][1])
    return globs


_matcher_cache: Dict[Tuple[str, ...], Pattern[str]] = {}


def _matcher(globs: Set[str]) -> Pattern[str]:
    key = tuple(sorted(globs))
    found = _matcher_cache.get(key)
    if found is None:
        found = _matcher_cache[key] = re.compile('|'.join(translate(g) for g in key)) if key else re.compile(r'(?!)')
    return found


class _Rule:
    __slots__ = ('rule', 'patterns', 'inc', 'exc', 'min_size', 'refine', 'facts')

    def __init__(self, rule: Rule, facts: Dict[str, Any]):
        self.rule = rule
        self.patterns = tuple(_compile_pattern(p) for p in rule.patterns)
        self.inc = name_regex(rule.name_include) if rule.name_include else None
        self.exc = name_regex(rule.name_exclude) if rule.name_exclude else None
        self.min_size = rule.min_size
        self.refine = None
        self.facts: Dict[str, Any] = {}
        if rule.detector:
            detector = REGISTRY.get(rule.detector)
            if detector is None:
                raise ValueError(f'rule {rule.id} names unknown detector {rule.detector!r}')
            if rule.detector not in facts:
                raise ValueError(f'no facts for detector {rule.detector!r} (rule {rule.id})')
            self.refine = detector.refine
            self.facts = facts[rule.detector]


class _Cand:
    __slots__ = ('cr', 'matcher', 'match', 'scoped')

    def __init__(self, cr: _Rule, matcher: Optional[Pattern[str]], match: Match):
        self.cr = cr
        self.matcher = matcher
        self.match = match
        # Protections apply in every domain, so their Match takes the domain of the root being scanned.
        # One Match per domain is kept here so the hot path does not build objects.
        self.scoped: Optional[Dict[Domain, Match]] = {} if cr.rule.priority >= PROTECTION_PRIORITY else None


class Classifier:
    def __init__(self, catalog: Catalog, facts: Dict[str, Any], bases: Bases, settings: Settings,
                 sniff: bool = True):
        """`sniff` lets classify() read the first 16 bytes of files matched by a REVIEW rule to spot SQLite
        databases; benchmarks and synthetic-path tests turn it off."""
        self.sniff = sniff
        self.catalog = catalog
        self.facts = facts
        self.facts_hash = facts_digest(facts)
        self.bases = bases
        self.settings = settings
        self._rules: List[_Rule] = [_Rule(r, facts) for r in catalog.rules]
        # per unit (0 = file, 1 = bundle): the initial NFA entries and the per-directory caches
        self._initial: List[Tuple[_Entry, ...]] = []
        for unit in (Unit.FILE, Unit.BUNDLE):
            entries: List[_Entry] = []
            for i, cr in enumerate(self._rules):
                if cr.rule.unit != unit:
                    continue
                for psegs in cr.patterns:
                    entries.append((i, psegs, frozenset(_close(psegs, {0}))))
            self._initial.append(tuple(entries))
        self._live_cache: List[Dict[str, Tuple[_Entry, ...]]] = [{}, {}]
        self._cand_cache: List[Dict[str, Tuple[_Cand, ...]]] = [{}, {}]
        self._unknown_cache: Dict[Tuple[str, str, bool], Match] = {}
        # privacy
        self._priv_literal = {p for p in catalog.privacy_skip if not any(c in p for c in _GLOB_CHARS)}
        self._priv_globs = [(p, len(p.split('/')), '**' in p.split('/'))
                            for p in catalog.privacy_skip if any(c in p for c in _GLOB_CHARS)]
        self._priv_cache: Dict[str, bool] = {}
        # context protections: the walk tells us a file sits in a git work tree or next to a database
        self._ctx_rules: Dict[str, Optional[Rule]] = {}
        for name, rule_id in (('git', 'prot.git-tree'), ('db', 'prot.databases')):
            try:
                self._ctx_rules[name] = catalog.rule(rule_id)
            except KeyError:
                self._ctx_rules[name] = None   # a catalog without the rule has no such protection
        self._ctx_cache: Dict[Tuple[str, Domain], Match] = {}
        self._priority = {r.id: r.priority for r in catalog.rules}
        self._marker_re = name_regex(DATABASE_MARKER_NAMES)
        self._ldb_current = name_regex(LEVELDB_NAMES[:1])
        self._ldb_manifest = name_regex(LEVELDB_NAMES[1:])

    # privacy ------------------------------------------------------------------------------------------

    def _privacy_self(self, logical: str) -> bool:
        if logical in self._priv_literal:
            return True
        depth = logical.count('/') + 1
        for pattern, n, has_star2 in self._priv_globs:
            if (has_star2 or n == depth) and match_pattern(pattern, logical):
                return True
        return False

    def privacy_skipped(self, logical: str) -> bool:
        """True for a path that is, or lies below, a privacy-skipped directory."""
        hit = self._priv_cache.get(logical)
        if hit is not None:
            return hit
        chain: List[str] = []
        cur = logical
        result = False
        while True:
            known = self._priv_cache.get(cur)
            if known is not None:
                result = known
                break
            chain.append(cur)
            if '/' not in cur:
                break
            cur = cur.rpartition('/')[0]
        for path in reversed(chain):
            result = result or self._privacy_self(path)
            self._priv_cache[path] = result
        return result

    # candidate lists ----------------------------------------------------------------------------------

    def _live(self, unit: int, directory: str) -> Tuple[_Entry, ...]:
        cache = self._live_cache[unit]
        hit = cache.get(directory)
        if hit is not None:
            return hit
        chain: List[str] = []
        cur = directory
        while True:
            known = cache.get(cur)
            if known is not None:
                base = known
                break
            chain.append(cur)
            if '/' not in cur:
                base = self._initial[unit]
                break
            cur = cur.rpartition('/')[0]
        for path in reversed(chain):
            segment = path.rpartition('/')[2]
            nxt: List[_Entry] = []
            for i, psegs, states in base:
                moved = _step(psegs, states, segment)
                if moved:
                    nxt.append((i, psegs, moved))
            base = cache[path] = tuple(nxt)
        return base

    def _cands(self, unit: int, directory: str) -> Tuple[_Cand, ...]:
        cache = self._cand_cache[unit]
        hit = cache.get(directory)
        if hit is not None:
            return hit
        per_rule: Dict[int, Optional[Set[str]]] = {}
        for i, psegs, states in self._live(unit, directory):
            globs = _last_segment_globs(psegs, states)
            if globs is None:
                per_rule[i] = None
            elif i not in per_rule:
                per_rule[i] = set(globs)
            elif per_rule[i] is not None:
                per_rule[i].update(globs)  # type: ignore[union-attr]
        out: List[_Cand] = []
        dir_segments = directory.split('/')
        for i in sorted(per_rule):
            globs = per_rule[i]
            if globs is not None and not globs:
                continue
            cr = self._rules[i]
            rule = cr.rule
            depth = rule.group_depth
            if depth is None:
                key = rule.id
            elif unit == 0 or depth <= len(dir_segments):
                # a file's group never reaches into its own name, so it stops at the directory
                key = f'{rule.id}:' + '/'.join(dir_segments[:depth])
            else:
                key = ''  # bundle closer to the root than the depth: its own path, filled in per match
            match = Match(rule.id, rule.tier, rule.domain, rule.kind, rule.unit, key)
            out.append(_Cand(cr, None if globs is None else _matcher(globs), match))
        result = cache[directory] = tuple(out)
        return result

    # classification -----------------------------------------------------------------------------------

    @staticmethod
    def _finish(cand: _Cand, logical: str, ref: Optional[Refinement], root_domain: Domain) -> Match:
        rule = cand.cr.rule
        match = cand.match
        domain = rule.domain
        if cand.scoped is not None:
            domain = root_domain
            match = cand.scoped.get(root_domain)
            if match is None:
                match = cand.scoped[root_domain] = Match(rule.id, rule.tier, root_domain, rule.kind, rule.unit,
                                                         f'{rule.id}:{root_domain.value}')
        key = match.group_key or f'{rule.id}:{logical}'
        if ref is None:
            return match if match.group_key else Match(rule.id, rule.tier, domain, rule.kind, rule.unit, key)
        tier = ref.tier or rule.tier
        if ref.group_key is not None:
            key = ref.group_key
        if tier == Tier.PROTECTED and rule.tier != Tier.PROTECTED:
            key = protect_group_key(key)  # a group never mixes protected and selectable items
        return Match(rule.id, tier, domain, rule.kind, rule.unit, key, ref.evidence)

    def _first_match(self, unit: int, logical: str, st: StatInfo, is_dir: bool, root_domain: Domain,
                     git_tree: bool = False) -> Optional[Match]:
        parent, _, name = logical.rpartition('/')
        lowered = name.lower()
        for cand in self._cands(unit, parent):
            cr = cand.cr
            if cr.inc is not None and not cr.inc.match(lowered):
                continue
            if cr.exc is not None and cr.exc.match(lowered):
                continue
            if cand.matcher is not None and not cand.matcher.match(name):
                continue
            if cr.min_size and st.size < cr.min_size:
                continue
            if cr.refine is None:
                found = self._finish(cand, logical, None, root_domain)
            else:
                ref = cr.refine(cr.rule, logical, st, is_dir, cr.facts)
                if ref is not None and ref.skip:
                    continue
                found = self._finish(cand, logical, ref, root_domain)
            # Inside a git work tree only protections and dependency folders (idle node_modules/.venv) may
            # claim a directory; anything else would swallow tracked project files.
            if git_tree and not (found.tier == Tier.PROTECTED or found.kind == Kind.PROJECT_DEPS):
                continue
            if self.sniff and found.tier == Tier.REVIEW and not is_dir:
                found = self._sqlite_guard(found, logical, st)
            return found
        return None

    def _sqlite_guard(self, found: Match, logical: str, st: StatInfo) -> Match:
        """Review rules match by name, and a database can carry any name: protect files that start like one.
        A file whose header cannot be read is protected too, since it cannot be shown to be safe."""
        if not stat.S_ISREG(st.mode):
            return found
        if st.flags & SF_DATALESS:  # reading would download it
            evidence = Evidence('evidence.header_unreadable', {'reason': 'dataless'})
        else:
            try:
                fd = os.open(self.bases.to_abs(logical), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                try:
                    head = os.read(fd, len(SQLITE_MAGIC))
                finally:
                    os.close(fd)
            except OSError as e:
                evidence = Evidence('evidence.header_unreadable', {'reason': errno.errorcode.get(e.errno, str(e.errno))})
            else:
                if head != SQLITE_MAGIC:
                    return found
                evidence = Evidence('evidence.sqlite_header', {})
        return Match(found.rule_id, Tier.PROTECTED, found.domain, found.kind, found.unit,
                     protect_group_key(found.group_key), found.evidence + (evidence,))

    def _context_match(self, which: str, root_domain: Domain) -> Optional[Match]:
        rule = self._ctx_rules[which]
        if rule is None:
            return None
        key = (rule.id, root_domain)
        hit = self._ctx_cache.get(key)
        if hit is None:
            hit = self._ctx_cache[key] = Match(rule.id, rule.tier, root_domain, rule.kind, Unit.FILE,
                                               f'{rule.id}:{root_domain.value}')
        return hit

    def classify_dir(self, logical: str, st: StatInfo, root_domain: Domain = Domain.AGENT, *,
                     in_git_tree: bool = False) -> Optional[Match]:
        """The bundle rule that claims this directory, or None (the scanner then descends)."""
        return self._first_match(1, logical, st, True, root_domain, in_git_tree)

    def _unknown(self, logical: str, root_domain: Domain, is_dir: bool) -> Match:
        parent = logical if is_dir else logical.rpartition('/')[0]
        cache_key = (parent, root_domain.value, is_dir)
        hit = self._unknown_cache.get(cache_key)
        if hit is None:
            segments = parent.split('/')
            # '~/Library/Application Support' alone names no app, so those trees keep one more segment
            depth = 4 if segments[1:2] == ['Library'] and segments[2:3] and segments[2] in _LIBRARY_CONTAINERS else 3
            prefix = '/'.join(segments[:depth])
            hit = self._unknown_cache[cache_key] = Match(
                UNKNOWN, Tier.PROTECTED, root_domain, Kind.UNKNOWN, Unit.BUNDLE if is_dir else Unit.FILE,
                f'{UNKNOWN}:{prefix}')
        return hit

    def classify(self, logical: str, st: StatInfo, is_dir: bool = False, root_domain: Domain = Domain.AGENT, *,
                 in_git_tree: bool = False, db_dir: bool = False) -> Match:
        """File rules for a file, bundle rules for a directory; anything no rule claims is protected 'unknown'.

        `in_git_tree` (a .git entry sits in this directory or above it) protects every file, whatever its
        name. `db_dir` (this directory directly holds a database) protects the files that a generic junk or
        review rule would otherwise offer, since journals and segments next to a database are part of it;
        unknown files are protected anyway, and labelled rules (priority 900 and up) keep their verdict."""
        if is_dir:
            found = self._first_match(1, logical, st, True, root_domain, in_git_tree)
            return found if found is not None else self._unknown(logical, root_domain, True)
        if in_git_tree:
            forced = self._context_match('git', root_domain)
            if forced is not None:
                return forced
        found = self._first_match(0, logical, st, False, root_domain)
        if found is None:
            return self._unknown(logical, root_domain, False)
        if db_dir and found.tier != Tier.PROTECTED and self._priority[found.rule_id] < LABELLED_PRIORITY:
            forced = self._context_match('db', root_domain)
            if forced is not None:
                return forced
        return found

    # context -------------------------------------------------------------------------------------------

    def has_database_markers(self, file_names: Iterable[str]) -> bool:
        """True if a directory holding these files is a database directory (SQLite, LevelDB, Realm, ...)."""
        current = manifest = False
        for name in file_names:
            lowered = name.lower()
            if self._marker_re.match(lowered):
                return True
            current = current or bool(self._ldb_current.match(lowered))
            manifest = manifest or bool(self._ldb_manifest.match(lowered))
        return current and manifest

    def file_context(self, logical: str) -> Tuple[bool, bool]:
        """(in_git_tree, db_dir) of a file, worked out from the disk. The scanner derives the same flags while
        it walks; code that classifies a single path later (the executor) calls this instead."""
        parent = logical.rpartition('/')[0]
        in_tree = False
        directory = ''
        for segment in parent.split('/'):
            directory = f'{directory}/{segment}' if directory else segment
            in_tree = in_tree or os.path.lexists(self.bases.to_abs(directory + '/.git'))
        with os.scandir(self.bases.to_abs(parent)) as it:
            names = [e.name for e in it if not e.is_dir(follow_symlinks=False)]
        return in_tree, self.has_database_markers(names)
