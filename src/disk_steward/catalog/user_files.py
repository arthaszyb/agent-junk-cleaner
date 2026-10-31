"""The user's own folders (settings.user_roots): installers, dependency folders of idle projects and large or
idle files. Everything else in them is listed as protected, so the space stays visible but is never offered."""
from __future__ import annotations

import os
from typing import Dict, Iterable, Tuple

from . import ScanRoot
from ..model import Action, AgeBasis, Domain, ErrorCode, Kind, Rule, StewardError, Tier, Unit
from ..paths import Bases, split_logical
from ..settings import Settings

D = Domain.USER_FILES
DOWNLOADS = '~/Downloads'
INSTALLER_NAMES = ('*.dmg', '*.pkg', '*.mpkg', '*.iso', '*.xip')
# Packages that look like folders but are one document to the user, or one app. Never listed inside.
PACKAGE_GLOBS = ('*.photoslibrary', '*.musiclibrary', '*.fcpbundle', '*.logicx', '*.band', '*.imovielibrary',
                 '*.tvlibrary', '*.xcarchive')
DEPS_DIRS = ('node_modules', '.venv', 'venv', 'target')
_TRASH_OR_DELETE = dict(actions=(Action.TRASH, Action.DELETE), default_action=Action.TRASH)

# iCloud "Desktop & Documents" mirrors these two folders here when it is switched on.
_ICLOUD_MIRRORS = (('~/Desktop', '~/Library/Mobile Documents/com~apple~CloudDocs/Desktop'),
                   ('~/Documents', '~/Library/Mobile Documents/com~apple~CloudDocs/Documents'))
_CLOUD_DIRS = ('~/Library/Mobile Documents', '~/Library/CloudStorage')


def _user_roots(settings: Settings) -> Tuple[str, ...]:
    out = []
    for root in settings.user_roots:
        prefix, segments = split_logical(root)
        # '$TMPDIR/x' belongs to app-library, and '~' alone would put every rule on the whole home folder
        if prefix != '~' or not segments:
            raise StewardError(ErrorCode.INVALID, 'error.invalid_setting', field='user_roots')
        out.append(root)
    return tuple(dict.fromkeys(out))


def roots(settings: Settings) -> Tuple[ScanRoot, ...]:
    return tuple(ScanRoot(r, D) for r in _user_roots(settings))


def _inside(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder + '/')


def rules(settings: Settings) -> Tuple[Rule, ...]:
    user_roots = _user_roots(settings)
    downloads = tuple(r for r in user_roots if _inside(r, DOWNLOADS))
    elsewhere = tuple(r for r in user_roots if r not in downloads)
    installer_age = settings.installer_age_days

    def rule(**kw: object) -> Rule:
        return Rule(provider='user', domain=D, **kw)  # type: ignore[arg-type]

    out = []
    # Apps outside Downloads are somebody's installed software; inside Downloads they are installers.
    packages = tuple(f'{r}/**/{g}' for r in user_roots for g in PACKAGE_GLOBS) + tuple(
        f'{r}/**/*.app' for r in elsewhere)
    if packages:
        out.append(rule(id='user.packages', tier=Tier.PROTECTED, kind=Kind.WORK_PRODUCT, unit=Unit.BUNDLE,
                        patterns=packages, priority=500))
    if downloads:
        out.append(rule(id='user.download-apps', tier=Tier.REVIEW, kind=Kind.INSTALLER, unit=Unit.BUNDLE,
                        patterns=tuple(f'{r}/**/*.app' for r in downloads), group_depth=2, priority=100,
                        default_min_age_days=installer_age, age_basis=AgeBasis.LAST_USED, **_TRASH_OR_DELETE))
        out.append(rule(id='user.installers', tier=Tier.REVIEW, kind=Kind.INSTALLER,
                        patterns=tuple(f'{r}/**' for r in downloads), name_include=INSTALLER_NAMES, group_depth=2,
                        priority=150, default_min_age_days=installer_age, age_basis=AgeBasis.LAST_USED,
                        **_TRASH_OR_DELETE))
    if user_roots:
        everywhere = tuple(f'{r}/**' for r in user_roots)
        out.append(rule(id='user.project-deps', tier=Tier.REVIEW, kind=Kind.PROJECT_DEPS, unit=Unit.BUNDLE,
                        patterns=tuple(f'{r}/**/{d}' for r in user_roots for d in DEPS_DIRS), group_depth=2,
                        priority=200, detector='project_deps', actions=(Action.DELETE, Action.TRASH),
                        default_action=Action.DELETE))
        out.append(rule(id='user.large', tier=Tier.REVIEW, kind=Kind.LARGE_FILE, patterns=everywhere,
                        min_size=settings.large_file_min_bytes,
                        group_depth=2, priority=120, age_basis=AgeBasis.LAST_USED, **_TRASH_OR_DELETE))
        out.append(rule(id='user.idle', tier=Tier.REVIEW, kind=Kind.IDLE_FILE, patterns=everywhere,
                        min_size=settings.idle_file_min_bytes,
                        group_depth=2, priority=110, default_min_age_days=settings.idle_days,
                        age_basis=AgeBasis.LAST_USED, **_TRASH_OR_DELETE))
        # The label of the generic 'unknown' group names a folder nobody asked about; this one reads as
        # "Other files in ~/Documents/Taxes". Same grouping depth as unknown, same protection.
        out.append(rule(id='user.other', tier=Tier.PROTECTED, kind=Kind.UNKNOWN, patterns=everywhere,
                        group_depth=3, priority=10))
    return tuple(out)


def _exists(bases: Bases, logical: str) -> bool:
    try:
        os.lstat(bases.to_abs(logical))
    except (FileNotFoundError, NotADirectoryError):
        return False
    except PermissionError:
        return True   # macOS hides the iCloud container unless iCloud Drive keeps data in it
    return True


def cloud_synced_roots(bases: Bases, user_roots: Iterable[str]) -> Dict[str, str]:
    """Logical user roots whose files live in iCloud or another cloud provider, with the reason
    ('cloud_folder' or 'icloud_desktop_documents'). Scanning them can download placeholders, so a report may
    want to warn. Nothing is opened below ~/Library/Mobile Documents."""
    found: Dict[str, str] = {}
    mirrored = [folder for folder, mirror in _ICLOUD_MIRRORS if _exists(bases, mirror)]
    for root in user_roots:
        real = os.path.realpath(bases.to_abs(root))
        if any(_inside(real, os.path.join(str(bases.home), cloud.partition('/')[2])) for cloud in _CLOUD_DIRS):
            found[root] = 'cloud_folder'
        elif any(_inside(root, folder) for folder in mirrored):
            found[root] = 'icloud_desktop_documents'
    return found
