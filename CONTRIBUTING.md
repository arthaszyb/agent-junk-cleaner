# Contributing

Disk Steward is a beta macOS tool that deletes files, so a change is judged by what it can remove and how that is checked. Read `docs/architecture.md` for the module contracts and `docs/safety.md` for the invariants before you touch scanning, selection, planning or the executor.

## Setup

There is nothing to install. Python 3.9 or newer, standard library only; the repo has no runtime or test dependencies, and a pull request should not add one.

```sh
git clone https://github.com/arthaszyb/agent-junk-cleaner.git && cd agent-junk-cleaner
PYTHONPATH=src python3 -m disk_steward --help
```

Write for Python 3.9: start modules with `from __future__ import annotations`, no `match`, no `X | Y` evaluated at runtime, no `slots=` dataclasses. Comments say why, not what. Every user-visible string is an i18n key with an `en` and a `zh-CN` text. A failure that the user can cause raises `StewardError`; do not catch an error to hide it.

## Tests

Run the suite on the Python that ships with macOS and on a current one:

```sh
PYTHONPATH=src /usr/bin/python3 -m unittest discover -s tests
PYTHONPATH=src python3 -m unittest discover -s tests
```

There is no hosted CI: the test suite needs macOS, so run the suite on both interpreters and the browser E2E on your Mac before you open a pull request.

It takes about two and a half minutes on 3.9. Every test works in a temporary home (`FakeHome` in `tests/support.py`) and a temporary data directory; `real_home_guard()` fails a test that touches the real home. `tests/test_macos_real.py` runs the real `lsof`, `mdls`, `tmutil` and friends, so it needs macOS. `tests/test_install.py` runs `scripts/install.sh` against a temporary `HOME`.

The browser E2E drives a real Chromium against a fake home. It needs Node, Playwright 1.61.1 and a Chromium build, and prints `SKIPPED` when one is missing:

```sh
sh tests/e2e/run.sh        # E2E_OUT=dir keeps the screenshots, E2E_PYTHON picks the interpreter
```

Playwright is never added to the repo. `run.sh` finds it in the npx cache or fetches it there with `npx -y -p playwright@1.61.1`.

To click through the portal yourself on the same fake home, run `python3 scripts/dev_portal_fixture.py` and open the printed URL. The fixture strips your real tools from `PATH`, so a "Run" in the report-only table can only reach fake tools under the temp folder; `--real-tools` lifts that, and a Run click then cleans your real Homebrew, pip or Ollama data.

To see what a scan finds on your real Mac without touching anything, run `python3 scripts/dev_smoke_scan.py`. It is read-only and writes to a temporary data directory.

## Building a release

```sh
sh scripts/build_release.sh
```

It installs `build` into a throwaway venv in `/tmp` (this downloads from PyPI), builds the sdist and wheel into `dist/`, builds `dist/disk-steward-<version>.pyz` with `scripts/build_pyz.py`, copies `scripts/install.sh` to `dist/install.sh`, and writes `dist/SHA256SUMS` for all of them. Nothing is published; the `.pyz` and `install.sh` are the two files users download from the release page. With `dist/` present, `tests/test_packaging.py` checks the artifacts: no databases, caches, screenshots or `.DS_Store`, and every package file in the wheel and the zipapp.

## Adding a catalog rule

A rule decides what a path is. It does not decide that the path is removed. Be conservative: when unsure, make it `review`, or protect it.

1. Pick the area and module in `src/disk_steward/catalog/` (`claude.py`, `codex.py`, `other_agents.py`, `app_library.py`, `user_files.py`, `dev_shared.py`). Choose the tier (junk is rebuilt by the app on demand, review needs a human, protected is never offered), the kind, the unit (`file`, or `bundle` for a folder handled as one item), the actions with the default first, the minimum age floor and the apps that must be quit (`quit_apps`, bundle ids).
2. Choose the priority. Protections are 1000, hand-written protections 900, specific rules 100 to 500, generic fallbacks below 100. Two rules of the same priority must not match the same path.
3. Patterns start with `~/`, `$TMPDIR/` or `$CACHEDIR/`. `*` stays inside a segment and `**` spans segments. If the verdict depends on disk state (which version is in use, whether an app is installed), write a detector in `src/disk_steward/detectors/` and register it in `detectors/__init__.py`; do not widen the pattern.
4. Add fixture paths to a test: files that must match and files next to them that must not, under `FakeHome`, with the tier and group key you expect (see `tests/test_catalog.py`, `tests/test_catalog_general.py` and `tests/m2_fixture.py`). Include a credential, a database or a git folder next to the junk to show it stays protected.
5. Add `rule.<id>.label` and `rule.<id>.effect` to `src/disk_steward/i18n/en/rules.json` and `zh-CN/rules.json`. A label with `{path}` is for a rule with `group_depth`. The effect says what removing it costs the user in plain words.
6. Run the lint tests: `python3 -m unittest tests.test_catalog tests.test_catalog_general tests.test_i18n`. They check that every pattern resolves to its own rule, that no rule at the same priority collides, that the fast classifier agrees with brute-force matching, that every detector is registered and that both locales have every key.
7. Regenerate the reference and commit it with the rule: `python3 scripts/gen_catalog_doc.py > docs/catalog.md`. `tests/test_docs.py` fails while it is stale.

## Pull requests

Say what the change can remove or expose, and how the tests show it cannot do more. For scanning, selection, planning and executor changes, add a test for the allowed case and one for the protected or racing case. Do not attach scan results, paths from your Mac, archives or audit logs.

Bugs and questions go to the [issues](https://github.com/arthaszyb/agent-junk-cleaner/issues) of the repository; security reports follow [SECURITY.md](SECURITY.md).
