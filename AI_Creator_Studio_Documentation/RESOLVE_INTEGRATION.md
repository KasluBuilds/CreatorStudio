# DaVinci Resolve Integration — Compatibility and MCP Record

**Status date:** 2026-11-08  
**Target:** DaVinci Resolve Free 21.0.4.5 on Windows

## 1. Compatibility conclusion

The tests completed so far demonstrate that the Free edition can perform several essential scripted operations required by this project, including timeline creation, clip insertion, transform changes and automated MP4 export.

There is no evidence from these tests that the user needs Resolve Studio for the initial editing automation scope. Do not recommend purchasing Studio or downgrading based solely on the tests completed.

This is not proof that every Resolve API function, effect, codec or workflow is supported.

## 2. Free-edition scripting behavior

- No external scripting preference was found in Resolve Preferences; this is expected in the tested Free edition.
- Workspace → Scripts menu is available.
- Resolve runs scripts through `fuscript.exe` child process.
- The bridge probe reported child-process mode, blocking required, and that a daemon listener would terminate when the script exits.
- The in-app `resolve_bridge` script therefore intentionally holds the script execution open while listening.
- The user must leave the script running while the MCP server is using Resolve.
- Listener is local at `127.0.0.1:49632`. Do not expose it to a network.

## 3. Verified direct scripting tests

| Test | Result | Notes |
|---|---|---|
| Resolve object/product/version | PASS | Identified DaVinci Resolve 21.0.4.5 Free |
| Project/media-pool inspection | PASS | Read active project, root folder and 41 media pool clips |
| Timeline inspection | PASS | Timeline 1, 25 fps, start frame 90000 |
| Create timeline | PASS | Disposable test project/timeline |
| Insert marker | PASS | Marker inserted; requested Blue appeared gray, so marker color mapping needs checking |
| Append clip | PASS | `MVI_0879.MOV` appended to test timeline |
| Source-range append/trim | PARTIAL | 100-frame nominal range yielded 95-frame timeline item at 25 fps; semantics require further testing |
| Transform read/write | PASS | `ZoomX` read as 1.0, set to 1.5, read back as 1.5 |
| MP4/H.264 render | PASS | Minimal settings accepted, job created and started, resulting MP4 played normally |

### Minimal render settings that worked
```python
project.SetRenderSettings({
    "SelectAllFrames": True,
    "TargetDir": output_dir,
    "CustomName": "MCP_AUTOMATED_EXPORT_TEST",
    "ExportVideo": True,
    "ExportAudio": True
})
```
The project accepted the settings, `AddRenderJob()` returned a job ID, `StartRendering(job_id)` returned True, and the resulting file existed and played.

## 4. Render options observed

- MP4 format available.
- H.264 and H.265 codecs listed for MP4.
- YUV 4:2:2 10-bit option also appeared.
- YouTube 1080p/2160p and TikTok 720p/1080p presets were listed.
- First attempt with additional optional render settings returned False; minimal settings succeeded. Add optional settings incrementally and verify.

## 5. MCP repository and installation

Repository cloned:
`C:\Users\Korisnik\CreatorStudio\tools\davinci-resolve-mcp`

Repository:
`samuelgursky/davinci-resolve-mcp`

Installed components:
- Python virtual environment under repository `venv`.
- MCP SDK and Node dependencies installed by installer.
- Resolve bridge installed in user and system Fusion script directories.
- Bridge config at:
  `C:\Users\Korisnik\.config\davinci-resolve-mcp\bridge.json`
- Bridge host/port: `127.0.0.1:49632`.
- Config allowed media root: `C:\Users\Korisnik`.
- Config allowed output root at install time: `C:\Users\Korisnik\Movies`.
- Authentication clock skew: 60 seconds.

Installer command used:
```powershell
py install.py --clients claude-code
```
It reported:
- Resolve running.
- Python 3.14.5 64-bit.
- MCP SDK installed.
- Node dependencies installed.
- Claude Code configured.
- Connected: DaVinci Resolve 21.0.4.5.
- Setup complete.

The repository installer emitted an update-check network timeout, but installation and connection still completed.

## 6. MCP configuration

Root config:
`C:\Users\Korisnik\CreatorStudio\.mcp.json`

It launches:
`C:\Users\Korisnik\CreatorStudio\tools\davinci-resolve-mcp\venv\Scripts\python.exe`

with:
`C:\Users\Korisnik\CreatorStudio\tools\davinci-resolve-mcp\src\server.py`

Environment values include Resolve API/library/PYTHONPATH and:
`PYTHONHOME=C:\Users\Korisnik\AppData\Local\Python\pythoncore-3.14-64`

The same `.mcp.json` also exists inside the MCP repository. The root-level config is intended for Claude Code launched from CreatorStudio.

Claude Code trust prompt was accepted using “Use this and all future MCP servers in this project”.

**Latest confirmed result:** Claude Code `/mcp` displayed `davinci-resolve` connected with **37 tools**. The separate `claude.ai Claude Docs` MCP showed 8 tools.

This confirms discovery/connection and tool listing, not that every tool is functional.

## 7. Bridge transport selection

Source inspection in `src/server.py` and `src/utils/resolve_bridge_client.py` established:
- `DAVINCI_RESOLVE_BRIDGE=1` forces bridge-only mode and disables the alternative transport.
- Without that variable, direct transport can be tried and the bridge used as fallback when direct connection is unavailable.
- Do **not** set the force variable in the launchers by default.
- Bridge config defaults to `~/.config/davinci-resolve-mcp/bridge.json`.
- `DAVINCI_RESOLVE_BRIDGE_CONFIG` can override the config path.
- `PYTHONHOME` is used by server setup to help locate the Resolve native scripting runtime; do not remove it without a targeted test.

## 8. Current launcher setup

In CreatorStudio root:
- `pro.ps1`: clears gateway-related environment variables, sets `ANTHROPIC_MODEL=sonnet`, runs `claude`.
- `backup.ps1`: configures OmniRoute at `http://localhost:20128`, sets model `auto/coding`, runs `claude`.
- `.claudeignore` excludes launcher files.

Do not paste API tokens into chat. The backup launcher still needs a local valid OmniRoute key if not already configured. The OmniRoute fallback has not yet been end-to-end tested with the Resolve MCP.

## 9. Important security/configuration note

Bridge config initially allows output under `C:\Users\Korisnik\Movies`, while the desired project root is under `C:\Users\Korisnik\CreatorStudio`. Before using MCP exports into `CreatorStudio\Projects`, inspect the bridge's allowed-output-root enforcement and update the allowlist deliberately if required. Do not broaden access unnecessarily.

The media root already includes `C:\Users\Korisnik`, which contains CreatorStudio.

## 10. Not yet tested

- All 37 MCP tools individually.
- Timeline track management, insert/overwrite editing, clip positioning and precise trims through MCP.
- Audio levels, ducking, EQ, noise reduction and music mixing.
- Captions/subtitles and typography.
- Fusion titles, motion graphics and complex effects.
- Speed ramps, retiming and stabilization.
- Multicam and compound/nested timelines.
- Proxy workflows and relinking.
- Render progress/status polling, failure recovery and batch exports.
- Vertical reframing and platform-safe overlays.
- Reliability under long autonomous jobs.
- OmniRoute profile with the MCP.
- Exact frame-boundary semantics for source-range append.

## 11. Next compatibility tests

Use disposable projects and short test media. Test in this order:
1. Read-only project/timeline/media inspection through an MCP tool.
2. Timeline creation and clip append through MCP.
3. Exact source in/out frame behavior and duration readback.
4. Multiple tracks and audio/video alignment.
5. Clip transform and crop.
6. Text/caption workflow.
7. Audio adjustment and sound effect placement.
8. Render settings, progress, completion and file verification.
9. Vertical 9:16 export.
10. Recovery from failed operation and bridge restart behavior.

Do not edit valuable footage until these behaviors are understood.
