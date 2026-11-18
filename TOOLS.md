# TOOLS.md — external tools and pinned versions

Everything listed here is installed on the developer machine and is **not** stored in this repository.
No credentials appear in this file. Last observed: 2026-11-09 (labels: **VERIFIED** = observed
on that date by running the tool; **REPORTED** = taken from project documentation, not re-tested).

## Observed tool versions (VERIFIED 2026-11-09)

| Tool | Version | Notes |
|---|---|---|
| Windows | 11 Pro 10.0.26200 | Primary shell: Windows PowerShell 5.1 (VS Code integrated terminal) |
| Python | 3.14.5 | User install (`pythoncore-3.14-64`). The Resolve MCP venv also uses 3.14.5 |
| Git | 2.56.0.windows.4 | |
| GitHub CLI (`gh`) | 2.103.3 | Installed with winget (`GitHub.cli`, user scope); authenticated through the OS keyring |
| FFmpeg / ffprobe | 9.0.1 (gyan.dev full build) | Used by ingestion and shot detection |
| Node.js | 24.19.0 | |
| Claude Code | 2.1.316 | |
| DaVinci Resolve | Free 21.0.4.5 | Do not infer compatibility with other versions (see `AI_Creator_Studio_Documentation/RESOLVE_INTEGRATION.md`) |

The Python test suite uses only the standard library (`unittest`); `pytest` is not installed.

## Resolve MCP server (third-party, not vendored)

- Upstream: `https://github.com/samuelgursky/davinci-resolve-mcp` — pinned at tag **v4.19.3**,
  commit `04496e2d59ac95ea32694f0222ca3984ae88a450` (VERIFIED: clean checkout, 2026-11-09).
- Expected location: `tools/davinci-resolve-mcp/` with its own `venv/`. The whole `tools/` folder is git-ignored
  and must be reinstalled on a new machine; do not edit it without a clear reason.
- Registered for Claude Code through the root `.mcp.json`. That file holds machine-specific paths and is
  git-ignored; start from `.mcp.json.example` and replace each `<PLACEHOLDER>`.
- Free-edition access goes through the Resolve in-app bridge (loopback only, `127.0.0.1:49632` — REPORTED).
  Do not expose it to a network. `DAVINCI_RESOLVE_BRIDGE=1` is not forced by default.
- Claude Code shows the server as connected with 37 tools (REPORTED in `MASTER.md`); the 37 tools are not all tested.

## Claude Code launch profiles (contents intentionally not stored here)

Two launcher scripts exist in the project root and are **git-ignored because they contain credentials**:

- `pro.ps1` — direct Anthropic login (Claude Pro).
- `backup.ps1` — OmniRoute fallback gateway (local, `http://localhost:20128` — REPORTED).

- OmniRoute local gateway start (OBSERVED 2026-11-18, OmniRoute 3.8.49): `omniroute serve --daemon --no-open`; health needs a key (an unauthenticated `/api/health` returns HTTP 401). Keys stay in OmniRoute's own machine-local env file and are never printed or copied here.
- Headless Claude Code worker flags (OBSERVED once, Claude Code 2.1.316): `claude -p --output-format stream-json --verbose --model <alias> --session-id <uuid>` with the prompt on stdin exits 0 and emits init, assistant, rate_limit_event and result events; without `--verbose` stream-json exits 1; `--append-system-prompt-file` is accepted although `claude --help` does not list it.

Do not add a third launcher, and never copy launcher contents into the repository. A new machine must recreate
them by hand. The environment variables they set are Anthropic-style (`ANTHROPIC_BASE_URL`, `ANTHROPIC_MODEL`,
`ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY`); the values are secrets and stay local.

## What is not in the repository on purpose

`Incoming/` and `test_media/` (original footage), `Projects/` (footage-derived transcripts, audio, frames, state),
`tools/`, the launchers, `.mcp.json`, `.claude/settings.local.json`, and all media, model and cache files.
See `.gitignore`.
