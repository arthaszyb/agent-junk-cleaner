# Disk Steward

Disk Steward finds what AI coding agents leave on a Mac and removes it only after you have reviewed it. It also covers ordinary Mac junk, such as app caches, old logs and update leftovers.

Version 0.4.0 (beta), MIT licensed ([LICENSE](LICENSE)). macOS only, Python 3.9 or newer, standard library only. It has been verified on one Mac (macOS 27.0.1, Apple silicon, Python 3.9.6 and 3.14.8). Run it while you watch, and report what you find ([Support](#support)).

## Install in 2 minutes

You need `python3` 3.9 or newer. Check with `python3 --version`; on a Mac without one, `xcode-select --install` installs it.

1. Download `disk-steward-0.4.0.pyz` and `install.sh` from the [release page](https://github.com/arthaszyb/agent-junk-cleaner/releases).
2. In Terminal, run the installer on the downloaded file, then start the portal:

```sh
sh ~/Downloads/install.sh ~/Downloads/disk-steward-0.4.0.pyz
disk-steward portal --open
```

The installer copies the file to `~/.local/share/disk-steward/` and writes a small wrapper at `~/.local/bin/disk-steward`. It never uses `sudo`, and it refuses to overwrite a file at that path that it did not write (a `pip` or `pipx` install leaves one there). If `~/.local/bin` is not on your `PATH`, it prints the line to add. Run it again with a newer `.pyz` to update. `sh install.sh --uninstall` removes the wrapper and the `.pyz`; your data stays in `~/Library/Application Support/Disk Steward`, and the command prints how to remove that too.

If Terminal answers `command not found`, `~/.local/bin` is not on your `PATH` yet: add it as the installer says, or run `~/.local/bin/disk-steward portal --open`. The portal prints a link and opens it in your browser; [Quick start](#quick-start) walks through the first scan.

To try it without installing anything, run the download directly:

```sh
python3 ~/Downloads/disk-steward-0.4.0.pyz portal --open
```

The release page also lists `SHA256SUMS`. `shasum -a 256 ~/Downloads/disk-steward-0.4.0.pyz` should print the same hash.

**Gatekeeper.** A file saved by a browser carries a quarantine flag. `python3 disk-steward-0.4.0.pyz` is not blocked, because macOS sees `python3` start, not the file. The Finder launcher `Open Disk Steward.command` (from a source checkout) can be blocked when it came through a browser: right-click it, choose Open, and confirm. Or skip it and use the command line.

## Who it is for

People whose Mac fills up from Codex, Claude Code, Claude Desktop, Cursor and similar tools: session histories, caches, update downloads, old versions of the apps and VM images. It also finds ordinary Mac junk (app caches, old logs, leftovers of uninstalled apps), and with the `user-files` area switched on, installers, idle project dependency folders and large idle files in your Downloads, Desktop and Documents. Caches of developer tools such as Homebrew, npm and Xcode are listed with the tool's own cleanup command.

## What it never does

- It makes no network calls of its own. There is no telemetry, no update check and no account. The portal talks to your browser over `127.0.0.1` only. Two optional features start programs that you control: the AI advisor (off by default) runs your own `codex` or `claude` CLI under its existing login, and the Run buttons for report-only tools run your own `brew`, `pip`, `ollama` and similar after you confirm the exact command.
- It deletes nothing without a cleanup list that you reviewed and started. The one other kind of removal is a report-only tool's own cleanup command from a Run button, and you see its exact text first.
- It offers only what a rule in the audited catalog recognises. Anything unknown is protected, and so are credentials, databases and git work trees in every area.
- It runs as you, with no `sudo` and no admin rights.

## Why

Codex, Claude Code, Claude Desktop, Cursor and similar tools write logs, caches, update downloads, old versions of themselves, session histories and VM images, and none of them cleans up completely. On a heavy Codex/Claude user's Mac we have seen:

| What | Size |
|---|---|
| `~/.codex` | about 7 GiB, of which sessions about 5 GiB |
| Claude (CLI and Desktop) | about 3 GiB |
| Ollama models | about 9 GiB |
| App caches under `~/Library` | about 14 GiB |
| Shared developer caches (Homebrew, npm, Cargo, Xcode...) | about 30 GiB, report only |

On one such Mac, Codex held 281 pairs of duplicate session files left behind by an ID migration. Disk Steward compared them and confirmed 124 pairs as identical apart from the IDs. Only those 124 go into a separate duplicates group. The other pairs stay in the normal sessions group.

Every deletion goes through a rule in an audited catalog ([docs/catalog.md](docs/catalog.md)) plus your explicit selection. Nothing outside the catalog is ever offered, and an AI model can suggest but never select.

## Other ways to run it

There is no PyPI release. Besides the installer above:

**From source, without installing.** In a checkout:

```sh
PYTHONPATH=src python3 -m disk_steward portal --open
```

**Installed from source:**

```sh
python3 -m pip install .      # or: pipx install .
disk-steward portal --open
```

**Build the single file yourself.** `scripts/build_release.sh` writes `dist/disk-steward-0.4.0.pyz` and `dist/install.sh`, the two files of the release page. `scripts/build_pyz.py` alone builds just the `.pyz`.

**From Finder.** Double-click `Open Disk Steward.command` in the source folder. It starts the portal in a Terminal window; keep that window open while you work and close it to stop. Outside a source checkout it runs `disk-steward`, which must be on your `PATH`. If macOS blocks the file because it came from a download, right-click it and choose Open. If unzipping removed the executable bit, run `chmod +x "Open Disk Steward.command"`.

`disk-steward doctor` prints the interpreter, the data directory, whether `codex`, `claude` and `/usr/bin/trash` are found.

## Quick start

### Portal

```sh
disk-steward portal --open
```

The command starts a server on `127.0.0.1` and prints a link of the form `http://127.0.0.1:<port>/launch?code=...`. The link works once, for one browser, within two minutes; `--open` opens it for you. From another terminal, `disk-steward portal --url` prints a fresh link to the portal that is running. `--port N` fixes the port. Ctrl-C stops the server.

The portal has seven pages: Overview, Insights, Browse, Cleanup, Archives, AI advisor and Settings. Every figure it shows leads to exactly the items behind it, and the number on a button is the number that gets cleaned.

1. Overview: press Scan again. A scan only reads. The top card says how much is safe to clean (rebuildable junk unchanged for at least 14 days; change the number of days in place). Every other figure (tiers, "where the scanned space goes", the largest groups) opens Browse filtered to exactly those items.
2. Press "Review and clean X". This opens the cleanup list with the same X, grouped by kind. Untick a group or a single item to keep it; the start button always shows what is left. The list names the apps you must quit to include more, and the entries that changed since the scan (those are kept).
3. Start cleaning. You can pause, resume or cancel, and leave out what is not done yet. The result appears on the same page: what was cleaned, every skipped item with its reason, and the space actually freed. The portal then rescans, and the overview shows what the last cleanup freed.

Insights shows what has happened to the disk. The key numbers (free space, scanned size, space freed by cleanups so far, what is safe to clean now) come with their change over the last 7, 30 or 90 days, or all history. A bar splits the volume into free, not scanned, protected, review, shown only and junk. Below it are the scan and cleanup history, the groups and AI agents that grew or shrank, and a short list of follow-ups; each links to the page that shows the same number. Scans taken before per-tier totals were stored are rebuilt from their group sizes with today's rules and marked approximate. The page only reads what scans and cleanups stored: it never scans, never measures `/Applications` and never changes a file. `disk-steward insights` prints the same data (`--window DAYS`, 0 for all history; `--json`).

Tools that keep their own caches (Homebrew, pip, uv, npm, pnpm, Yarn, Go, Colima, Xcode simulators, Ollama models) are listed as "shown only", because Disk Steward does not delete their files itself. Where the tool has an official cleanup command, the row has a Run button: it shows the exact command, you confirm it, and the tool does its own cleanup while the output streams in. Disk Steward measures the tool's folders before and after and reports what was freed, then rescans. A run can be cancelled, stops after 15 minutes, and is written to the audit log and to the Insights history. Ollama models are removed one at a time by name. A command that deletes by path or may download (`rm -rf ~/.npm/_npx`, `npx playwright uninstall --all`) is shown to copy, never run here. `disk-steward tools` lists the rows and `disk-steward tools run RULE_ID [--target NAME]` runs one (it asks you to type the rule id; `--yes` skips that).

To pick things by hand instead, use Browse: tick groups or single items ("Include anyway" adds a too-recent item on purpose) and create a cleanup list from your selection. The Cleanup page shows the running task, the list waiting for you, or recent results. A list belongs to one scan; the next scan replaces lists that never ran.

### Command line

The same flow, from scan to archive restore. This output comes from a small throwaway home directory (240 MiB of sample files), abridged.

```console
$ disk-steward scan --domain agent
Scan #2, finished 2026-10-29 00:13. Areas: AI agents.
...
Largest groups (top 8)
Tier            Size  Items      Change  Group
---------  ---------  -----  ----------  -------------------------------------------------------
Review     55.00 MiB      2         0 B  Codex sessions (~/.codex/sessions/2026/08)
Junk       46.00 MiB      2         0 B  Cursor caches
Junk       44.00 MiB      2         new  Old Claude Code versions
Junk       30.00 MiB      2         0 B  Codex logs
Protected  22.00 MiB      1  -44.00 MiB  Old Claude Code versions (kept)
...

$ disk-steward plan --select junk --select 'codex.sessions:~/.codex/sessions/2026/08' --older-than 14d
Selected 9 of 12 scanned items: 189.00 MiB allocated, 189.00 MiB reclaimable.

Action   Items        Size  Effect
-------  -----  ----------  --------------------------------------------------------
Archive      2   55.00 MiB  frees about 38.50 MiB (estimate; depends on compression)
Delete       7  134.00 MiB  frees 134.00 MiB
...
Plan a50c7c4e6c8f5e9d32a573f8 created. It expires 2026-10-30 00:13.
Review it:  disk-steward show a50c7c4e6c8f5e9d32a573f8 --entries
Run it:     disk-steward apply a50c7c4e6c8f5e9d32a573f8

$ disk-steward apply a50c7c4e6c8f5e9d32a573f8
Plan a50c7c4e6c8f5e9d32a573f8: 9 entries pending, 189.00 MiB allocated.
Rules and settings are unchanged since the scan this plan is based on.
Type the number of entries (9) to confirm: 9
Plan a50c7c4e6c8f5e9d32a573f8 finished in 1.4 s.
...
Removed 189.00 MiB, freed about 133.98 MiB, moved to the Trash 0 B.
Free space: estimated change +133.98 MiB, measured change +133.81 MiB.
Warning: 3 local snapshot(s) exist. Removed space may not show as free until they age out.

$ disk-steward archives list
ID                 State        Size     Stored  Created           Path
-----------------  ------  ---------  ---------  ----------------  ------------------------------------------
a50c7c4e-6-a75f3b  Stored  31.00 MiB  31.01 MiB  2026-10-29 00:13  ~/.codex/sessions/2026/08/21/rollout-...jsonl
a50c7c4e-7-2e8342  Stored  24.00 MiB  24.01 MiB  2026-10-29 00:13  ~/.codex/sessions/2026/08/14/rollout-...jsonl

$ disk-steward archives restore a50c7c4e-6-a75f3b
Restored ~/.codex/sessions/2026/08/21/rollout-2026-08-21T16-02-10-0198f4b9-12aa-7c02-9d3e-0a1b2c3d4e5f.jsonl (31.00 MiB).
```

The sample files are random bytes, so the archives barely shrink. Real session files are JSONL text and compress well.

Other commands: `insights` (environment, history and follow-ups; `--window DAYS`, `--json`), `tools` and `tools run RULE_ID` (native cleanup commands of report-only tools), `report` (add `--since-last` for growth only, `--apps` to measure `/Applications`), `list --tier junk` or `list --group KEY`, `plan --dry-run`, `plan --exclude GLOB` and `--include ITEM_KEY`, `plan --action archive|delete|trash`, `show ID --entries`, `archives purge`, `diagnose-session ITEM_KEY` (counts what makes a Codex session file large, never prints its content). `--lang en|zh-CN` and `--data-dir PATH` work on every command. Exit codes: 0 done, 1 scan problems or a cancelled run, 2 refused, 3 another operation holds the lock.

`apply` asks you to type the number of entries. `--yes` skips that, and without a terminal it is required. Ctrl-C during a run pauses it: continue, exclude entries by glob, or abort.

## How cleanup decisions work

### Tiers

| Tier | Meaning | How you select it |
|---|---|---|
| junk | Rebuilt by the app on demand: caches, logs, update leftovers, superseded versions | In bulk (`--select junk`, Select all junk) |
| review | Needs your judgment: session histories, VM images, runtimes, installers, large or idle files, leftovers of uninstalled apps | Per group or per file |
| protected | Credentials, databases, git work trees, settings, your own content, the version in use, anything no rule recognises | Never. Listed so the space stays visible |

### Areas

| Area | Default | What it covers |
|---|---|---|
| `agent` | on | Codex, Claude, Cursor, Windsurf, Antigravity, Ollama |
| `app-library` | on | `~/Library/Caches`, `~/Library/Logs`, Electron caches, VS Code, browser caches, leftovers, `$TMPDIR` |
| `user-files` | off | Installers, idle project dependency folders, large and idle files in `~/Downloads`, `~/Desktop`, `~/Documents`, `~/Movies` (the `user_roots` setting) |
| `dev-shared` | off | Homebrew, npm, pip, Cargo, Go, Gradle, Xcode and similar stores. Report only: the group shows its size and the native command that cleans it |

A few `dev-shared` groups also appear with the default areas, because their folders sit under `~/Library/Caches`. They stay report only.

Turn areas on in Settings in the portal. The CLI has no settings command, so from the CLI write the `domains` key of `settings.json` in the data directory. The full rule list, with patterns, minimum ages and the apps each rule needs quit, is [docs/catalog.md](docs/catalog.md).

### Age filter, floors, Include anyway

Each item has an age: the later of its modification and creation time, or for installers and large or idle files the later of that and Spotlight's last-used date. The age filter (14 days by default) is a bulk setting for selected groups.

A rule can carry a floor, such as one day for logs and session files or 90 days for leftovers of uninstalled apps. No filter setting and no "Include anyway" goes below it. A rule can also pre-fill a stricter filter for its group, such as 180 days for idle files, and it can only make your filter stricter, never looser.

"Include anyway" selects one item that the filter would hold back. It overrides the filter and nothing else.

### The plan handshake

The portal shows a count and size for your selection and sends a digest of that selection when you create a plan. The server resolves the selection again and compares:

- If the result differs (an item crossed the age threshold since the page loaded, say), it answers 409 with the added and removed items and creates nothing. A selection made on an older scan than the latest one is refused as stale.
- If nothing qualifies, it answers 422 with a breakdown of why.
- Otherwise it stores a plan: every path with its fingerprint, the action and the age threshold. A plan expires after 24 hours and refuses to run if the catalog or the settings that affect classification changed since its scan. Detector facts (the current CLI version, installed apps) are checked per entry instead: an entry whose verdict changed is kept, and the preflight says how many.

What you saw is what runs. The CLI's `plan` does the same steps in one process.

### The executor

`apply` and the portal's cleanup list start the executor, which does not trust the plan. Before it starts, it checks that the plan is runnable, that the lock is held, that free space covers the archives (1.2 times their size plus 1 GiB) and that `/usr/bin/trash` exists if a trash action is planned. For every entry it then checks, in this order, and skips the entry at the first failure:

1. You have not paused, cancelled or excluded it.
2. Its parent folders are real folders owned by you or root, with no symlink anywhere on the way.
3. It is still the same kind of object, owned by you, on the same volume, not locked and not an undownloaded cloud placeholder.
4. Its fingerprint matches the plan: for a file, inode, size, modification and creation time and a link count of one; for a folder, its inode, then the file count, allocated size and newest change found by a fresh walk.
5. Classified again with today's catalog it is the same rule, not protected, and the action is still allowed.
6. It is still at least as old as the plan's threshold, judged now.
7. No app the rule needs quit is running, and no process has it open or is executing from it.

Only then does it act, writing an intent line to the audit log before and an outcome line after. Skipped entries carry a reason in the summary, for example "Changed since the plan was made", "In use: Cursor is running" or "Newer than the age threshold now". Nothing is forced.

### Delete, archive, trash

Delete renames the item into a private staging folder inside the data directory, checks it there once more, and then unlinks it. Archive first streams the item into a compressed file (`.gz` for a file, `.tar.gz` for a folder) while hashing it, reads the archive back and verifies it, records it, and only then deletes the original. Session histories default to archive. Trash hands the item to `/usr/bin/trash`; it is the default for user files and leftovers.

Restore puts an archive back at its original path. It never overwrites an existing file, verifies the hash first, and restores the original mode and modification time.

```sh
disk-steward archives list
disk-steward archives restore ID
disk-steward archives purge ID     # deletes the archive for good
```

The Archives page of the portal does the same. Safety details and the threat model are in [docs/safety.md](docs/safety.md).

## AI advisor

Off by default. When you turn it on (`disk-steward advise --enable`, or the Settings page), it asks a local `codex` or `claude` CLI to comment on your groups. It uses the login those CLIs already have; no API key is read or stored. The advisor only suggests: it attaches a verdict (likely safe, review, keep) and a short reason to a group. It never selects anything, creates a plan, or changes a file.

What leaves your Mac is a JSON payload of numbers and catalog vocabulary: sizes, item counts, age buckets, growth and category names per group, under a random token per group. No path, file name, bundle id, project name or file content is in it. The payload goes to the provider behind the CLI you picked (OpenAI for Codex, Anthropic for Claude). Each CLI adds its own instructions around the payload (Codex may include your global `AGENTS.md`), which Disk Steward cannot show. Preview the payload first with `disk-steward advise --dry-run`, which prints it with the exact command and sends nothing. Field list, command lines, validation rules and the manual export and import are in [docs/ai-advisor.md](docs/ai-advisor.md).

```sh
disk-steward advise --enable
disk-steward advise --dry-run --provider claude
disk-steward advise --provider claude
```

## Data, schedule, uninstall

Data lives in `~/Library/Application Support/Disk Steward`: the SQLite database, archives, the audit log, settings and the lock. Use `--data-dir PATH` or `DISK_STEWARD_DATA_DIR` to put it elsewhere. The only data that leaves the Mac is the advisor payload above, and only when you turn the advisor on. The database stays small: a finished cleanup keeps a summary plus its skipped, failed and left-out entries, the per-item rows of everything it cleaned are deleted seven days after it finished, and free pages go back to the disk. The audit log keeps every removed path.

`disk-steward schedule install` adds a LaunchAgent that runs a scan every Monday at 10:00 (`--weekday 0-7`, `--hour 0-23`) and posts a notification with what could be cleaned and what grew. It never plans or deletes. `schedule status` shows the last log lines, `schedule remove` removes it. A job installed from a `.pyz` points at that file, so keep the file where it is.

To uninstall:

```sh
disk-steward schedule remove               # only if you installed the weekly scan
sh install.sh --uninstall                  # if you used install.sh; or: pip uninstall, pipx uninstall
rm -r "$HOME/Library/Application Support/Disk Steward"
```

`install.sh --uninstall` stops while the weekly scan still points at the installed `.pyz`. The data directory holds your archives: restore the ones you want back before deleting it. Delete a source checkout or a loose `.pyz` like any other folder or file.

## Limitations

- Items moved to the Trash have no restore in Disk Steward. Use Finder's Put Back. The space comes back only when you empty the Trash.
- Spotlight's last-used date changes only when LaunchServices opens a file (Finder, `open`). A file used from a terminal or a script can look idle. This affects installers, large files and idle files, which are review items that default to Trash.
- APFS clones and local snapshots can keep removed space allocated, so the space freed can be smaller than the estimate. The run summary shows the estimate, the measured change and the number of local snapshots. Archive estimates assume 70 percent of the size comes back.
- Shared developer caches are report only. Run the native command the group shows.
- Browser caches are offered per profile, only while the browser is quit and the newest file in that cache is older than the filter.
- A writer that opens, appends and closes a file is invisible to `lsof`. That is why session and log rules have a one-day floor.
- The portal speaks plain HTTP on the loopback address. Other users on the same Mac cannot sign in without a launch link, but a process running as you can read the data directory.
- macOS only, and verified only on macOS 27.0.1 with Python 3.9.6 and 3.14.8. A scan of the default areas on the author's Mac, about 275,000 items, takes around 35 seconds.
- The advisor needs a `codex` or `claude` build that accepts the flags listed in [docs/ai-advisor.md](docs/ai-advisor.md). The Claude probe names a missing flag.

## Support

Questions, bugs, and rules that miss or misjudge something: open an issue at https://github.com/arthaszyb/agent-junk-cleaner/issues.

Put these in the issue:

- the output of `disk-steward doctor` (version, Python, macOS, data directory, and whether `codex`, `claude` and `/usr/bin/trash` are found);
- what you did and what you expected;
- for a wrong scan result, the group name and the rule shown in the portal. Issues are public: leave out file contents and any path you would rather not publish.

If a cleanup removed something it should not have, stop cleaning and keep the data directory as it is. Open an issue and quote the lines that concern it from `~/Library/Application Support/Disk Steward/audit/`: the audit log lists every removed path with its plan and time. An archived item comes back with `disk-steward archives list` and `archives restore ID`; an item moved to the Trash with Finder's Put Back.

For a security problem, follow [SECURITY.md](SECURITY.md).

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, tests and how to add a catalog rule, [docs/architecture.md](docs/architecture.md) for the module contracts, [SECURITY.md](SECURITY.md) for reporting, and [CHANGELOG.md](CHANGELOG.md) for what changed from 0.3.0, the predecessor `ai-agent-disk-tool`.

MIT licensed; see [LICENSE](LICENSE).

## 简体中文

Disk Steward 是 macOS 磁盘清理工具，专门处理 AI 编程代理（Codex、Claude Code、Cursor 等）留下的日志、缓存、更新残留、旧版本、会话历史和虚拟机镜像，也覆盖常见的 Mac 垃圾。版本 0.4.0（beta），MIT 许可（见 [LICENSE](LICENSE)）。只支持 macOS，Python 3.9 及以上，只用标准库。只在一台 Mac 上验证过（macOS 27.0.1，Python 3.9.6 和 3.14.8），请在自己看着的时候使用。

2 分钟安装：

1. 从[发布页](https://github.com/arthaszyb/agent-junk-cleaner/releases)下载 `disk-steward-0.4.0.pyz` 和 `install.sh`。
2. 在 Terminal 里运行：

```sh
sh ~/Downloads/install.sh ~/Downloads/disk-steward-0.4.0.pyz
disk-steward portal --open       # 浏览器界面，终端会打印一次性链接（2 分钟内有效）
disk-steward scan                # 命令行：scan, report, list, plan, show, apply, archives
```

安装脚本把文件放到 `~/.local/share/disk-steward/`，并在 `~/.local/bin/disk-steward` 写一个启动脚本。它不使用 sudo，不会覆盖不是它写的同名文件；`~/.local/bin` 不在 PATH 里时，会打印要添加的那一行。想先试用、不安装，直接运行：`python3 ~/Downloads/disk-steward-0.4.0.pyz portal --open`。更新时用新的 `.pyz` 再运行一次安装脚本。卸载：`sh install.sh --uninstall`，只删除启动脚本和 `.pyz`，数据保留，命令会打印删除数据的方法。

浏览器下载的文件带有隔离标记。`python3 xxx.pyz` 的方式不会被 Gatekeeper 拦截；双击 `Open Disk Steward.command`（在源码目录里）可能被拦截，请右键选择“打开”并确认，或者直接用命令行。

它不会做的事：

- 自己不发起任何网络请求：没有遥测、没有更新检查、没有账号。门户只通过 `127.0.0.1` 与你的浏览器通信。两个可选功能会启动由你控制的程序：AI 顾问（默认关闭）调用你本机已登录的 `codex` 或 `claude`；“运行”按钮在你确认完整命令后，运行你自己的 `brew`、`pip`、`ollama` 等。
- 没有你审阅并开始的清理清单，不会删除任何东西。唯一的例外是“运行”按钮启动的工具自带清理命令，命令全文会先显示给你确认。
- 只处理审计过的目录里有规则的东西。无法识别的一律受保护，凭据、数据库、git 工作区在所有区域都受保护。
- 以你自己的身份运行，不用 sudo，不需要管理员权限。

安全模型：

- 只有目录里有规则、且你明确选中的东西才会被清理。规则分三层：junk（可重建，可批量选）、review（需要你判断，按组或按文件选）、protected（凭据、数据库、git 工作区、设置、你自己的内容，永远不可选）。
- `agent` 和 `app-library` 默认开启；`user-files` 需要手动开启；`dev-shared` 只报告，给出各工具自带的清理命令。
- 年龄过滤默认 14 天。规则自带最低年龄（例如日志和会话文件 1 天），过滤设置和“Include anyway”都不能低于它。
- 门户的概览顶部显示“可安全清理 X”（默认至少 14 天未变动的可重建垃圾，天数可直接改）。点“查看并清理 X”进入数字相同的清理清单，可按分组或单项取消，开始按钮始终显示剩余数量与大小；清理结果就在同一页显示。概览上的每个数字都能点进“浏览”，看到的正是该数字对应的项目。
- 仅展示的工具（Homebrew、pip、uv、npm、Ollama 模型等）若有官方清理命令，会出现“运行”按钮：先显示并确认完整命令，由工具自己清理，前后测量释放的空间；命令行用 `disk-steward tools run RULE_ID`。
- “洞察”页只读展示磁盘环境、扫描和清理历史、增长最多的分组与 AI 代理、带链接的待办；命令行用 `disk-steward insights`。清理完成 7 天后，逐项明细会从数据库删除（保留汇总，审计日志保留完整记录）。
- 创建计划时，服务端会按你看到的选择重新计算：结果不同返回 409 并列出差异，没有可清理项返回 422。计划 24 小时过期；规则目录或影响分类的设置变化后整份计划拒绝执行。检测事实（当前 CLI 版本、已安装应用等）逐条重新判断，判断结果变了的条目保留不动，执行前的预检会列出数量。
- 执行时逐条重新检查：父目录无符号链接、指纹一致、按当前规则重新分类、年龄、相关应用未在运行且文件未被打开。任何一项不满足就跳过并写明原因，绝不强行执行。
- 删除先改名进入私有暂存目录再确认；归档先压缩并校验，成功后才删除原文件；恢复永远不会覆盖已有文件。移入废纸篓没有软件内恢复，请用 Finder 的“放回原处”。

AI 顾问默认关闭，调用本机已登录的 `codex` 或 `claude` CLI，不需要 API key。Disk Steward 发送的只有数字和目录词汇（大小、数量、年龄分布、增长、类别名，每组一个随机 token），不含路径、文件名、应用 ID 或文件内容；这些数据会发给 CLI 背后的服务商。各 CLI 会在外面加上自己的指令（Codex 可能带上你的全局 AGENTS.md），Disk Steward 无法显示这部分。顾问只给建议，不会选择、创建计划或改动任何文件。`disk-steward advise --dry-run` 可以先查看将要发送的内容。

数据保存在 `~/Library/Application Support/Disk Steward`（可用 `--data-dir` 或 `DISK_STEWARD_DATA_DIR` 修改），其中包含归档。`disk-steward schedule install` 安装每周一次的只扫描、只通知的 LaunchAgent。卸载：如果装过每周扫描，先运行 `disk-steward schedule remove`（否则 `install.sh --uninstall` 会拒绝执行）；再运行 `sh install.sh --uninstall`（或 `pip uninstall disk-steward`）；最后删除数据目录（先恢复需要的归档）。

已知限制：废纸篓无软件内恢复；Spotlight 的“上次使用”只在 LaunchServices 打开文件时更新；APFS 克隆和本地快照会让实际释放空间小于估算；开发者共享缓存只报告；浏览器缓存仅在浏览器退出且缓存早于过滤期时才会被提供；未在其他 macOS 版本上验证。

支持：问题和 bug 提到 https://github.com/arthaszyb/agent-junk-cleaner/issues 。请附上 `disk-steward doctor` 的输出。如果清理误删了东西，先停止清理，保持数据目录原样，提交 issue，并引用 `~/Library/Application Support/Disk Steward/audit/` 审计日志中相关的行，它记录了每个被删除的路径。issue 是公开的，不要贴文件内容。安全问题见 [SECURITY.md](SECURITY.md)。
