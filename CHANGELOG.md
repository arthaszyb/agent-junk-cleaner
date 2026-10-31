# Changelog

## 0.4.0 - beta

First beta release. A rewrite of `ai-agent-disk-tool` 0.3.0 under a new name, `disk-steward`. macOS only. Verified on one Mac (macOS 27.0.1, Apple silicon, Python 3.9.6 and 3.14.8). 0.4.0 reads nothing from 0.3.0's data directory.

### New

- A rule catalog of 107 rules over four areas: AI agents (Codex, Claude Code and Desktop, Cursor, Windsurf, Antigravity, Ollama), apps and Library, your own files (opt-in) and shared developer caches (report only). The reference is generated into `docs/catalog.md`, and a test fails when the two differ.
- Three tiers. Junk is selected in bulk, review per group or file, protected never. Credentials, databases and git work trees are protected in every area, and so is a review-tier file that starts like a SQLite database.
- Detectors that judge by disk state: duplicate Codex sessions left by an ID migration, the Claude CLI version in use, Claude Desktop engines and plugins, obsolete editor extensions, Electron caches with the owning app named, Application Support folders of uninstalled apps, dependency folders of idle projects.
- A server-owned selection with an age filter, per-rule minimum ages that nothing lowers, and "Include anyway" for one item.
- A plan handshake: the plan is created only if a fresh resolution matches what the user saw (409 otherwise, 422 when empty). A plan stores each path with its fingerprint, expires after 24 hours and refuses to run after a catalog or settings change; entries whose verdict changed with the detector facts are kept, and the preflight counts them.
- An executor that re-checks every entry before touching it (parent folders, identity, fingerprint, classification, age, running apps, open files), stages items by rename before deleting, verifies an archive before removing its source, recovers from a crash, and writes an audit log.
- A browser portal on the loopback address with a one-time launch link, seven pages and English and 简体中文 text. A full command line: `scan`, `report`, `list`, `plan`, `show`, `apply`, `archives`, `diagnose-session`, `schedule`, `advise`, `insights`, `tools`, `portal`, `doctor`.
- Restorable archives (`.gz` for a file, `.tar.gz` for a folder) that restore without overwriting.
- A weekly LaunchAgent that scans and posts a notification. It never deletes.
- An AI advisor that runs the local `codex` or `claude` CLI on an anonymised payload, validates the reply strictly and only attaches suggestions. A manual export and import path works with any chat.
- An Insights page and `disk-steward insights` command (`GET /api/insights`): environment, key numbers with their change over 7, 30, 90 days or all history, where the volume's space goes, scan and cleanup history, the groups and AI agents that grew or shrank, and up to 12 follow-ups, each linking to the page that shows the same number. Database schema 3 stores per-scan totals by tier and area for it; scans from before are rebuilt from group history and marked approximate.
- Retention: a finished cleanup keeps a complete summary, and seven days after it finished its per-item rows are deleted (skipped, failed and left-out entries stay; the audit log keeps every removed path). Free pages are returned to the file system after every deletion and at start. On a copy of the author's database, two cleanups of about 230,000 entries took the file from 224 MB to 19 MB.
- Native cleanup commands for report-only tools: a Run button (and `disk-steward tools run RULE_ID`) where the tool has an official command. A fixed registry of argv lists (`brew cleanup --prune=all`, `pip3 cache purge`, `go clean -cache`, `go clean -modcache`, `pnpm store prune`, `uv cache prune`, `npm cache clean --force`, `yarn cache clean`, `colima prune --force`, `xcrun simctl delete unavailable`, `ollama rm MODEL`), the exact command confirmed first, the tool found on `PATH` at run time, output streamed live, a 15-minute timeout that ends the whole process group, cancel, the space freed measured before and after, an audit line each side, and a rescan. Commands that delete by path or may download (`rm -rf`, `npx`) stay copy-only.
- Packaging: wheel, sdist, a single-file `.pyz`, `scripts/install.sh` (puts a downloaded `.pyz` under `~/.local/share/disk-steward/` with a wrapper in `~/.local/bin/`, and removes both with `--uninstall`), `Open Disk Steward.command` for Finder, and `scripts/build_release.sh`, which also copies `install.sh` into `dist/` and checksums it. The test suite needs macOS and runs on the maintainer's Mac.
- A unit suite (see `docs/safety.md` for the split) and a Playwright E2E against a fake home, both run on Python 3.9 and 3.14.

### Changed from 0.3.0

- Package `disk_steward`, command `disk-steward`, data in `~/Library/Application Support/Disk Steward`. The `ui` command is now `portal`.
- Python 3.9 or newer instead of 3.10.
- Shared caches are the `dev-shared` area, report only with the native command shown, instead of `scan --include-shared`.
- The advisor no longer needs `ANTHROPIC_API_KEY`: it uses the login of the local `codex` or `claude` CLI. Codex runs automatically instead of by manual export only, and the manual path remains.

### Removed

- The legacy shell scripts and Python entry points of 0.3.0, and the Chinese-named Finder launcher.
- The Ollama advisor provider. Ollama models are still listed, report only, with `ollama rm` as the way to remove one.

### Known limits

See the Limitations section of the README. In short: no restore for items moved to the Trash, space freed can be below the estimate on APFS, no other macOS version or Mac verified, and no hosted runner runs the suite, because it needs macOS.
