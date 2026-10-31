# Disk Steward architecture

Disk Steward is a local, review-driven disk cleanup tool for macOS. It specialises in data written by AI agents (Codex, Claude and similar tools) and also covers common Mac junk. Its authority to remove anything comes from audited catalog rules plus an explicit user selection. Model output never grants that authority.

This document is the contract between modules. The shared types live in `src/disk_steward/model.py`. For what each safety mechanism defends against, see [safety.md](safety.md); the rule list is in [catalog.md](catalog.md).

## Conventions

- Python 3.9+, standard library only. Use `from __future__ import annotations`. No `match` statements, no `slots=` dataclasses, no `X | Y` evaluated at runtime.
- No silent fallbacks. Expected user-facing failures raise `model.StewardError(code, message_key, **details)`. Anything else propagates.
- All user-visible text goes through i18n keys (`i18n.t(key, locale, **args)`). Code and comments are English. Comments explain *why*.
- **Logical paths everywhere.** Use `~/…`, `$TMPDIR/…` and `$CACHEDIR/…`. Only `paths.Bases` turns them into absolute paths. Rules, the database, plans, the API and the audit log carry logical paths only. (Group labels, the Applications list and a few diagnostics show `/Applications/...` as a plain absolute path, because no logical prefix covers it.)

## Modules

| Module | Responsibility |
|---|---|
| `model.py` | Enums, frozen dataclasses, `StewardError`, `asdict` |
| `paths.py` | `Bases(home, tmpdir, cachedir)`, `to_abs()`, `to_logical()`, `match_pattern()`, `item_key()`, data-dir resolution |
| `i18n/` | `t()`, `detect_locale()`, catalogs `i18n/<locale>/<namespace>.json` |
| `settings.py` | `Settings` dataclass loaded from and saved to `settings.json`; `settings_hash()` covers only fields that affect classification |
| `store.py` | SQLite persistence (scans, items, groups, history, scan totals, specs, plans, entries, run summaries, archives, advice) and plan retention |
| `queries.py` | Read-only SQL that `Store` has no method for (light item rows for `resolve()`, evidence rows, plan inserts, scan totals, cleanup history, `quick_totals`) |
| `macos.py` | `MacOS` protocol, `RealMacOS`, `FakeMacOS` (tests) |
| `fsutil.py` | `stat_nofollow`, `walk_bundle`, `open_parent_nofollow`, `rename_excl`, lock flags |
| `catalog/` | `load_catalog(settings) -> Catalog` (rules, roots, privacy skip list, hash). One module per area |
| `detectors/` | Dynamic refinements, one module each. `prepare()` produces facts; `refine()` adjusts a match |
| `classify.py` | `Classifier(catalog, facts, bases, settings)`: `classify()`, `classify_dir()`, `privacy_skipped()`, `file_context()` |
| `scanner.py` | Walks roots, classifies, aggregates groups, streams items to the store |
| `selection.py` | `decide()`, `resolve()`, `apply_ops()`, `migrate_spec()`: the single eligibility function |
| `planner.py` | `create_plan()`, `validate_for_run()` |
| `guards.py` | Running-app and open-file snapshots; `Guard.blockers()` |
| `actions.py` | Delete, archive, trash, restore, purge, and the staging and archive-verification helpers |
| `executor.py` | Runs a plan entry by entry with pause, exclude, cancel, preflight and crash recovery |
| `locking.py` | One fcntl operation lock per data dir, shared by CLI and portal |
| `audit.py` | Append-only rotating JSONL audit log |
| `events.py` | In-process event log with a version counter, for the portal's long poll |
| `report.py` | Text and JSON reports, growth since the last scan, volume accounting |
| `native.py` | Native cleanup commands of report-only tools: the fixed argv registry, tool probes, folder measurement and the runner ([Native commands](#native-commands-nativepy)) |
| `insights.py` | The Insights page as one dict: environment, history, growth, agents and follow-up TODOs ([Insights](#insights-insightspy)) |
| `schedule.py` | Scan-only LaunchAgent install/remove/status (remove also deletes the plist of a job that is not loaded), and the notification text |
| `service.py` | `App` facade. **The only API that `cli/` and `portal/` use** |
| `advisor/` | Anonymised payload, Codex/Claude CLI providers, manual export/import, validation ([ai-advisor.md](ai-advisor.md)) |
| `cli/` | argparse front end |
| `portal/` | Loopback HTTP server, session-token auth, JSON API, static ES modules |

## Paths (`paths.py`)

```python
class Bases:  # frozen
    home: Path; tmpdir: Path                         # resolved (realpath)
    cachedir: Optional[Path]                         # None where getconf cannot report it (not macOS)
    @classmethod
    def detect(cls, home: Optional[Path] = None) -> 'Bases'  # TMPDIR env / getconf DARWIN_USER_CACHE_DIR
    def to_abs(self, logical: str) -> Path   # raises StewardError(INVALID) for unknown prefix, '..', '.', empty segment, NUL
    def to_logical(self, p: Path) -> str     # raises StewardError(INVALID) if outside every base

def match_pattern(pattern: str, logical: str) -> bool   # segment glob: '*' within a segment, '**' zero+ segments; fnmatchcase per segment
def item_key(logical: str) -> str                         # sha1 hex[:20]
def default_data_dir() -> Path    # $DISK_STEWARD_DATA_DIR or ~/Library/Application Support/Disk Steward
```

`to_abs('$CACHEDIR/...')` raises `UNAVAILABLE` when `cachedir` is None.

## MacOS adapter (`macos.py`)

All OS queries go through this adapter so tests can script them.

```python
class OpenFiles:            # snapshot
    taken_at: float
    by_inode: dict[(dev, ino), list[str]]   # process labels 'Name(pid)'
    paths: list[str]                         # sorted absolute paths of open files (prefix checks for bundles)
    executing: dict[(dev, ino), list[str]]   # lsof 'txt' entries (running binaries)
    def holders(self, dev, ino) -> list[str]
    def holders_under(self, abs_dir: str) -> list[str]

class MacOS(Protocol):
    def running_bundle_ids(self) -> set[str]            # lsappinfo list
    def open_files(self) -> OpenFiles                    # lsof -nP -F pcftDin -w
    def last_used(self, abs_paths: list[str]) -> dict[str, float | None]   # batched mdls kMDItemLastUsedDate
    def installed_apps(self) -> dict[str, str]           # bundle id -> app path (mdfind plus /Applications, ~/Applications, /System/Applications)
    def installed_app_info(self) -> dict[str, dict[str, str]]   # bundle id -> {path, name, display_name, executable, stem}
    def trash(self, abs_path: str) -> None               # /usr/bin/trash; StewardError(UNAVAILABLE) if missing
    def trash_available(self) -> bool
    def statfs_free(self, abs_path: str) -> int
    def volume_usage(self, abs_path: str) -> tuple[int, int]   # (total, used)
    def snapshots(self) -> list[str]                     # tmutil listlocalsnapshots /
    def tool_version(self, name: str) -> str | None      # codex/claude --version
    def app_sizes(self, paths: list[str], timeout=180) -> dict[str, dict]   # one `du -sk -x` per path: {'bytes'} or {'error'}
```

`FakeMacOS` holds plain attributes (`running`, `open`, `last_used_map`, `apps`, `app_info`, `trashed`, `free`, `usage`, `trash_ok`, `snapshot_names`, `tool_versions`, `app_size_map`) and records every call in `calls`.

## Catalog, detectors, classifier

`catalog.load_catalog(settings) -> Catalog`:

```python
class Catalog:  # frozen
    rules: tuple[Rule, ...]          # sorted by priority desc, then id
    roots: tuple[ScanRoot, ...]      # where to walk; per domain
    privacy_skip: tuple[str, ...]    # logical patterns never opened; recorded as skipped_privacy
    hash: str                        # sha256 of canonical JSON of rules + roots + privacy_skip + CATALOG_SCHEMA
    def rule(self, rule_id) -> Rule

class ScanRoot:  # frozen
    path: str          # logical
    domain: Domain     # fallback domain for unmatched files under it
```

The catalog modules are listed in `catalog/__init__.py` (`_MODULES`): `protections`, `claude`, `codex`, `other_agents`, `app_library`, `user_files`, `dev_shared`. Each exposes `rules(settings)` and may expose `ROOTS` or `roots(settings)` and `PRIVACY_SKIP`. `build_catalog` validates every rule (id format, patterns start with a logical prefix, protected and report-only rules carry no actions, a rule with actions has a default). A rule is report-only when it has a `hint` or sits in the `dev-shared` domain.

**Detectors.** `detectors.REGISTRY: dict[str, Detector]`:

```python
class Detector(Protocol):
    name: str
    def prepare(self, ctx: DetectorContext) -> dict     # JSON-safe facts, included in facts_hash
    def refine(self, rule: Rule, path: str, st: StatInfo, is_dir: bool, facts: dict) -> Refinement | None
class DetectorContext: bases: Bases; mac: MacOS; settings: Settings; rules: tuple[Rule, ...]   # rules naming this detector
```

Registered detectors: `claude_versions`, `desktop_engines`, `claude_plugins`, `codex_dupes`, `codex_state_tmp`, `editor_extensions`, `newest_dir`, `electron_apps` (names the installed app that owns an Electron cache folder, so the executor can ask for it to be quit), `orphans` (Application Support folders named after a bundle id no installed app carries) and `project_deps` (dependency folders of idle projects). `codex_bloat.diagnose` is not registered; `App.diagnose_session` calls it on demand.

`facts = {detector_name: prepare(...)}` for the detectors some catalog rule names, and `facts_hash = sha256(canonical JSON(facts))`. A detector must be deterministic for the same disk state.

**Classifier.** `Classifier(catalog, facts, bases, settings)`:

- `privacy_skipped(path) -> bool`: never `scandir`, `stat` or open these paths.
- `classify_dir(path, st, root_domain, in_git_tree=) -> Match | None`: the bundle rule that claims this directory. The scanner then calls `fsutil.walk_bundle` and stores one item; `None` means descend.
- `classify(path, st, is_dir, root_domain, in_git_tree=, db_dir=) -> Match`:
  - Rules are tried in priority order. A rule matches on pattern, `name_include`/`name_exclude`, `min_size` and unit.
  - The detector `refine` may skip, override the tier or group, or add evidence.
  - A match with a REVIEW tier on a regular file reads its first 16 bytes; a SQLite header or an unreadable header makes it protected.
  - In a git work tree (a `.git` entry in the directory or above) every file is protected and only protections and dependency folders may claim a directory. A file in a directory that holds a database marker (`*.sqlite*`, `*.db`, LevelDB's `CURRENT` plus `MANIFEST-*`, ...) is protected unless a hand-written rule of priority 900 or more claims it.
  - With no match the result is `Match(rule_id='unknown', tier=PROTECTED, domain=root.domain, kind=UNKNOWN, group_key='unknown:<first 3 segments>')`. Under `~/Library/{Application Support,Caches,Logs}` the key keeps four segments.
- `file_context(path) -> (in_git_tree, db_dir)` works out the same two flags from disk, for code that classifies one path later (the executor).
- Group key: `rule.id` when `group_depth` is None, otherwise `f'{rule.id}:{first group_depth logical segments}'`. Refinements may override it. A protected variant of an unprotected rule gets the marker `#protected` after the rule id, so a group never mixes protected and selectable items.
- **Taint:** a bundle whose `walk_bundle` returns a non-empty `taint` is stored with tier PROTECTED and evidence `evidence.tainted`, except when its rule is report-only (a module cache full of file names that look like credentials is only listed). Taints, in reporting order: mount, foreign_owner, immutable, dataless, git, database, credential, unreadable. Name taints (database, credential) are skipped for the kinds `old-version`, `update-leftover`, `project-deps` and `runtime`.

**Scanner.** `scanner.scan(store, classifier, catalog, bases, mac, settings, domains, progress, cancel, prune=True) -> scan_id`:

- Walks the union of enabled roots once. A root inside another enabled root is covered by the outer walk. It uses `os.scandir`, never follows symlinks, and counts a hard-linked file once by `(dev, ino)`.
- A root that is a symlink, or reached through one, is an error (`root_symlink`).
- Never crosses into another `st_dev`; such directories are recorded as `other_volume`.
- Before descending into a directory it checks, in order: privacy skip, then bundle rule, then descend.
- Items are written to the store in batches of `BATCH_ROWS` (5,000) while the walk runs, and group totals are accumulated in memory. Only items of rules with the `LAST_USED` basis wait for the end of the walk, because they need the batched `mac.last_used` call first.
- Protected and unknown files under 50 MiB are rolled up into one item per group and base, with unit BUNDLE, path equal to the longest common folder and `files = count`. Larger ones stay individual. This keeps the row count bounded and still lets the user see big protected files.
- It writes `scan_roots` statuses: `ok`, `skipped_privacy`, `missing` or `error`, and per-path `scan_errors` (`skipped_privacy`, `permission_denied`, `other_volume`, `undecodable_name`, `vanished`, `root_symlink`, `not_a_directory`, `dataless_skipped`).
- Calls `progress(event: dict)` at most every 0.25 s. When `cancel()` returns true it raises `StewardError(IN_PROGRESS, 'error.scan_cancelled')`, and a cancelled or failed scan discards the items it already streamed in.

## Store (`store.py`)

SQLite in WAL mode with `busy_timeout=5000`, `synchronous=NORMAL` and one connection per thread. `meta.schema_version` drives migrations (currently 3). New files are created with `auto_vacuum=INCREMENTAL`. Writes that must be on the disk before a source file is unlinked (`add_archive`, `set_archive_state`) go through `_durable()`, which switches to `synchronous=FULL` and `fullfsync=ON` for that statement. A lost claim or outcome elsewhere is re-derived from the disk by recovery.

- **Retention.** Items, groups, scan roots and scan errors are kept for the latest done scan only. `finish_scan(..., 'done')` drops every other scan's rows, garbage-collects unused `dirs` and `evidence`, then runs `incremental_vacuum` and a WAL truncate. The `scans` rows, `group_history`, `scan_totals`, plans, archives, advice and specs are never pruned; the entries of a finished plan are compacted after a week (see Plan retention). A plan entry carries its path and fingerprint, so a plan runs without its scan's rows (the executor reads that scan's row for extra evidence only if it still exists); report growth reads `group_history`. `finish_scan(..., prune=False)` leaves the rows for the caller to drop with `prune_scans()` (`App.scan` does that after it migrated the selections, because the migration notes compare against the old scan). A cancelled or failed scan removes the items it already streamed in (`discard_items`).
- **Item rows are compact.** An item stores its directory (`dirs.id`) and file name, its group (`groups.id`) and its evidence (`evidence.id`, deduplicated). Its rule, tier, domain, kind and unit are NULL while they equal the group's `i_*` values. A `groups` row exists from the first item added to it (`provider IS NULL` until `add_groups` fills in the rest and the row becomes visible to `groups()`), so a scan streams items in batches before the totals are known. `Store` and `queries` return the same `Item` objects and light rows.
- **Schema 2** replaced the scan tables of schema 1 with the compact ones above. Opening a schema 1 file drops them (the next scan rebuilds them), keeps everything else and runs one `VACUUM`.
- **Schema 3** adds `scan_totals(scan_id, tier, domain, report_only, alloc, reclaim, items, backfilled)` with primary key `(scan_id, tier, domain, report_only)`, and the partial covering index `plan_entries_done` (done entries only), which lets the cleanup history sum 10^5 entries in tens of milliseconds. `finish_scan(..., 'done')` writes the totals from the scan's groups in the same transaction as `group_history`; `report_only` means no action and not protected, the same split the report makes. Nothing prunes the table. The migration backfills every done scan: one whose `groups` rows still exist (the latest) is totalled exactly (`backfilled = 0`); an older one is rebuilt from `group_history` with `group_key_class(key, catalog)` (`backfilled = 1`, `reclaim` 0). A key whose rule is `prot.<x>:<area>` carries its area; a `#protected` variant or a rule the catalog no longer has is protected; an unclaimed path takes the area of the outermost scan root that contains it, else app-library. History is therefore approximate for scans before schema 3, and the history rows say so (`backfilled`).
- **Plan retention.** When a run ends, `save_run_summary` stores a complete summary as `meta` `run:<plan_id>`: the executor's figures plus `finished_at`, `items_done|skipped|failed|excluded|needs_attention|cancelled`, `removed`, `by_group` (`{group_key: {done, alloc, freed, actions}}`) and `skipped_reasons`. A plan that finished without one (an older version) gets one built from its entries and marked `synthesized`; `App.plan_result` does not return it, because it has no free-space figures. Seven days after a plan finished (`service.COMPACT_AFTER_DAYS`), `App.compact_plans` deletes its `done` and `cancelled` entries, keeps the `skipped`, `failed`, `needs_attention` and `excluded` ones (few, and they explain outcomes) and sets `meta` `compacted:<plan_id>`. `compact_plan` refuses a plan that is not done, cancelled or failed, and one whose summary is incomplete. Draft, running and paused plans are never touched. After compaction `App.plan` counts the removed rows from the summary and adds `compacted_at`, `plan_groups` merges `by_group`, and the cleanup history reads the plan from its summary, so every figure is the same before and after. The audit log keeps every removed path, and the text `plan.compacted_note` (core) says so. The pass runs when an `App` starts (a failure raises) and after every scan and run (a failure is published as `retention.failed` and audited, because the scan or run already happened). Every deletion batch (compaction, a discarded or superseded draft, the prune after a scan) ends with `incremental_vacuum` and a WAL truncate, and an `App` start does the same when free pages are more than 10 percent of the file.
- **`spec_include_fp(session, item_key, fp, path)`** holds the fingerprint an explicit include had when it was saved. `save_spec` keeps it in step with `spec.include`. `migrate_spec` keeps an include only if the new scan's item has that fingerprint; an include with no row counts as changed.
- `meta` also holds `spec_notes:<session>` (what the last carry-over of a selection dropped), `advisor.export` (the latest manual advisor export), `run:<plan_id>`, `compacted:<plan_id>` and `native:<epoch ms>` (one per native command run).

```python
Store(path: Path, clock=time.time)
begin_scan(catalog_hash, facts_hash, settings_hash, domains) -> scan_id
add_items(scan_id, items: Iterable[Item]); add_groups(scan_id, groups); add_scan_root(...); add_scan_error(...)
finish_scan(scan_id, status, vol_total, vol_free, attributed, error='', prune=True); prune_scans(); discard_items(scan_id)
latest_scan(status='done') -> dict | None; scan(scan_id) -> dict; scan_roots(scan_id); scan_errors(scan_id)
groups(scan_id, domain=None, tier=None) -> list[Group]; group(scan_id, key) -> Group
items(scan_id, group_key=None, keys=None, order='alloc', offset=0, limit=None, query='') -> list[Item]; count_items(...)
iter_items(scan_id) -> Iterator[Item]; item(scan_id, key) -> Item
group_history(key, limit=20) -> list[(scan_id, finished_at, alloc, items)]
meta(key); set_meta(key, value); delete_meta(key)
read_snapshot()   # context manager: reads on this thread see one moment of the database, even if a scan prunes meanwhile
save_spec(session, spec); load_spec(session) -> Spec | None; include_fingerprints(session, keys) -> {key: (path, fp)}
save_plan(plan, entries); plan(plan_id) -> Plan; entries(plan_id, state=None, offset=0, limit=None) -> list[Entry]; count_entries(...)
plans_in_state(state); entries_in_state(state)
run_summary(plan_id) -> dict | None; save_run_summary(plan_id, summary=None) -> dict   # completes and stores meta run:<plan_id>
finished_plan_ids() -> list; compacted_ids() -> list; compacted_at(plan_id) -> float | None; compact_plan(plan_id, when) -> int
shrink_if_fragmented() -> bool         # incremental_vacuum + WAL truncate when free pages exceed 10 percent
set_plan_state(plan_id, state, error=''); claim_entry(plan_id, seq) -> bool   # atomic pending -> in_progress
finish_entry(plan_id, seq, outcome: Outcome); exclude(plan_id, seqs, revision) -> new_revision
add_archive(row); archives(); archive(id); set_archive_state(id, state)
add_advice(row); latest_advice(scan_id)
```

## Selection (`selection.py`): the single eligibility function

```python
decide(item, group, rule, spec, now, enabled_domains, trash_available) -> Decision
resolve(store, spec, now, settings, mac) -> Resolution
  Resolution: decisions: dict[item_key, Decision]; summary: dict; digest: str; now: float
  summary = {count, alloc, reclaim, total, by_action{action:{count, alloc, reclaim, frees, ...}},
             by_state{state:{count, alloc}}, scope_by_state (by_state limited to selected groups and explicit includes),
             groups{gkey:{total, selected, alloc, reclaim, too_recent, too_recent_alloc, floor_held, floor_held_alloc,
                          excluded, protected}},
             warnings[{key, ...}]}
  digest = sha256(canonical JSON of [scan_id, spec.version, sorted [item_key, action] of selected items])
apply_ops(spec, ops, store, enabled_domains=None) -> Spec   # pure; bumps version once; validates keys against the scan
migrate_spec(store, spec, scan_id, now=, enabled_domains=, trash_available=, session=) -> (Spec, notes)
```

`decide`, first matching step wins:

1. Tier PROTECTED or taint gives `protected`.
2. Domain not enabled gives `domain_off`.
3. A file unit with `nlink > 1` gives `no_gain`.
4. Key in `spec.exclude` gives `excluded`.
5. Neither an explicit include nor a group selector gives `unselected`.
6. The action is taken from the include override, then the group selector, then the rule default. If it is not in `rule.actions`, gives `unsupported_action` (reason `report_only` when the rule has no actions at all). That includes `trash` when `/usr/bin/trash` is missing.
7. Age is `now - newest_ns` (for the LAST_USED basis, `now - max(last_used_ns, newest_ns)`). Below `rule.min_age_floor_days` gives `too_recent_floor`.
8. An explicit include gives `selected(via='explicit')`, whether or not its group is selected ("include anyway": the filter yields, the floor in step 7 does not). Toggling an item on is an explicit include, except when it undoes the user's own exclusion inside a selected group.
9. The threshold is `max(floor, sel.min_age_days)` when the group has its own override, otherwise `max(floor, group.default_min_age_days or 0, spec.filter_min_age_days)`. A rule default can only make the user's filter stricter. Age below it gives `too_recent`; otherwise `selected(via='group')`.

`resolve` passes light rows and the group's copies of the rule fields (actions, default action, floor, age basis), so it needs no catalog. Its warnings are `warning.codex_both_copies` (a duplicate pair and its kept copy are both selected) and `warning.group_selected_none_qualify`.

`apply_ops` op shapes (JSON):

- `{op:'set_filter', min_age_days}`
- `{op:'select_group', group, on, action?, min_age_days?}`
- `{op:'select_tier', tier:'junk', domain?, on}`: every group of that tier in enabled domains that has an action
- `{op:'toggle_item', item, on, action?}`:
  - on is refused for protected, tainted, domain-off and report-only items (`error.not_selectable`);
  - off on a group-selected item adds it to `exclude`;
  - on removes it from `exclude` (handing it back to the group's filter), or, in any other case, adds it to `include`;
  - off on an explicit include removes it from `include`.
- `{op:'set_action', group|item, action}`
- `{op:'clear'}`

`migrate_spec` carries a selection to a newer scan: group selectors stay when the group still exists, exclusions always stay, and an explicit include stays only when the item's fingerprint equals the one recorded when the user picked it. The notes (`dropped_groups`, `include_changed`, `include_missing`, `kept_excludes`, `group_changes`) are stored in `meta` until the user next edits the selection.

## Planner (`planner.py`)

```python
create_plan(store, spec, now, expected, settings, mac, *, catalog=None, notes=None) -> Plan
  # expected = {'spec_version', 'digest', 'scan_id', optional 'resolved_at'}
  # refusals, in order: INVALID error.spec_stale (the spec or the client's view belongs to a scan other than the latest),
  #   RULES_CHANGED (catalog or settings hash differs from the scan's), DIGEST_MISMATCH (added/removed lists, 50 each),
  #   EMPTY_PLAN (carries the per-state breakdown)
  # entries ordered by group, then alloc desc; expires_at = now + 24h; revision = 1
  # entries_digest = sha256 of canonical JSON [[seq, item_key, action, fp]] over ALL entries; fixed at creation.
  # Exclusions never change it; they bump `revision`. run() needs both, so a client acting on a stale view fails.
validate_for_run(plan, current_hashes, now, revision, entries_digest) -> None
  # raises PLAN_EXPIRED / RULES_CHANGED / REVISION_MISMATCH. Only the catalog and settings hashes are compared.
  # Facts drift while apps run and a plan's own removals change them, so they are applied per entry instead
  # (re-classification, step 7 below); preflight reports the entries that would be kept as `rules_changed`.
```

## Executor (`executor.py`, `actions.py`, `guards.py`)

```python
Executor(store, catalog, classifier, bases, mac, audit, clock, settings, data_dir, hooks=None, lock=None)
  .run(plan_id, revision, entries_digest, control, on_event=None) -> summary
  .preflight(plan_id) -> dict      # what a run would meet now; changes nothing
  .recover() -> dict               # settle what a crashed run left behind
```

`classifier` may be None. `hooks` are fault-injection seams named in `HOOK_NAMES` (`after_claim`, `after_parent`, `after_lstat`, `after_fingerprint`, `after_classify`, `after_guard`, `before_unlink`, `archive_chunk`); only tests pass them.

**Pre-flight** (whole plan), in order:
1. Plan state is `draft` or `paused` (`IN_PROGRESS` if running, `INVALID` otherwise).
2. `validate_for_run` passes.
3. The operation lock is held.
4. The staging directory `data_dir/staging/<plan_id>` exists (mode 0700, owned by us) on the same `st_dev` as each base (`~`, `$TMPDIR`, `$CACHEDIR`) the plan touches, and so does `data_dir/archives` when the plan archives.
5. Archive free space is at least 1.2 × the summed alloc of archive entries + 1 GiB.
6. `/usr/bin/trash` exists if any entry trashes.
7. Guard snapshots are taken, and Spotlight last-used dates are read for entries of LAST_USED rules.

**Per entry**, in order. The first failure gives `Outcome(SKIPPED, reason)` and moves on to the next entry:
1. `control.checkpoint()` (pause or cancel), then: the lock is still held (`LockLost` stops the run) and the classifier's facts are no older than 15 s, or they are recomputed.
2. `store.claim_entry` returns False, meaning the entry was excluded: skip without an outcome.
3. The catalog still has the entry's rule (`skip.rules_changed`).
4. Open the parent chain from `/` with `O_NOFOLLOW|O_DIRECTORY`. Every ancestor below the base must be owned by us or by root (`skip.unsafe_parent`; `skip.vanished` for ENOENT).
5. `lstat` through the parent fd: a regular file for a file entry, a directory for a bundle (`skip.changed`), our uid (`skip.foreign_owner`), the same device as the base and the staging folder (`skip.other_volume`), not `SF_DATALESS` (`skip.dataless`), none of `UF_IMMUTABLE|SF_IMMUTABLE|UF_APPEND|SF_APPEND` (`skip.locked`).
6. The fingerprint matches `entry.fp` (`skip.changed`). For a file that means dev/ino/size/mtime_ns/birth_ns and `nlink == 1`. For a bundle it means the directory's dev/ino here; the walk-based part (files, alloc, newest_ns, no taint) is checked after staging.
7. Re-classifying with the current catalog and facts gives the same `rule_id`, a tier other than PROTECTED, and the action is still allowed (`skip.rules_changed`).
8. Age is still at least `entry.threshold_days`, computed against the current time; for LAST_USED rules the newest of the changed time, the scan's Spotlight date and a fresh one (`skip.too_recent`, `skip.last_used_unknown`).
9. Guard: no `rule.quit_apps` app (and no app named by the fresh classification's `evidence.requires_quit`) is running and no file of the entry is executing or open (`skip.blocked:<reason>`).
10. The audit intent line, then the action:
   - **Delete:** `rename_excl` into staging, then re-verify (file: the staged inode equals the fingerprint; bundle: `walk_bundle` gives the same files, alloc, newest_ns and no taint). Then unlink, or `shutil.rmtree`. On mismatch, `rename_excl` it back, and if that fails mark it `NEEDS_ATTENTION`.
   - **Archive:** a file streams through sha256 into `archives/<id>.part` (gzip), a bundle is staged and verified first and then streamed as tar.gz. Then fsync, re-stat the source, decompress and verify, rename to final, write the sidecar `<id>.json` and the database row, then the delete path.
   - **Trash:** the identity (and, for a bundle, the walk) is checked once more, then `mac.trash(abs)`.
11. `store.finish_entry` and an audit outcome line.

**Control.** `Control` has `pause()`, `resume()`, `cancel()` and `checkpoint()`.
- Pause takes effect at entry boundaries and inside archive streaming every 8 MiB. Pausing or cancelling inside an archive rolls it back: the `.part` file is deleted, the source is never touched, and the entry returns to `pending`.
- A resume re-validates the plan (expiry, and the catalog and settings hashes, with the settings file read again); a failure leaves the plan paused with the error.
- Cancel marks the remaining entries `cancelled`.
- `exclude()` works on pending entries at any time and bumps the revision. A running executor doesn't need the new revision: the claim check already enforces exclusions.

**Recovery.** `recover()` runs when an `App` starts, unless another process holds the operation lock (its `in_progress` entries are alive). For each `in_progress` entry it works out how far the run got from where the item is:
- stale `.part` files are deleted; an archive file with no database row is registered when its sidecar and content verify, and deleted otherwise;
- an item that is still at its original path was never staged: the entry goes back to `pending` and an archive made on the way is discarded;
- an item in staging that matches its fingerprint is finished when its removal was already authorised (a delete, or a verified archive) and put back otherwise; a bundle that no longer matches may be half removed, so it is finished when authorised and flagged `NEEDS_ATTENTION` when not, never put back;
- an item gone from both places is `done` (`recovered.gone`), or `NEEDS_ATTENTION` when its archive no longer verifies;
- every affected plan becomes `paused` with the error `interrupted`.

**Restore.** `restore(archive_id)` checks the archive, creates missing parents (0700), and refuses when the destination exists. A file is decompressed into a temporary sibling opened with `O_EXCL`, its sha256 and length are verified, mode and mtime are restored, and it is linked into place (`link` never replaces). A bundle is extracted through directory fds with `O_NOFOLLOW`, refusing absolute and `..` members, links that leave the bundle, devices, hard links and repeated paths. `purge_archive` deletes the archive files and marks the row.

## Service facade (`service.py`)

```python
App(home=None, data_dir=None, mac=None, clock=time.time, locale=None, settings=None, bases=None, hooks=None)
  settings / update_settings(patch) -> Settings      # RUN_SAFE_SETTINGS only while a run is active
  scan(domains=None, progress=None, cancel=None) -> scan_id          # blocking, takes the operation lock, rescan-safe
  start_scan(domains=None) -> job; start_run(plan_id, revision, entries_digest) -> job   # background threads
  job() -> dict      # status idle|running|paused|done|error, kind None|'scan'|'run'|'native', phase, plan_id, counts, scan, current, summary, ...; a native job adds rule_id, target, command, started_at, output (last 40 lines), output_lines, native (the result)
  overview() -> dict; app_sizes() -> dict      # totals, growth, not-included breakdown, volume accounting; du over /Applications
  groups(domain=None, tier=None, session) -> list[dict]    # each with the group's selection summary
  items(group=None, order='alloc', offset=0, limit=100, query='', session, keys=None) -> {'total', 'rows':[item + decision]}
  spec(session) -> Spec; selection(session) -> dict; spec_notes(session) -> dict | None
  apply_ops(ops, session) -> {'spec', 'scan_id', 'summary', 'digest', 'resolved_at'}
  create_plan(session, expected) -> dict;  plan(plan_id) -> dict;  entries(plan_id, state=None, offset=0, limit=100)
  pending_totals(plan_id) -> dict      # what a run would still handle; plan()['totals'] stays as created
  exclude(plan_id, seqs, revision) -> int
  preflight(plan_id) -> dict; validate_run(plan_id, revision, entries_digest) -> None   # raises what run would raise
  run_plan(plan_id, revision, entries_digest, on_event=None) -> dict   # blocking
  pause() / resume() / cancel()
  snapshots() -> list[str]      # local Time Machine snapshots
  insights(window_days=30, locale=None) -> dict      # the Insights page; see below
  native_tools(locale=None) -> list[dict]            # report-only groups with their command and whether it can run
  start_native(rule_id, target, command) -> job; run_native(rule_id, target, command, on_event=None) -> result; check_native(...)
  compact_plans() -> int        # plan retention; runs at start and after every scan and run
  plan_result(plan_id) -> dict | None   # the run summary of a finished plan; plan(plan_id) adds compacted_at
  archives() / restore(archive_id) / purge_archive(archive_id)
  diagnose_session(item_key) -> dict     # Codex bloat counts only
  advisor -> advisor.AdvisorService      # built on first use
  events: EventBus  (version counter + the last 500 events; wait(since, timeout) for long-poll)
  recovery: dict | None                  # what recover() settled at start
```

`groups()`, `items()` and plan creation read inside `Store.read_snapshot()`, so a page, its decisions and its total come from one state of the database. If a scan finished after the selection was read, they raise `INVALID error.spec_stale` instead of returning rows that may be gone.

Scans and runs share one background slot: while either is active, a second `start_*` raises `BUSY`. A completed run rescans the areas of its plan's scan, carries the selections over to the new scan and then prunes the old rows. Selections are keyed by session name (`default` for the portal, `cli` for the CLI); the CLI rebuilds its selection on every `plan` and `list`.

Settings outside `settings_hash()` (`RUN_SAFE_SETTINGS`: `locale`, `advisor_enabled`, `advisor_provider`, `default_min_age_days`) can change during a run; any other setting raises `BUSY` while a run is active or paused.

Events published on the bus: `scan.progress`, `scan.done`, `scan.failed`, `recover.done`, `recover.skipped`, `run.started`, `run.progress`, `run.paused`, `run.resumed`, `run.finished`, `entry.done`, `archive.restored`, `archive.purged`, `advisor.started`, `advisor.done`, `advisor.failed`, `retention.failed`, `native.output`, `native.done`, `native.failed`.

## Insights (`insights.py`)

`App.insights(window_days=30, locale=None)` builds the data of the Insights page; `GET /api/insights` serves it and `disk-steward insights [--window DAYS] [--json]` prints it. Texts are already in the locale, sizes are bytes, times are epoch seconds. It reads what scans and runs stored, plus the volume, `tmutil`, `lsappinfo` and the versions of `codex`, `claude` and `ollama` (cached for ten minutes per `MacOS` adapter). It walks no disk, saves nothing (not even the "safe to clean" selection), and never measures `/Applications`: `largest_apps` is present only after `app_sizes()` ran. It works without a scan.

```python
insights.build(app_or_parts, window_days, locale, now) -> {
  generated_at, window_days,
  environment: {macos, arch, python, tool_version, volume: {total, used, free}, snapshots: int | None (+ snapshots_error),
                data_dir, data_dir_bytes, clis: {codex, claude, ollama}, clis_errors, advisor_enabled,
                schedule: {installed, weekday, hour}, areas, last_scan: {id, finished_at, duration_s, items, privacy_skipped, errors} | None},
  kpis: {free: {value, delta, series}, scanned: {value, delta, series}, freed_total: {value, count, since},
         safe_to_clean: {bytes, items, days}, growth_per_day},           # series: [{t, v}], one point per done scan in the window
  composition: [{key, bytes, label}],     # free, outside, protected, review, report_only, junk: adds up to the volume
  history: {scans: [{id, t, used, free, scanned, junk, review, protected, report_only, backfilled}], cleanups: [...]},
  growth: {up: [top 8], down: [top 5]},   # {group, label, domain, tier, delta, per_day, now, before}, first scan in the window to the latest
  agents: [{provider, label, bytes, delta}],
  todos: [{id, severity, title, detail, bytes, action: {label, href} | None, command}],   # at most 12
  largest_apps: [{path, bytes}]           # only after app_sizes()
}
```

Free space and `used` come live from the volume; the other bars come from the latest scan's `scan_totals`. If a scan attributed more than is in use (APFS clones count twice) the four scanned bars are scaled down together so the sum still equals the volume. `delta` is the value now minus the first point inside the window, and `per_day` divides by the span in days, never less than one, so a rescan minutes after a scan cannot extrapolate. A cleanup row has `kind` (`plan`, or `native` for a tool run, with `plan_id` null, `rule_id`, `target`, `command`, `exit_code`, `before`, `after`, `items_*` null and `freed` what the tool freed; `kpis.freed_total` adds those) and, for a plan, `plan_id`, `created_at`, `finished_at`, `items_done|skipped|failed|excluded`, `removed` (every done entry, Trash moves included), `freed`, `moved_to_trash`, `measured_delta` and `estimated` (from the run summary, `None` without one); see Plan retention for plans whose entries are gone.

TODOs come from fixed rules, each linking to the page that shows the same number: junk that qualifies (`#/overview?quick=1`); a running app that holds back part of that selection; review groups of 500 MiB or more (`#/browse?group=KEY`, top 3); report-only groups of 200 MiB or more with a command (`#/browse?report_only=1`, top 3, with `command` and `runnable`, whether its tool's own command can run here); groups that grew 1 GiB, or 200 MiB a day, in the window (top 3); Codex legacy-ID copies; a last scan older than 7 days, or none; the weekly scan not installed; archives older than 30 days; local snapshots holding back more than half of the last cleanup. Order is act, warn, review, tip, then size. `quick_totals` in `queries.py` produces the first two rules' numbers in one SQL statement, ten times faster than `selection.resolve_for` on 10^5 items. It mirrors `selection.decide()` for the "every junk group" spec, with the same float arithmetic, and `tests/test_insights_queries.py` compares the two on random stores: change `decide()` and that test fails until the SQL follows.

## Native commands (`native.py`)

Report-only groups (shared developer caches, Ollama models) are never deleted by Disk Steward. For the tools that have an official cleanup command, the service can start that command, after the user confirmed its exact text. `App.native_tools(locale)` lists the report-only groups of the latest scan that hold data, biggest first: `{rule_id, group, label, bytes, items, command, runnable, reason, tool, tool_version, targets}`. `command` is the text a run must be given back; `reason` (translated) says why a row cannot run; `tool` is what `shutil.which` found; `tool_version` is cached for ten minutes; `targets` is `[{name, size, bytes, command}]` for Ollama (the models `ollama list` shows) and `None` otherwise.

The registry is a dict in `native.py`, rule id to argv list. It is never built from a request, a scan or an advisor reply:

| Rule | argv |
|---|---|
| `dev.homebrew` | `brew cleanup --prune=all` |
| `dev.pip` | `pip3 cache purge` |
| `dev.go-build`, `dev.go-mod` | `go clean -cache`, `go clean -modcache` |
| `dev.pnpm`, `dev.uv`, `dev.npm`, `dev.yarn` | `pnpm store prune`, `uv cache prune`, `npm cache clean --force`, `yarn cache clean` |
| `dev.colima` | `colima prune --force` |
| `dev.coresimulator` | `xcrun simctl delete unavailable`, runnable only when `xcrun simctl help` exits 0 (else the reason names `sudo xcode-select -s /Applications/Xcode.app`) |
| `ollama.models` | `ollama rm NAME` per target; NAME must equal a name `ollama list` shows now |

`dev.playwright` (`npx` may download code) and `dev.npx` (a plain `rm -rf`) are copy-only with a reason. Rules with no official command (gradle, maven, cargo, Xcode derived data and device support, puppeteer, node-gyp, puccinialin) have `command` null and the reason "no official cleanup command".

`start_native(rule_id, target, command)` refuses, in order: an unknown or non-report-only rule (`INVALID error.native_unknown`), a tool that cannot run (`UNAVAILABLE error.native_not_runnable`, details `reason_key`, `reason_args`, `reason`), a bad or missing Ollama target (`error.native_bad_target`, `error.native_target_required`), a `command` that is not the display string it derives (`error.native_command_changed`, details `expected`), and a busy slot or operation lock (`BUSY`). The tool is looked up with `shutil.which` at that moment. The job then runs on the same slot as a scan or a plan run (job kind `native`; `cancel()` stops it, pause is refused):

1. Audit `native.intent` (rule, target, command, argv with the resolved tool path). Measure the tool's folders: `walk_bundle` over every existing match of the rule's patterns (`before`).
2. Run the argv with no shell, `cwd` a fresh empty temporary folder (removed afterwards), the user's environment, stdin closed, `start_new_session=True`. Output (stdout and stderr together) is split on `\n`, `\r` or `\r\n`, empty lines dropped, lines cut at 4,096 characters, and published as `native.output {line}` for the first 2,000 lines; `job()['output']` holds the last 40 and `output_lines` the count. After 2 MiB the output is read and dropped. After 15 minutes, or on cancel, the process group gets SIGTERM, then SIGKILL after 5 seconds.
3. Measure again (`after`); `freed = max(before - after, 0)`. Audit `native.outcome`. Save meta `native:<epoch ms>` (`rule_id, target, command, status, exit_code, before, after, freed, started_at, finished_at`). Publish `native.done {rule_id, target, exit_code, before, after, freed, output_tail, output_truncated}`, or `native.failed {code, message_key, details}` for a timeout, a cancel or a tool that could not be started. A non-zero exit code is a `native.done` with that code.
4. Release the operation lock, then rescan the current areas (when a scan exists), so every number follows the tool. The job ends `done` (also for a non-zero exit), `error` (timeout, could not start) or `idle` (cancelled); `job()['native']` keeps the result: `{rule_id, target, command, status (ok | failed | timeout | cancelled | error), exit_code, before, after, freed, started_at, finished_at, output_truncated, output_tail, error, rescan}`.

## CLI (`cli/`)

`cli.main(argv=None, app_factory=None) -> int`. Commands: `doctor`, `scan`, `report`, `list`, `plan`, `show`, `apply`, `archives`, `diagnose-session`, `schedule`, `advise`, `portal`, `insights`, `tools`. `--data-dir` and `--lang` work before or after the command. Exit codes: 0 ok, 1 scan problems or a cancelled `apply`, 2 an expected error (`StewardError`), 3 another operation holds the lock. `apply` asks for the entry count unless `--yes`, and runs the plan in a thread so Ctrl-C can pause it. `doctor` runs before any `App` exists and reports the default data directory, ignoring `--data-dir`.

## Portal

- Bound to `127.0.0.1` on a random port (`--port` to fix it).
- `disk-steward portal` prints `http://127.0.0.1:<port>/launch?code=<one-time>`. `/launch` serves the same page as `/`. Its script trades the code (valid for 120 s, spent on use) for a session token with `POST /api/session`, keeps the token in `localStorage` under a key that includes the port, takes the code out of the address bar, and sends the token in the header `X-Disk-Steward-Token` on every call (`credentials: 'omit'`). There is no cookie, because a cookie set for 127.0.0.1 goes to every server on that address whatever its port; a leftover `ds_session` cookie from an earlier version is cleared. Tokens live in memory (at most 64), so a restart invalidates them: the page then gets a 401 and shows the locked page.
- `--url` prints a fresh code from a running portal: it reads `portal.json` (port, pid, launch secret; mode 0600) from the data dir and POSTs to `/internal/launch-code` with the secret in `X-Launch-Secret`. A request that names an `Origin` is refused there even with the secret. The CLI prints the answer only if it is exactly `http://127.0.0.1:<port>/launch?code=<urlsafe>` for that port, and reads at most 64 KiB of it. A second portal on the same data dir refuses to start.
- Every request checks Host (`127.0.0.1:<port>` or `localhost:<port>`, else 421) and, for `/api/`, the token header (else 401). A POST or PUT (including `/api/session`) also needs `Origin` equal to `http://<host>`, the header `X-Disk-Steward: 1`, `Content-Type: application/json` and a body of at most 1 MiB that is a JSON object. Any other method on `/api/` is 405. At most 32 requests are handled at once, long polls included; the next one gets a 503.
- Errors return `{error:{code, message_key, message, details}}` with status 400/404/409/422/423/503 for a `StewardError` (see `portal/api.py` `STATUS`), and 401/403/405/411/413/415/421/500/503 for the request itself.
- CSP: `default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'`, plus `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`. API responses are `Cache-Control: no-store`.
- Static files are read with `pkgutil.get_data`, so they work from a source tree, a wheel and a zipapp. Only plain path segments and the extensions `.html .css .mjs .js .svg` are served.

**API.** All routes need the token header except `/`, `/launch`, `/static/*`, `POST /api/session` and `/internal/launch-code` (which takes the launch secret instead).

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/session` | Trade a launch code `{code}` for a session token (403 for an unknown, used or expired code) |
| GET | `/api/state` | version, locale, job, settings, latest scan, recovery |
| GET | `/api/events?since=&timeout=` | Long poll; `timeout` 0 to 25 s, anything else is a 400; `reset: true` when the client's counter is stale or the log dropped events |
| POST | `/api/scan` (or `/api/scan/start`) | Start a background scan (`{domains?}`) |
| GET | `/api/job` | The job |
| POST | `/api/job/pause\|resume\|cancel` | Job control (a scan or a native command cannot be paused; cancel stops either) |
| GET | `/api/overview` | Overview data |
| GET | `/api/apps` | Sizes of the applications (`du`, cached per `App`) |
| GET | `/api/native` | `{tools: [...]}`: the report-only groups of the latest scan with their native command (see Native commands) |
| POST | `/api/native/run` | `{rule_id, target?, command}` starts a native command as a job (`command` must be the text the row showed; 400, 503 or 423 as specified) |
| GET | `/api/insights?window=` | The Insights page; `window` is 0 (all history), 7, 30 or 90 days, default 30, anything else is a 400 |
| GET | `/api/groups?domain=&tier=` | Groups with the session's selection summary |
| GET | `/api/items?group=&order=&offset=&limit=&q=` | Items with decisions |
| GET | `/api/spec` | The selection, summary and digest |
| GET | `/api/spec/notes` | What the last scan carry-over dropped |
| POST | `/api/spec/ops` | Apply selection ops |
| POST | `/api/plans` | Create a plan from `{expected}` (409 or 422 as specified) |
| GET | `/api/plans/<id>` | Plan detail with `pending` totals |
| GET | `/api/plans/<id>/entries?state=&offset=&limit=` | Plan entries |
| POST | `/api/plans/<id>/exclude` | Exclude pending entries (`{seqs, revision}`) |
| GET | `/api/preflight/<id>` | What a run would meet now |
| POST | `/api/plans/<id>/run` | Run a plan (`{revision, entries_digest}`); refusals come back as HTTP errors |
| GET | `/api/archives` | Archive list |
| POST | `/api/archives/<id>/restore\|purge` | Restore or purge an archive |
| GET | `/api/advisor?probe=0` | Advisor status and the latest advice. `probe=0` runs no CLI (cached answers only); with the advisor off no CLI runs at all |
| GET | `/api/advisor/last` | The newest advice, for the badges in Browse; never touches a CLI |
| POST | `/api/advisor/enable\|preview\|run\|export\|import` | Advisor actions |
| GET/PUT | `/api/settings` | Settings (`live_fields` lists those a run does not lock) |
| GET | `/api/i18n/<locale>` | Translations |

## i18n

- Catalogs live in `i18n/<locale>/<namespace>.json` (`core`, `rules`, `cli`, `portal`, `advisor`, `insights`, `native`) and are merged at load; a key defined in two namespaces is an error. The namespace list is fixed in code, so loading is the same for a source tree, a wheel and a zipapp.
- `en` is the base; `zh-CN` must have exactly the same keys, enforced by a parity test.
- `detect_locale(env_lang, accept_language)` returns `zh-CN` for any `zh*`, otherwise `en`. Accept-Language wins over the environment.
- Every catalog rule needs `rule.<id>.label` and `rule.<id>.effect`; `docs/catalog.md` prints both.

## Tests

- `tests/support.py` provides:
  - `FakeHome`, which builds trees in a temp dir and sets ages with `os.utime`; it also sets birthtime by creating the file first and checks the APFS behaviour;
  - `FakeMacOS`, a fixed `Clock`;
  - `real_home_guard()`, which fails a test if the real HOME is touched.
- Every test uses a temp HOME and a temp data dir.
- `python -m unittest discover -s tests -v` must pass on `/usr/bin/python3` (3.9) and on a current Python.
- `tests/test_docs.py` fails when `docs/catalog.md` differs from `scripts/gen_catalog_doc.py`. `tests/test_packaging.py` checks the built artifacts in `dist/` and is skipped without them.
- Browser E2E (`tests/e2e/`) is dev-only and runs with `npx -y playwright@1.61.1`. It is skipped when unavailable.
