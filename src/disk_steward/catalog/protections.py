"""Cross-domain protections: paths that are never opened, plus the highest-priority rules that keep
credentials, databases and git repositories out of every cleanup."""
from __future__ import annotations

from typing import Tuple

from ..model import Domain, Kind, Rule, Tier, Unit

PROTECTION_PRIORITY = 1000
# Rules at or above this priority are hand-written for exact paths (labelled protections, state-tmp). The walk's
# database-neighbour protection overrides only matches below it, so such a rule keeps its own verdict.
LABELLED_PRIORITY = 900

# TCC-protected or private by nature. A scan never lists, stats or opens these, so macOS never
# shows a permission prompt for them and their content is never read.
PRIVACY_SKIP: Tuple[str, ...] = (
    '~/Library/Containers', '~/Library/Group Containers', '~/Library/Daemon Containers', '~/Library/Mail',
    '~/Library/Messages', '~/Library/Safari', '~/Library/Cookies', '~/Library/HomeKit', '~/Library/Calendars',
    '~/Library/Reminders', '~/Library/Suggestions', '~/Library/Biome', '~/Library/DuetExpertCenter',
    '~/Library/IdentityServices', '~/Library/Accounts', '~/Library/Sharing', '~/Library/Trial',
    '~/Library/PersonalizationPortrait', '~/Library/Keychains', '~/Library/Photos',
    '~/Library/Mobile Documents', '~/Library/CloudStorage',
    '~/Library/Metadata/CoreSpotlight',
    '~/Library/Application Support/AddressBook', '~/Library/Application Support/CallHistoryDB',
    '~/Library/Application Support/CallHistoryTransactions', '~/Library/Application Support/com.apple.TCC',
    '~/Library/Application Support/Knowledge', '~/Library/Application Support/MobileSync',
    '~/Library/Application Support/FaceTime', '~/Library/Application Support/FileProvider',
    '~/Library/Application Support/com.apple.sharedfilelist',
    '~/Library/Caches/com.apple.*', '~/Library/Application Support/com.apple.*',
    '~/.Trash', '~/Pictures/*.photoslibrary', '~/Music/Music',
)

# Basename globs, matched case-insensitively. walk_bundle uses the same lists, so a folder that holds
# one of these files is protected as a whole unless its kind opts out of name taint.
CREDENTIAL_NAMES: Tuple[str, ...] = (
    'auth.json', '.credentials.json', 'credentials*', '*.pem', '*.key', '*.p12', 'id_rsa*', 'id_ed25519*',
    'id_ecdsa*', '*.keychain*', '.netrc', '*token*.json',
    '.env', '.env.*', '.git-credentials', '.npmrc', '.pypirc', '.pgpass', '*creds*', '*secret*', '*-key',
    '.*-key', '*.pfx', '*.jks', '*.kdbx',
)
DATABASE_NAMES: Tuple[str, ...] = (
    '*.sqlite', '*.sqlite3', '*.sqlite-wal', '*.sqlite-shm', '*.sqlite-journal', '*.db', '*.db-wal', '*.db-shm',
    '*.db-journal', '*.realm', '*.ldb', 'MANIFEST-*', 'CURRENT', 'LOCK', 'LOG', 'LOG.old',
    '*.vscdb', '*.duckdb', '*.mdb',
    # Chromium's extension-less profile databases
    'Cookies', 'Login Data', 'History', 'Web Data', 'Favicons', 'Top Sites',
)

# A directory that directly holds one of these is a database directory: its other files (journals, logs,
# LevelDB segments, sidecars with names no list can predict) are protected with it. 'CURRENT' alone is too
# common a name, so LevelDB counts only when CURRENT and a MANIFEST-* file are both there.
DATABASE_MARKER_NAMES: Tuple[str, ...] = ('*.sqlite*', '*.db', '*.db-wal', '*.ldb', '*.realm', '*.vscdb', '*.duckdb',
                                          '*.mdb')
LEVELDB_NAMES: Tuple[str, ...] = ('CURRENT', 'MANIFEST-*')

_EVERYWHERE = ('~/**', '$TMPDIR/**', '$CACHEDIR/**')


def rules(settings: object) -> Tuple[Rule, ...]:
    return (
        Rule(id='prot.credentials', provider='system', domain=Domain.AGENT, tier=Tier.PROTECTED,
             kind=Kind.CREDENTIAL, patterns=_EVERYWHERE, name_include=CREDENTIAL_NAMES,
             priority=PROTECTION_PRIORITY),
        Rule(id='prot.databases', provider='system', domain=Domain.AGENT, tier=Tier.PROTECTED,
             kind=Kind.DATABASE, patterns=_EVERYWHERE, name_include=DATABASE_NAMES,
             priority=PROTECTION_PRIORITY),
        Rule(id='prot.git', provider='system', domain=Domain.AGENT, tier=Tier.PROTECTED,
             kind=Kind.WORK_PRODUCT, patterns=('~/**/.git',), unit=Unit.BUNDLE, priority=PROTECTION_PRIORITY),
        # Every file of a git work tree. The classifier applies it through the walk's in_git_tree flag (a work
        # tree is wherever a .git entry sits, not only inside .git); the patterns cover the .git folder itself.
        Rule(id='prot.git-tree', provider='system', domain=Domain.AGENT, tier=Tier.PROTECTED,
             kind=Kind.WORK_PRODUCT, patterns=('~/**/.git/**', '$TMPDIR/**/.git/**', '$CACHEDIR/**/.git/**'),
             priority=PROTECTION_PRIORITY),
    )
