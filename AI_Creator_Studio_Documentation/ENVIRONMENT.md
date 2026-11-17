# Development Environment

**Status:** facts marked VERIFIED were observed on 2026-11-09 on the development machine. REPORTED means a project document says so
and it was not re-observed. PROPOSED steps for a second machine have never been run end to end. **No secrets belong in this file.**
Exact tool versions and the pinned Resolve MCP release live in `TOOLS.md` (one place, to avoid drift).

## 1. Machine and shells

| Item | Fact | Label |
|---|---|---|
| OS | Windows 11 Pro, build 10.0.26200 | VERIFIED |
| Editor | VS Code; its integrated terminal runs Windows PowerShell 5.1 | VERIFIED (running terminal processes) |
| Project root | `C:\Users\Korisnik\CreatorStudio` (machine-specific) | VERIFIED |
| Task worktrees | `C:\Users\Korisnik\CreatorStudio-worktrees\<slug>` (see `PROJECT_OPERATING_PROTOCOL.md`) | PROCEDURE |
| Time zone | UTC+01:00; timestamps in `ACTIVE_TASKS.json` carry explicit offsets | VERIFIED |
| Line endings | Working files are CRLF; the repository stores LF (`.gitattributes`: `* text=auto`), so Git prints harmless "LF will be replaced by CRLF" notices | VERIFIED |

PowerShell 5.1 specifics: no `&&`/`||` chaining (use `;` or `if ($?) { ... }`), and `2>&1` on native programs wraps stderr lines as errors.
Inside Claude Code, the `!` prefix runs the command in **Git Bash**, not PowerShell (a PowerShell-style `& "path"` fails there); use POSIX syntax
and forward slashes (`/c/Users/...`) after `!`.

## 2. Python and tests

- Python 3.14.5 (user install). The Resolve MCP server uses its own virtual environment, also 3.14.5 (VERIFIED).
- The project code in `orchestrator/` uses only the standard library; `pytest` is not installed and not needed. No dependency installation is required to run the tests.
- Full suite, from `orchestrator/`: `python -B -I -m unittest discover -s tests`. A single module: add `-p test_<name>.py`.
- Command-line tools (run **without** `-I`, from `orchestrator/`): `python -m creator_state <command>` and `python -B -m governance <command>`.
- Some tests use real `ffmpeg`/`ffprobe` (both on `PATH`, version in `TOOLS.md`) to generate and analyse small synthetic clips (REPORTED in `INGESTION.md`, `SHOT_DETECTION.md`).
- Latest recorded result: see `PROJECT_STATUS.json` → `baseline`. Do not copy a test count from any other document.

## 3. Claude Code and the two launchers

- Claude Code CLI (version in `TOOLS.md`), started from `CreatorStudio` so `CLAUDE.md` and the project `.mcp.json` load.
- Exactly two launchers exist, both git-ignored and **never** copied into the repository or its documents:
  - `pro.ps1` — starts Claude Code with the direct Anthropic login (Claude Pro).
  - `backup.ps1` — starts Claude Code through the OmniRoute gateway (fallback when Pro is unavailable).
  Do not add a third. They set Anthropic-style environment variables whose values are credentials. They are recreated by hand on a new machine.
- Provider switches are performed by a human starting the other launcher; `creator_state switch-provider` only records the fact.

## 4. OmniRoute

- Local gateway at `http://localhost:20128` (REPORTED, `MASTER.md` section 10). Provider keys (free Gemini and Groq tiers) are held by OmniRoute on this machine and are not in the repository.
- The fallback launcher has not been tested end to end with the Resolve MCP (open item in `ROADMAP.md`).

## 5. DaVinci Resolve and the MCP

| Item | Fact | Label |
|---|---|---|
| Edition | DaVinci Resolve **Free 21.0.4.5**; install under `C:\Program Files\Blackmagic Design\DaVinci Resolve\` | VERIFIED |
| Compatibility | Only this version is a tested target. Free 21.1 may no longer list Python scripts (a statement in the MCP repository, not a test of this install) | REPORTED |
| Access path | External scripting is a Studio feature; Free is reached through the in-app bridge script `resolve_bridge` (Workspace > Scripts). Claude Code to local MCP server to loopback bridge to Resolve | REPORTED |
| Bridge | Listens on loopback `127.0.0.1:49632` only; keep it local. Config lives under the user profile in `.config\davinci-resolve-mcp\bridge.json` | REPORTED |
| MCP server | Third-party `davinci-resolve-mcp`, pinned in `TOOLS.md`, installed under `tools\davinci-resolve-mcp\` with its own `venv` | VERIFIED (install present, clean checkout) |
| Registration | Root `.mcp.json` (machine-specific, git-ignored). `/mcp` lists `davinci-resolve`; 37 tools reported, not all tested | REPORTED |
| Forcing the bridge | Do not set `DAVINCI_RESOLVE_BRIDGE=1` by default; it disables automatic fallback | REPORTED |
| Concurrency | The bridge is single-threaded: Resolve mutations are serialized (exclusive resource `resolve`, protocol section 6) | PROCEDURE |

`.mcp.json` fields (see `.mcp.json.example` for the tracked template with `<PLACEHOLDER>` values): `command` (the venv's `python.exe`), `args`
(`src\server.py`), and four environment variables — `RESOLVE_SCRIPT_API` (Resolve's `Support\Developer\Scripting` folder under `ProgramData`),
`RESOLVE_SCRIPT_LIB` (`fusionscript.dll` in the Resolve install folder), `PYTHONPATH` (that folder's `Modules`), and `PYTHONHOME` (the Python 3.14 install the MCP venv is based on).
All four are machine-specific paths, not credentials.

Resolve safety rules live in `CLAUDE.md` and `RESOLVE_INTEGRATION.md`: validated plan and recovery path before any mutation; originals immutable; read-only inspection is always fine.

## 6. Git and GitHub

- Git, GitHub CLI (`gh`, installed with `winget install --id GitHub.cli --scope user`; a shell opened before the install may not have it on `PATH` until restarted) and repository-local identity (`user.name`, `user.email` set per repository, global config untouched).
- Authentication: `gh auth login --hostname github.com --git-protocol https --web` (device code in the browser). The token is kept in the Windows keyring; scopes granted: `repo`, `read:org`, `gist`. HTTPS pushes worked after login (VERIFIED); which credential helper served them was not inspected.
- Repository: `https://github.com/KasluBuilds/CreatorStudio`, public since 2026-11-17 (private before), default branch `main`. Branch protection is available now that the repository is public (VERIFIED 2026-11-17; it answered HTTP 403 while private) and is configured on `main`: see `PROJECT_OPERATING_PROTOCOL.md` section 7.
- What is deliberately not in the repository: footage (`Incoming/`, `test_media/`), derived per-project data (`Projects/`), `tools/`, launchers, `.mcp.json`, local settings, media and model files (`.gitignore`).

## 7. Setting up a second machine (PROPOSED, never exercised)

1. Install Git, Python 3.14, FFmpeg/FFprobe (versions in `TOOLS.md`), GitHub CLI, Claude Code, and DaVinci Resolve Free 21.0.4.5.
2. `gh auth login`, then clone `KasluBuilds/CreatorStudio`. Run `cd orchestrator && python -B -I -m unittest discover -s tests`; it should pass without any of the local-only files.
3. Install the Resolve MCP server at the pinned commit into `tools\davinci-resolve-mcp\` with its own venv, and the in-app bridge as that project documents.
4. Copy `.mcp.json.example` to `.mcp.json` and replace every `<PLACEHOLDER>` with the local path.
5. Create `pro.ps1` and `backup.ps1` by hand (credentials come from the owner, never from the repository). Create empty `Incoming\` and `Projects\` folders.
6. Verify with a read-only call (`/mcp` in Claude Code), then follow `CLAUDE.md`.
