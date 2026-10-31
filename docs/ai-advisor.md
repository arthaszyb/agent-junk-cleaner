# AI advisor

The advisor is optional and off by default. It asks a language model which of your groups look safe to clean, and shows the answer as a badge and a short reason next to each group. It only suggests. No code path leads from the advisor to your selection, to a plan, or to a file, and the reply is used only after it passes the checks below.

```sh
disk-steward advise --enable                      # or: Settings, Enable the AI advisor
disk-steward advise --dry-run --provider claude   # print the exact payload and command; send nothing
disk-steward advise --provider claude             # run it
disk-steward advise --disable
```

Providers are `codex` and `claude`, which run the local CLI of that name with the login it already has (no API key is read or stored by Disk Steward), and `manual`, which hands the prompt to you. `--provider` can be left out when the `advisor_provider` setting names one.

## What is sent

One JSON object, built from the latest scan, at most 48 KiB. Groups are sorted by size, and the largest ones that fit are included. `coverage` says how much of the scan they stand for.

```text
format                      1
coverage                    groups_included, groups_total, alloc_included, alloc_total
scan
  domains                   the areas that were scanned
  totals.by_tier            junk | review | protected -> alloc, reclaim, items, groups
  totals.by_domain          area -> alloc, reclaim, items, groups
  volume                    total, used, free (bytes)
  previous_scan_age_days    number or null
groups[]                    one row per group
  token                     "g" plus 8 random hex digits, new for every request
  domain, tier, kind        catalog categories
  provider                  the product that writes the data (codex, claude, cursor, macos, devtools, ...)
  label_key                 the catalog key of the category name, never its text
  actions                   what the tool could do with the group; empty means report only
  has_hint                  true when the group is report only and has a native clean-up command
  items, alloc, reclaim     counts and bytes
  ages                      lt7, d7_14, d14_30, d30_90, d90_180, gt180 -> [items, bytes]
  growth                    {delta_alloc, new} against the previous scan, or null
  evidence                  catalog evidence keys, without their arguments
  protected                 true for protected groups
```

Group rows are built from this allowlist of fields, so a new field in the database cannot leak into a request.

What the payload never holds: a path, a file or folder name, a bundle id, a project name, the value inside a group label (the path in "Codex sessions (~/.codex/sessions/2026/08)"), an evidence argument such as a version, your user name, any file content, your selection or plans, your settings. The map from tokens back to groups stays on your Mac. For a manual export it is saved in `steward.db` so that a later `advise --import` can decode the reply; it is never sent.

The payload still tells the provider which tools are installed and how large their data is. It goes to the service behind the CLI you pick: for `codex` the service that Codex is signed in to, for `claude` Anthropic. `--dry-run` and the portal's preview show the payload exactly as it would be sent. Disk Steward's own instructions are in `src/disk_steward/advisor/prompts.py` and tell the model to answer only with the schema, never to recommend commands or paths, and never to call a protected group safe.

Each CLI adds instructions of its own around that. Codex may include your global `AGENTS.md` and its built-in instructions. Claude runs in safe mode, which skips `CLAUDE.md`, plugins and MCP servers, but it still adds its built-in prompt. Disk Steward cannot show those parts, and the preview and `--dry-run` say so: only the JSON payload shown there comes from Disk Steward.

## The commands

The temporary files below live in a temporary directory that is removed after the call. The working directory of the child process is fixed at `<data dir>/advisor-cwd`, which is private (mode 0700), verified empty before every call and emptied after it, so the agent's per-directory records cannot grow with the number of calls.

Codex, with the prompt (instructions followed by the payload) on stdin:

```text
codex exec --ephemeral --sandbox read-only --skip-git-repo-check --ignore-user-config --ignore-rules
  --disable shell_tool --disable unified_exec --disable apps --disable plugins --disable browser_use
  --disable computer_use --disable in_app_browser --disable multi_agent --disable image_generation
  --disable view_image --disable memories --disable hooks --disable tool_suggest --disable skill_search
  --disable code_mode_host --disable goals
  -C <data dir>/advisor-cwd --output-schema <tmp>/schema.json -o <tmp>/reply.json -
```

The `--disable` flags are the ones this Codex build lists in `codex features list`. If `shell_tool` or `unified_exec` is not listed, the advisor refuses to run, because a read-only sandbox still lets a shell read the disk.

Claude, with the payload on stdin and the instructions in `--system-prompt`:

```text
claude -p --tools '' --safe-mode --strict-mcp-config --no-session-persistence --permission-prompts none
  --output-format json --json-schema '<reply schema>' --system-prompt '<instructions>'
```

While the advisor is off no CLI is run at all, and the providers show as unavailable with the reason "disabled". When it is on, the portal probes both CLIs side by side and reuses an answer for 30 seconds (10 seconds after a failure). The Claude probe runs `claude --help` and refuses to run if any of these flags is missing: `--print`, `--tools`, `--safe-mode`, `--strict-mcp-config`, `--no-session-persistence`, `--permission-prompts`, `--output-format`, `--json-schema`, `--system-prompt`.

The child runs in its own process group with your environment, because the login needs `HOME` and friends. The environment is never logged. A call is stopped after 180 seconds, and a stdout, stderr or reply file over 1 MiB stops it at once. The whole process group is killed when the call ends. Nothing in argv carries your data.

## Leftover sessions

The whole point of this tool is to remove session bloat, so the advisor checks that its own calls leave none. After every call, including one that failed:

- Codex: new files under `sessions/` and `archived_sessions/` of `$CODEX_HOME` (default `~/.codex`) are read, and any whose first line names the advisor's working directory is a failure. So is growth of `history.jsonl` or `session_index.jsonl` by text that names it.
- Claude: anything that appeared or changed under `projects/<encoded working directory>` of `$CLAUDE_CONFIG_DIR` (default `~/.claude`) is a failure. A mention of the working directory in `~/.claude.json` is not: with a fixed directory it is a single entry, so it is recorded as a trace and shown with the advice.

A failure raises an error that names the path and the reply is discarded. The advisor does not delete what it finds; the path is yours to remove, and Disk Steward's own session rules will list it.

## Validation

A reply is parsed and checked before anything is stored or shown:

- It is one JSON object with exactly the keys `summary` and `recommendations`. Duplicate keys, `NaN` and `Infinity` are refused. For Codex the whole output file is the object. For Claude it is the `structured_output` of the JSON envelope, and an envelope that reports an error is a CLI failure, not a bad reply. A manual reply may be bare JSON or exactly one fenced `json` block with nothing around it.
- `summary` is a string of at most 1,500 characters. There are at most 60 recommendations, each with exactly `group`, `verdict` and `reason`.
- `group` is a token of this request and appears once. A token the request does not hold is refused.
- `verdict` is `likely_safe`, `review` or `keep`, and `likely_safe` on a protected group is refused.
- `reason` is at most 400 characters. Strings may contain newlines and tabs but no other control character and no invisible formatting character (bidi overrides, zero-width marks, the byte order mark, tag characters), because the text reaches terminals and web pages and could disguise what it says.
- The request must be live: a preview is valid for one hour, at most five are kept, and one call runs at a time.

The checked tokens are mapped back to group keys and labels, and the advice is stored in the `advice` table with the scan id, provider, CLI version and a digest of the payload.

## How results are shown

In the portal, the AI advisor page has two steps, Preview what would be sent and Ask (Codex or Claude, each with its version or the reason it is unavailable). Below them are the manual path (Show the prompt, Paste the reply, Import reply) and the advice itself, and the Browse page puts the verdict and reason on each group the advisor named. From the CLI, `advise` prints the summary and, for each group, the verdict, the group's label and the reason, followed by "Suggestions only — nothing was selected or changed." Advice is shown only for the latest scan. If a new scan finished while a call was running, the result is marked stale. Switching the advisor off clears pending requests and the manual export, and an answer that arrives after the switch is discarded.

## Manual export and import

For any chat you trust, with no CLI involved:

```sh
disk-steward advise --enable
disk-steward advise --export prompt.txt     # instructions, the payload and the reply schema; valid for one hour
# paste prompt.txt into a chat, save the model's reply as reply.json
disk-steward advise --import reply.json     # validates against the latest export
```

The portal does the same with Show the prompt and Paste the reply. The reply goes through the same validation, and an import without a live export is refused.

## Time and cost

Measured on the author's Mac with `codex-cli 0.160.0` and Claude Code 2.1.299, a few runs each:

| Provider | Time per call | Cost |
|---|---|---|
| Codex | about 37 seconds | counts against your Codex plan |
| Claude | about 10 seconds | about $0.02 to $0.03, as reported by the CLI |

A dry run costs nothing and sends nothing, but it does run the CLI's `--version` and `--help` (or `features list`) to build the command.
