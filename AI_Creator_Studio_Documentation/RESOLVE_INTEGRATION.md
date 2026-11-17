# DaVinci Resolve Integration — Compatibility and MCP Record

**Status date:** 2026-11-18 (sections 10-15 added by task `T-RSV-002`; sections 1-9 are the 2026-11-08 record, corrected where marked)  
**Target:** DaVinci Resolve Free 21.0.4.5 on Windows

Sections 10-15 describe the safe execution layer `orchestrator/resolve_executor/` and what was **measured live** on
2026-11-18 with synthetic media on disposable projects (live evidence `E-RSV-LIVE-0001`). "VERIFIED" in a capability
table below means *exercised live by that suite with readback agreeing*; it is not the milestone label VERIFIED, which
needs real input and a human review.

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
| Source-range append/trim | PARTIAL, now EXPLAINED | 100-frame nominal range yielded a 95-frame timeline item. **Correction (2026-11-18, read-only inspection):** the test timeline `MCP_TRIM_TEST` runs at **24 fps**, not 25, and `MVI_0879.MOV` is 25 fps. Its item reads source `[0, ...)`, duration 95 = floor(99 × 24/25), consistent with an `endFrame` of 99 under the exclusive-end rule; `[0, 100)` gives 96. Reproduced with synthetic media (section 11). |
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

**Update 2026-11-18:** the allowlist was read, not changed: the bridge's `allowed_output_roots` is still only the
Movies folder named above (which did not exist and was created as a folder inside that root for the live suite).
The allowlist is enforced by the MCP server's tools, **not** by the bridge's raw `call` operation, so
`resolve_executor` enforces its own output and media allowlists (`validation.Policy`) and refuses anything outside
them. Exporting into `CreatorStudio\Projects` still needs a deliberate decision; nothing was broadened.

## 9a. Not yet tested (historical list of 2026-11-08; superseded by section 15)

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

The 2026-11-08 test order (inspection, append, frame semantics, tracks, transform, text, audio, render, 9:16,
recovery) was executed by `T-RSV-002`; results are in sections 11-14. Do not edit valuable footage until the
remaining gaps in section 15 are understood.

## 10. Safe execution layer (`orchestrator/resolve_executor/`, task T-RSV-002)

Two ways reach Resolve, for different callers:

| Path | Caller | Use |
|---|---|---|
| Claude Code → MCP server (37 tools, `{action, params}`) → bridge | an interactive LLM session | inspection, experiments, one-off work |
| pipeline code → `resolve_executor` → bridge | the Editor/orchestrator | **every planned edit**: typed, validated, journaled, read back |

The executor never exposes the raw API: an Editor submits typed requests (`operations.py`), `validation.py` checks
them without touching Resolve, the `Executor` refuses unsupported capabilities, checks the open project and the
timeline's rate, checks preconditions from readback (media in the pool exactly once, range inside the file, media
part present, tracks exist, slot free), writes a journal `begin` (fsynced) before the mutation, then reads the result
back and only reports `SUCCEEDED` when the readback equals the request. An interrupted mutation is never repeated:
its idempotency key stays blocked until `resolve_uncertain` records what an inspection found. Renders are verified
from the file with ffprobe. Package documentation: `orchestrator/resolve_executor/README.md`.

The live adapter speaks the bridge's protocol 1.0 itself (HMAC-SHA256 over the canonical request, one connection per
request); the token is read from the machine-local bridge config at run time and is never logged, returned or
stored. Nothing under `tools/` and no machine-local configuration was changed.

## 11. Frame semantics (measured live 2026-11-18, synthetic media)

| Question | Answer | How it was established |
|---|---|---|
| Are `AppendToTimeline` `startFrame`/`endFrame` inclusive? | **Half-open** `[startFrame, endFrame)`; length `endFrame - startFrame` | `[0,100)`→100, `[0,1)`→1, `[99,100)`→1, `[180,200)`→20, `[37,62)`→25 frames; a render decoded frame by frame (luma encodes the source index) contains exactly source frames 0-99, 0, 99, 180-199, 37-61 |
| What are source frames counted from? | The first frame of the file (frame 0), not its timecode | same render |
| How are timeline positions expressed? | Absolute frames including the start frame (90000 = 01:00:00:00 at 25 fps; 0 with a 00:00:00:00 start); `GetEnd()`, `GetEndFrame()` exclusive | readback; zero-start timeline with an explicit gap |
| Different source and timeline rates? | `floor(n × timeline_rate / source_rate)` timeline frames | 100 frames of 50/29.97/24 fps in 25 fps → 50/83/104; 25 fps in 23.976 → 95; 25 fps in 24 → 96 |
| The historical 95/100 | A 25 fps clip in a **24 fps** timeline with `endFrame` 99: floor(99 × 24/25) = 95 | read-only inspection of `MCP_TRIM_TEST`; reproduced: `[0,100)`→96, `[0,99)`→95 |
| Does the MCP adapter change values? | Not involved: the executor talks to the bridge directly; the bridge passes arguments unchanged | protocol source reading; results above |
| Is duration readback reliable? | `GetStart`/`GetEnd`/`GetDuration` yes. **`GetSourceEndFrame` no**: it read 100 for `[0,100)` but 199 for `[180,200)` and 39 for a 40-frame title; it is recorded, never used to verify | readback |
| Range past the end of the file | **Accepted silently**: `[190,210)` of a 200-frame file became a 20-frame item whose last 10 frames freeze frame 199 | render decoded; the executor refuses such ranges |
| Overlapping placement | `AppendToTimeline` **returns an item but places nothing** | track readback; the executor refuses overlaps before sending |
| Render range | `{"SelectAllFrames": False}` alone is rejected; `MarkIn`/`MarkOut` alone set a range; **`MarkOut` is inclusive** (all frames of `[90000, 90125)` reads 90000-90124) | queued-job readback; a `[90010, 90035)` render has 25 frames |
| Timeline frame rate | `Timeline.SetSetting("timelineFrameRate")` accepts **integer rates only** (24/25/30/48/50/60). NTSC rates come only from the project rate, settable only while the project has no timelines; new timelines inherit it | probe in disposable projects |
| Audio-only clips (WAV) | `Frames` is empty and `FPS` is the project rate at import; the frame count is the `Duration` timecode at that rate | media pool readback; 192 frames at 24 fps placed as 200 at 25 |

Conversions in code use `timebase.py` (integers and `Fraction`; floats refused). There is no constant correction
anywhere.

## 12. Capability inventory (Resolve Free 21.0.4.5 through the in-app bridge)

The machine-readable inventory, with API calls, inputs, limitations and verification method, is
`orchestrator/resolve_executor/capabilities.py`. Summary:

| Area | Status | Notes |
|---|---|---|
| Project inspection, media pool inspection, create project, save project | VERIFIED | save returns a bool only; no save-state readback exists |
| Import media | VERIFIED | lands in the current bin; allowlisted paths only; never copies or moves |
| Timeline creation and inspection | VERIFIED | NTSC via the project rate (section 11) |
| Track creation (video, audio mono/stereo) | VERIFIED | subtitle tracks refused |
| Clip insertion, positioning, source trimming, gaps, multitrack, video+audio sync | VERIFIED | half-open, floor rule, overlap and past-end refused |
| Transform and crop (Zoom, Pan, Tilt, Rotation, Crop, Opacity, Flip) | VERIFIED | read back exactly; visual effect not pixel-checked; Pan/Tilt units are not pixels for clips narrower than the timeline (third-party measurement) |
| Markers | VERIFIED | relative frames; colour, name, note, duration read back |
| Titles/text (Text+ via a nested timeline, Size, Center) | VERIFIED | exact track/frame/duration and text read back; font/colour styling not implemented; max 5 s per title |
| Music and sound-effect placement on audio tracks | VERIFIED | placement only |
| Render configuration, monitoring, timeout stop, export verification (MP4 H.264/H.265, 1920×1080, 1080×1920, with/without audio, range) | VERIFIED | file checked with ffprobe: codec, size, rate, exact frame count, audio stream |
| Remove a wrong placement (`DeleteItems`, checkpoint first) | VERIFIED | timeline items only |
| Clip/track volume, fades, EQ, noise reduction | UNSUPPORTED | confirmed live: `SetProperty('Volume'/'Level'/'AudioVolume')` returns False; no audio-level methods |
| Transitions | UNSUPPORTED | no `AddTransition` on 21.0.4.5 (21.1 has it; Free 21.1 has no Python scripting) |
| Subtitle-track captions | UNSUPPORTED (deliberately not tested) | third-party reports of a Resolve abort on subtitle appends; burn-in titles instead |
| Insert/overwrite/ripple edit modes | UNSUPPORTED | no API; positioned appends only |
| Render failure reported by Resolve itself | IMPLEMENTED, not observed live | no way found to make Resolve fail a job on synthetic media; covered offline |

## 13. Recovery behaviour

Pre-operation inspection, journal `begin`/`finish` records (append-only JSONL, fsynced), automatic checkpoint
(project save + timeline snapshot) before destructive requests and on demand, failure classes with recovery
recommendations (`errors.py`), bounded retries only when readback proves nothing changed, and a blocked idempotency
key after any unknown outcome. Resolve has no scripted undo: recovery is readback plus explicit removal of wrong
items. Live: an unreachable bridge is classified `bridge_unavailable`; an executor expecting another project refuses
to mutate the open one; a render stopped on timeout leaves Resolve not rendering within the wait and the partial
file is reported. Rule kept from third-party measurements: never close or delete a project while it renders.

## 14. Live evidence `E-RSV-LIVE-0001`

`python -B -m resolve_executor.live_suite --out <folder inside the bridge's allowed output root>` on 2026-11-18
against Resolve Free 21.0.4.5 (version read live), run `run_20261118_002837`: **26 of 26 checks passed** on the final
code. Synthetic media only (ffmpeg colour/sine sources); new projects `CS_T-RSV-002_<stamp>` and `..._ntsc`; no
real footage, no `MVI_0879.MOV` (read only as metadata of the historical timeline), no change to the bridge
configuration; the previously open project was reopened afterwards. The evidence file, journal, checkpoints and
renders stay on the local machine (not in the repository). Earlier runs on 2026-11-18 failed four checks; each
failure was a real Resolve behaviour (NTSC rate rule, WAV frame count, title timeline rate, render range keys) and
was fixed in code before the final run.

## 15. Still not tested or not possible

- Real footage of any kind (no permission sought; not needed for these semantics).
- Variable-frame-rate sources, drop-frame timecode (refused by `timebase`), 48/60 fps timelines, very long timelines.
- Visual correctness of transforms and titles beyond readback (no pixel comparison except the frame-index render).
- A render failure reported by Resolve itself, and bridge restart in the middle of a mutation (covered offline).
- Linking of separately appended video and audio parts (they are placed in sync but are not linked clips).
- Text+ font, colour, background and safe-area styling; caption placement beyond a burn-in title.
- Proxy workflows, relinking, retiming, multicam, compound clips, Fusion beyond Text+.
- Rendering into `CreatorStudio\Projects` (needs a deliberate allowlist decision).
- OmniRoute with the MCP; Resolve 21.1 (Free 21.1 drops Python scripting).
