# Safety

What Disk Steward guarantees before it removes anything, which module enforces each guarantee, what it defends against, how that was tested, and what was not tested. The design is in [architecture.md](architecture.md); the rules are in [catalog.md](catalog.md).

Delete is permanent. Archive keeps a verified, restorable copy, and Trash hands the item to macOS. Junk defaults to delete, session histories to archive, and installers, large and idle files and leftovers of uninstalled apps to Trash.

## Invariants

| Invariant | Enforced in |
|---|---|
| Whether an item is selected is decided in one function. The planner, the UI counts, the CLI and the report all call it, so the number you see is the number a plan holds. | `selection.decide()` and `resolve()` |
| The client sends operations, never a list of paths. The server validates every key against the scan and refuses protected, tainted, domain-off and report-only items. | `selection.apply_ops()`, `portal/api.py` |
| Selection semantics are fixed: an exclusion beats everything, a rule's minimum age is never lowered by a filter or by "Include anyway", and a rule default can only make your filter stricter. | `selection.decide()` steps 4 to 9 |
| A selection carried to a newer scan keeps its exclusions and group choices, and keeps an individual include only if the file is still the one you picked. | `selection.migrate_spec()`, `store.spec_include_fp` |
| A plan is created only if a fresh resolution gives the digest the client saw. It then stores each path with a fingerprint and the age threshold, and expires after 24 hours. | `planner.create_plan()` |
| A plan runs only if the catalog and the settings that affect classification are the ones its scan used, and the client holds its current revision and entries digest. Detector facts (current CLI version, installed apps, newest editor cache) drift while apps run, so they never stop a plan as a whole: each entry is re-classified with fresh facts and kept when its verdict changed, and preflight counts those entries in advance. | `planner.validate_for_run()`, `executor.run()` |
| Every entry is re-checked at run time, in a fixed order, and skipped at the first failure. Nothing is forced. | `executor._attempt()`, `_checked()` |
| Parent folders are opened one by one without following symlinks, and each must belong to you or to root. | `fsutil.open_parent_nofollow()`, `actions.open_parent_checked()` |
| An item is renamed, never copied, into a private staging folder with `renameatx_np(RENAME_EXCL)`, verified there, and only then unlinked. It goes back under its own name the same way, or stays in staging and is reported. | `actions._stage()`, `put_back()`, `fsutil.rename_excl()` |
| An archive is hashed while it is written, synced to the disk, read back and verified, registered in the database, and only then is its source removed. | `actions._archive_file()`, `_archive_bundle()`, `_finalise_archive()` |
| After a crash, every `in_progress` entry is settled from where its item is and what its archive verifies to. A folder that may be half removed is never put back. | `executor.recover()` |
| A running app that owns the data, or a process that has the file open or is executing it, blocks the entry. If the snapshot of running apps and open files cannot be taken, the run stops; it is never read as "nothing is open". | `guards.Guard`, `macos.RealMacOS` |
| Restore never overwrites, verifies the archive before the file appears, and refuses archive members that would write outside the destination. | `actions.restore()`, `_Extractor` |
| One operation at a time per data directory, across the CLI and the portal, and the executor checks that it still holds the lock before every entry. | `locking.OperationLock` |
| Every run writes an intent line before it touches an entry and an outcome line after, each fsynced. | `audit.Audit` |
| A scan never lists, stats or opens the privacy list, never follows a symlink, never crosses to another volume, and counts hard-linked data once. | `classify.Classifier.privacy_skipped()`, `scanner` |
| Credentials, databases and git work trees are protected wherever they sit, and a folder that holds one is protected as a whole. | `catalog/protections.py`, `classify`, `fsutil.walk_bundle()` |
| The advisor suggests. No code path between it and the selection exists, and a reply is used only after strict validation. | `advisor/` ([ai-advisor.md](ai-advisor.md)) |

## The per-entry checklist and what each step stops

| Step | Check | Stops | Skip reason |
|---|---|---|---|
| 1 | Pause, cancel, lock still held, facts no older than 15 s | A run that continues after you stopped it, after another process took the lock, or on stale facts | |
| 2 | The entry was not excluded since the plan was made | Removing what you took out of the plan | none (not touched) |
| 3 | The rule still exists in the catalog | A plan made under another catalog | `skip.rules_changed` |
| 4 | Parent chain opened with `O_NOFOLLOW`, ancestors owned by you or root | A symlink or a folder owned by someone else swapped in above the item | `skip.unsafe_parent`, `skip.vanished` |
| 5 | `lstat` through the parent fd: right type, your uid, same volume as the base and staging, not a cloud placeholder, not immutable | Removing a symlink or another kind of object that took the path, a file of another user, a mount point, a file that would be downloaded just to be listed | `skip.changed`, `skip.foreign_owner`, `skip.other_volume`, `skip.dataless`, `skip.locked` |
| 6 | Fingerprint equals the plan (file: device, inode, size, mtime, birth time, one link; folder: device and inode) | A file replaced, rewritten or appended to since the plan | `skip.changed` |
| 7 | Re-classified with today's catalog and facts: same rule, not protected, action allowed | A git folder or database that appeared next to a junk file, a version that became current, a rule that changed | `skip.rules_changed` |
| 8 | Age at least the plan's threshold, judged now (Spotlight re-read for last-used rules) | A file used since the scan, a clock that moved back | `skip.too_recent`, `skip.last_used_unknown` |
| 9 | No quit-first app running, no open or executing file under the item | Deleting data from under a running app | `skip.blocked:<reason>` |
| 10 | Stage by rename, re-verify (file: fingerprint; folder: a fresh walk gives the same file count, size and newest change and no taint), then unlink | A swap in the last instant, a folder that changed while it was being archived | `skip.changed`, or `needs_attention` when the item cannot be put back |

Before the first entry the executor also checks the plan state, the hashes, the lock, that staging and the archive folder are on the same volume as the entries, that free space is at least 1.2 times the archives plus 1 GiB, and that `/usr/bin/trash` exists if a trash action is planned.

## What it defends against

| Threat | Defense |
|---|---|
| A file swapped between the scan and the delete | The fingerprint at step 6, the staged re-verification at step 10, and the private staging folder, which nothing else writes to. |
| A symlink or a folder owned by someone else placed above an item | The scanner never follows symlinks; the executor opens every ancestor with `O_NOFOLLOW` and checks its owner. |
| A stale plan | 24-hour expiry, hash checks, a revision and entries digest the client must present, and the per-entry re-checks. A plan made from a stale view is refused with 409 and what changed. |
| The rules or settings changing after a plan was made | The catalog and settings hashes refuse the whole plan; changed facts are handled by a fresh classification of every entry. |
| An app that is running or a file that is open | Step 9, using `lsappinfo` and `lsof`. A one-day floor covers writers that open, append and close, which `lsof` never sees. |
| A web page, another site or DNS rebinding talking to the portal | Loopback bind, exact Host check, one-time launch code traded for a session token that travels in a header (no cookie, so other servers on 127.0.0.1 never receive it), Origin check, custom header, JSON-only bodies, CSP, no CORS headers. |
| An advisor reply that tries to select, run commands or smuggle text | The advisor has no path to the selection. Replies are validated against a schema, tokens must belong to the request, a protected group cannot be called likely safe, and control and invisible formatting characters are refused. |
| A hostile or damaged archive | The archive's hash and length are checked against the manifest while it streams, a stream longer than promised is cut off, and members with absolute names, `..`, devices, hard links, escaping symlinks or repeated paths are refused. |
| A crash or power cut in the middle of a run | Staged renames, durable archive records, and `recover()` at the next start. |
| Credentials, databases and repositories | They are protected in every area, by name, by neighbouring files, by git work tree, by SQLite header for review files, and by taint for any folder that holds one. |

## What it does not defend against

- A malicious process running as you. It can read the data directory and `portal.json`, mint launch codes, edit the database and the plan, or delete files itself.
- Root, and physical access to an unlocked Mac.
- APFS snapshots and clones. Removing a file does not free blocks that a local snapshot or a clone still references. The run summary reports the estimate, the measured change and the number of local snapshots.
- A writer that changes a file without holding it open. The age floors reduce the window; they do not close it.
- Processes `lsof` cannot see. It lists only the processes your user may inspect.
- A wrong decision on your side. A review item that you select and confirm is removed (archived by default, for session histories).
- Rules that are wrong. The catalog is written from what these tools do today, and a vendor can change where it keeps things. That is why every entry is classified again at run time, and why unknown files are protected.
- What the model CLIs add. Codex may include your global `AGENTS.md` and Claude adds its built-in prompt; only the JSON payload is Disk Steward's.
- Anything outside macOS.

## How it was verified

Unit tests run with a temporary home and data directory, a fake macOS adapter, and a settable clock. As of 0.4.0 the suite has 1,741 tests (the split below is a snapshot, not enforced), all passing on `/usr/bin/python3` (3.9.6) and Python 3.14.8.

| Area | Tests | Files |
|---|---|---|
| Primitives: paths, settings, i18n, filesystem, lock, audit, events, fake home | 134 | `test_paths`, `test_settings`, `test_i18n`, `test_fsutil`, `test_locking`, `test_audit`, `test_events`, `test_fakehome` |
| macOS adapter and schedule | 77 | `test_macos_real` (real `lsof`, `mdls`, `tmutil` output), `test_schedule` |
| Catalog, classification, detectors, scanner | 299 | `test_catalog`, `test_catalog_general`, `test_classify`, `test_detectors`, `test_scanner` |
| Store, selection, planner, reports, service | 295 | `test_store`, `test_selection`, `test_planner`, `test_report`, `test_report_extras`, `test_service` |
| Executor, actions, runs | 206 | `test_executor`, `test_actions`, `test_service_run` |
| CLI | 95 | `test_cli` |
| Portal | 111 | `test_portal_server`, `test_portal_service`, `test_portal_assets` |
| Advisor | 183 | `test_advisor_cli`, `test_advisor_i18n`, `test_advisor_payload`, `test_advisor_providers`, `test_advisor_service`, `test_advisor_validate` |
| Documentation, packaging and installer | 46 | `test_docs`, `test_packaging` (the packaging tests skip without `dist/`), `test_install` (`scripts/install.sh` in a temporary home, under `/bin/sh` and `/bin/dash`) |

**Fault injection.** The executor takes hooks at eight points (`after_claim`, `after_parent`, `after_lstat`, `after_fingerprint`, `after_classify`, `after_guard`, `before_unlink`, `archive_chunk`), and tests use them to change the world at the worst moment. Covered: inode swapped, file appended or touched after the plan, file replaced by a symlink or a folder, an ancestor replaced by a symlink after the claim, file vanished, becoming hard-linked, owner changed, immutable and dataless flags, another volume, a git folder or a database appearing next to the item, the clock moving back, a running app, an open file, an executing binary, a bundle with an open or running file inside, an unexpected exception, a failure inside the action, the original name recreated while the item is staged, a lost lock, a store error, a failed or stale guard snapshot, pause and cancel in the middle of an archive, the disk filling during an archive, a file written to while it is archived, a FIFO inside a bundle, and a plan that expires or whose settings change during a pause. Crash recovery is tested after staging, after the unlink, before staging, while streaming an archive, after a verified archive, with an archive that never reached the database, with a damaged archive, and with the original name taken. Restore is tested with hostile tar members (parent directory, absolute names, link chains that leave the bundle, writes through a link, devices, FIFOs, hard links, duplicate paths), a stream longer than its manifest, an archive that does not match its manifest, symlinked and foreign-owned ancestors, and a destination that exists.

**Real-browser E2E.** `tests/e2e/run.sh` drives headless Chromium through Playwright 1.61.1 against the portal on a fake home with a fake macOS, fake tools and a fake advisor. 55 steps, among them: trading a launch code for a session token (no cookie); the overview's safe-to-clean figure equal to the server's resolution and to the cleanup list it opens; every overview figure opening Browse with exactly that count and size; "Include anyway"; a stale view refused with the new summary; leaving out a group or one item and the start button following; a run with pause, leave-out and resume and its result on the same page; the disk showing exactly the finished entries gone; drafts replaced by a later scan; archive restore (never over an existing file) and purge; the advisor annotating without selecting; 简体中文 with no raw message keys; a blocked and a changed entry skipped with translated reasons; no console errors or CSP violations. It passed 55 of 55 on macOS 27.0.1 with Node 26.10.0 on 2026-10-30. It is not part of the unit run or CI.

**Reviews.** Four independent Claude Opus reviews ran during development, each read-only and adversarial: the classification layer (catalog, detectors, classifier, scanner), selection and planning, the executor (actions, guards, recovery), and the portal together with the advisor. Every finding marked critical, high or medium was fixed with a regression test. That is model review. No person outside the project has audited the code.

## What is not verified

- Any other Mac, macOS version, Python version (only 3.9.6 and 3.14.8 were run), or an Intel Mac.
- A hosted test run. The suite needs macOS, so the tests have run only on the maintainer's Mac.
- Safari and Firefox. The E2E uses Chromium only.
- The advisor against `codex` and `claude` builds other than the ones on the author's Mac (`codex-cli 0.160.0`, Claude Code 2.1.299).
- iCloud Drive and other cloud-synced folders with real placeholders. The code skips undownloaded placeholders and warns about synced roots, but no test used a real cloud folder.
- Very large trees. A scan of about 275,000 items took around 35 seconds; resolving a selection and creating a plan were exercised with 150,000 items in the unit tests. Nothing larger was tried.
- Behaviour with Time Machine running a backup while a plan executes.
- Long-term effects of vendors changing their folder layouts after 0.4.0.
