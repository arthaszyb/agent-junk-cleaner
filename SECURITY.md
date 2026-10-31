# Security

Disk Steward 0.4.0 is a beta release for macOS. Only the latest release gets fixes.

## Reporting a vulnerability

Report it privately through GitHub's private vulnerability reporting: https://github.com/arthaszyb/agent-junk-cleaner/security/advisories/new. Do not put file paths, scan results, archives, audit logs or screenshots of your Mac in a public issue.

Include the version (`disk-steward --version`), the macOS version, and the steps. A failing test or a short script helps most.

## What counts

In scope:

- Removing, overwriting or exposing something the catalog and your selection did not allow: a protected item offered or deleted, a path outside the scanned roots touched, a plan that runs after its rules changed.
- A race between plan and run that lets a replaced file, folder or symlink be deleted or archived.
- A restore that writes outside its destination or overwrites a file.
- A request to the portal, from a web page or another local user, that gets past the launch link, the token, the Host and Origin checks or the custom header.
- The advisor sending anything beyond the documented payload (`docs/ai-advisor.md`), or acting on a reply that failed validation.
- A native tool command (`disk-steward tools run`, the portal's Run button) that is started with an argument, a model name or a path the fixed registry does not produce, without the user having confirmed that exact command text, or through a shell.

Out of scope:

- A malicious process running as you or as root. It can already read your files, the data directory and the portal's launch secret.
- Space that stays allocated after a delete because of APFS snapshots or clones.
- Harm from confirming a plan you did not read.
- Bugs in macOS, Python, `codex`, `claude` or your browser.

## The portal's model

The portal is a local web server for one user.

- It listens on `127.0.0.1` only, on a random port unless you pass `--port`. Every request must carry `Host: 127.0.0.1:<port>` or `localhost:<port>`; anything else gets 421, which stops DNS rebinding.
- `disk-steward portal` prints a launch link. Its code is valid for 120 seconds, works once, and is compared in constant time. The page trades the code for a session token, which it keeps in `localStorage` and sends in the header `X-Disk-Steward-Token`. Tokens live in memory and end when the server stops.
- The token is not a cookie, on purpose: a cookie for 127.0.0.1 would go to every other server on that address, whatever its port. `localStorage` belongs to the portal's own origin, which includes the port. The server is plain HTTP on loopback.
- Every API call needs the token. A POST or PUT also needs `Origin` equal to the portal's own origin, the header `X-Disk-Steward: 1` (a header a cross-site form cannot send), `Content-Type: application/json` and a JSON object body of at most 1 MiB. The server sends no CORS headers. At most 32 requests are handled at once.
- Pages carry a strict Content-Security-Policy (`script-src 'self'`, no inline script or style, `frame-ancestors 'none'`).
- `disk-steward portal --url` asks the running server for a new code through `portal.json` in the data directory (mode 0600), which holds a secret. Whoever can read that file can mint launch codes, so a local attacker running as you is outside the model.
- The browser never sends a list of paths to delete. It sends selection operations; the server resolves them and the executor checks every entry again at run time.

## Data handling

Everything stays on your Mac. Disk Steward makes no network calls of its own: no telemetry, no update check, no account. The portal listens on the loopback address only.

- The data directory (`~/Library/Application Support/Disk Steward`, or `--data-dir`) holds the SQLite database, archives, the audit log, settings and the lock. The tool creates its folders with mode 0700 and its files with mode 0600.
- The database and the audit log hold logical paths (`~/...`) of what was scanned and removed.
- An archive holds the full content of the file or folder it replaced, including session histories with your prompts and code. Treat the data directory as sensitive, and purge archives you no longer need.
- With the advisor off, nothing leaves the machine. The advisor is opt-in and uses the login of your own `codex` or `claude` CLI; Disk Steward reads and stores no API key. With it on, the payload described in `docs/ai-advisor.md` goes to the provider behind the CLI you chose, and the manual export writes the same payload to a file you pick. The payload has no paths, names or file content. The CLI adds its own instructions around it (Codex may include your global `AGENTS.md`), and Disk Steward cannot show or control those.
- **Native tool commands.** For report-only tools Disk Steward can start your own copy of the tool's cleanup command, after you confirm the exact command text (`brew cleanup --prune=all`, `uv cache prune`, `ollama rm MODEL`, and so on). The commands are a fixed registry of argv lists in `native.py`, keyed by rule id; nothing in a request, a scan or an advisor reply can add one or change an argument. The only free input is an Ollama model name, which must equal a name that `ollama list` shows at that moment and is passed as one argv element, never through a shell. A run must be given back the exact command text the user was shown (else `error.native_command_changed`). The tool is looked up on `PATH` when the run starts, not when it was listed, and runs in a new empty temporary folder with your environment, no stdin, in its own process group, for at most 15 minutes. Tools such as `rm -rf` and `npx` (which may download code) are shown but never run from here. The tool does its own deleting, so these runs are not covered by the plan, selection or fingerprint checks; the audit log records an intent line before and an outcome line after, with the resolved path of the tool.
- `diagnose-session` reads a Codex session file and reports counts, byte sizes, event type labels and timestamps only.

## Safe use

Read the plan before you confirm it. Quit the apps a plan names. Keep a backup of what you cannot replace. Do not run `apply --yes` from a script until you have seen the same plan interactively. `docs/safety.md` lists what is enforced, what is tested and what is not.
