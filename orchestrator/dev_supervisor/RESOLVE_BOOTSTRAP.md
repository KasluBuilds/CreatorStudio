# Resolve project discovery and bridge bootstrap (T-DEV-005)

Status labels: OBSERVED = seen in a command or read-only MCP reply during this lane (2026-11-18, Resolve Free 21.0.4.5, Windows).
REPORTED = stated by documentation or another session, not re-observed here. INFERRED = reasoning from the above.
NOT TESTED = not done. Nothing here is VERIFIED beyond the OBSERVED rows.

## Question

Why could an agent launch DaVinci Resolve yet not identify the intended project or get the in-app bridge?

## OBSERVED (read-only, this lane)

| Fact | Command / call |
|---|---|
| One `Resolve.exe` process was running with a GUI (not `-nogui`), one instance | `Get-Process` filter on name; MCP `resolve_control.runtime_mode` |
| TCP connect to `127.0.0.1:49632` succeeded (a listener exists). No bytes were sent. | `Test-NetConnection 127.0.0.1 -Port 49632` |
| The MCP server answered read-only calls through that path | `resolve_control.runtime_mode`, `project_manager.get_current`, `project_manager.list`, `timeline.get_name` |
| Database: `Local Database`, type `Disk` | `resolve_control.runtime_mode` |
| `project_manager.get_current` returns BOTH a name and a UUID-style `id` (so a stable identity exists; name alone is not needed) | same call |
| The current project was the T-RSV-002 disposable probe project (`CS_T-RSV-002_ntsc_probe`), not `MCP_COMPATIBILITY_TEST`; it had a current timeline | same calls |
| The project list held 7 projects, including `MCP_COMPATIBILITY_TEST` and two `CS_T-RSV-002_*` disposable projects | `project_manager.list` |
| `timeline.get_name` also returns the timeline unique id in its operation metadata | same call |
| The project folder is NOT exposed by these read calls | absent from every reply |
| Discovery CLI against the live machine, with a config expecting `MCP_COMPATIBILITY_TEST` + `Local Database`, gave verdict MISMATCH, exit 1; with no config, UNCONFIGURED, exit 2 | `python -B -m dev_supervisor.resolve_discovery` (MCP replies supplied via `--mcp-snapshot` because the CLI has no MCP client of its own) |

The in-app bridge is authenticated (a token lives in a machine-local config). This lane did not read that config and sent
nothing to the port, so what the bridge answers on a raw socket is NOT TESTED; only "a TCP listener accepts" is observed.

## INFERRED: why launching Resolve was not enough

1. Launching Resolve (`resolve_control.launch`, or starting the exe) only starts the application. On the Free edition external
   scripting is unavailable, so there is no connection until the user runs `Workspace > Scripts > resolve_bridge` inside Resolve and
   leaves it running (REPORTED in `RESOLVE_INTEGRATION.md` section 2 and the MCP README). An agent cannot click that menu.
2. Identity comes from whatever project is currently open. There is no project-level pin. Another task (T-RSV-002) legitimately
   changes the open project, so "the current project" is not a stable answer: today it is a disposable probe project. Without a
   configured expected identity, an agent that trusts `get_current` can operate on the wrong project. Name alone is weak: names are
   user-chosen and can repeat or be renamed.
3. REPORTED (memory note, 2026-11-17, via the coordinator): the user opens `MCP_COMPATIBILITY_TEST`, then runs `resolve_bridge`, and leaves it
   running. If the bridge script is stopped or Resolve restarts, the listener disappears and an agent only sees a connection error.
4. No machine-local identity config existed, so nothing could report "wrong project" explicitly.
5. Free 21.1 reportedly drops Python scripts from the Scripts menu (MCP README); on 21.1+ this path may be final. Resolve here is 21.0.4.5.

## What the code does about it

`resolve_discovery.py` (stdlib only, all transports injectable):
- checks process, loopback TCP connect only, and four whitelisted read-only MCP calls (listed in `READ_ONLY_CALLS`); anything else is refused in code;
- labels every field OBSERVED or UNAVAILABLE; folder is always UNAVAILABLE with a reason;
- compares against a machine-local config (path via `--config` or env `CREATORSTUDIO_RESOLVE_IDENTITY`, never committed):
  `{"project_id": "..."}` or `{"project_name": "...", "database": "..."}`. A name-only config is UNCONFIGURED. If an id is configured but
  not observable it reports UNAVAILABLE and does not fall back to name. `require_match()` raises on MISMATCH;
- CLI exit codes: 0 MATCH, 1 MISMATCH, 2 UNCONFIGURED, 3 UNAVAILABLE;
- `--start-bridge --resolve-exe <path>` only starts the Resolve application when it is not running; the bridge script itself cannot be started
  from outside, so the report always carries the manual steps.

## Manual fallback (what the user must do)

1. Open the intended project in Resolve.
2. `Workspace > Scripts > resolve_bridge`; leave the script running.
3. Create the local identity config with that project's `id` (take it from the discovery report) and point `CREATORSTUDIO_RESOLVE_IDENTITY` at it.
4. Re-run `python -B -m dev_supervisor.resolve_discovery`; proceed only on MATCH. While T-RSV-002 holds the `resolve` resource, do not switch projects.

## Not done / open

- The CLI has no MCP client; it uses an agent-supplied snapshot. A real transport (stdio to the MCP server or the authenticated bridge) is OPEN and not built.
- Project folder identity is not exposed by the calls used; a deeper read (folder tree) is NOT TESTED.
- Raw reply of the listener on 49632 is NOT TESTED (authenticated protocol).
