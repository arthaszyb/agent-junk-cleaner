"""Translation catalogs: i18n/<locale>/<namespace>.json, merged per locale."""
from __future__ import annotations

import json
import math
import os
import pkgutil
import re
from typing import Dict, List, Optional, Set, Tuple

from ..model import ErrorCode, StewardError

LOCALES = ('en', 'zh-CN')
# Each milestone owns one namespace file. A fixed list (instead of listing the directory) keeps
# loading identical for source trees, wheels and zipapps, where Python 3.9 cannot enumerate resources.
NAMESPACES = ('core', 'rules', 'cli', 'portal', 'advisor', 'insights', 'native')

# (locale, key) pairs that t() could not resolve. Tests assert it stays empty.
MISSING: Set[Tuple[str, str]] = set()

_cache: Dict[str, Dict[str, str]] = {}


def _read_namespace(locale: str, namespace: str) -> Optional[Dict[str, str]]:
    """None when the namespace file does not exist."""
    try:
        data = pkgutil.get_data(__name__, f'{locale}/{namespace}.json')
    except OSError as e:
        # zipimport reports a missing member as a bare OSError with errno 0, not FileNotFoundError
        if isinstance(e, FileNotFoundError) or e.errno == 0:
            return None
        raise
    if data is None:
        raise RuntimeError(f'cannot load resources of {__name__}')
    return json.loads(data.decode('utf-8'))


def _load(locale: str) -> Dict[str, str]:
    merged: Dict[str, str] = {}
    for namespace in NAMESPACES:
        entries = _read_namespace(locale, namespace)
        if entries is None:
            continue  # milestones add their namespaces over time
        clash = sorted(merged.keys() & entries.keys())
        if clash:
            raise RuntimeError(f'i18n keys defined in more than one namespace ({locale}): {clash}')
        merged.update(entries)
    return merged


def _catalog(locale: str) -> Dict[str, str]:
    if locale not in LOCALES:
        raise StewardError(ErrorCode.INVALID, 'error.invalid_locale', locale=str(locale))
    if locale not in _cache:
        _cache[locale] = _load(locale)
    return _cache[locale]


def catalog(locale: str) -> Dict[str, str]:
    return dict(_catalog(locale))


def t(key: str, locale: str = 'en', **args: object) -> str:
    text = _catalog(locale).get(key)
    if text is None:
        MISSING.add((locale, key))
        return key
    return text.format_map(args)


def _locale_of_tag(tag: str) -> Optional[str]:
    lang = re.split(r'[-_.@]', tag.strip().lower(), maxsplit=1)[0]
    if lang == 'zh':
        return 'zh-CN'
    if lang == 'en':
        return 'en'
    return None


def _from_accept_language(header: str) -> Optional[str]:
    ranked: List[Tuple[float, int, str]] = []
    for order, part in enumerate(header.split(',')):
        tag, _, params = part.partition(';')
        q = 1.0
        m = re.search(r'q\s*=\s*([^;\s,]*)', params)
        if m:
            try:
                q = float(m.group(1))
            except ValueError:
                continue   # 'q=1..5' is not a weight; a header is user input, so the tag is skipped
            if not math.isfinite(q):   # float() accepts 'nan' and 'inf'
                continue
        if tag.strip() and q > 0:
            ranked.append((-q, order, tag))
    for _, _, tag in sorted(ranked):
        found = _locale_of_tag(tag)
        if found:
            return found
    return None


def detect_locale(env_lang: Optional[str] = None, accept_language: Optional[str] = None) -> str:
    """'zh-CN' for any zh*, otherwise 'en'.

    Accept-Language wins over env_lang: the browser's preference is the more specific signal. With
    both None the process environment (LC_ALL, LC_MESSAGES, LANG) is consulted."""
    if accept_language:
        found = _from_accept_language(accept_language)
        if found:
            return found
    if env_lang is None and accept_language is None:
        env_lang = os.environ.get('LC_ALL') or os.environ.get('LC_MESSAGES') or os.environ.get('LANG')
    if env_lang:
        return _locale_of_tag(env_lang) or 'en'
    return 'en'
