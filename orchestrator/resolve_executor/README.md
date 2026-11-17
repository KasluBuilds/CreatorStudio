# resolve_executor — safe, validated DaVinci Resolve operations

Task `T-RSV-002`, milestone `M-RSV-02`. Standard library only. Status: **implemented; core operations exercised
live on Resolve Free 21.0.4.5 with synthetic media** (live evidence `E-RSV-LIVE-0001`). Nothing here has been used
on real footage, and nothing is wired into a project pipeline yet: the Editor Agent and its edit-plan schema
(`M-EDT-01`) do not exist.

## Why it exists

The LLM must never drive Resolve with arbitrary low-level API calls. An Editor produces **typed requests**; this
package validates them, executes only supported operations, reads the result back from Resolve, journals it, and
refuses or reports anything it cannot prove.

```
Editor plan ─▶ operations (typed, frozen) ─▶ validation (pure; never calls Resolve)
           ─▶ Executor: capability gate ─▶ idempotency journal ─▶ identity + preconditions (reads)
           ─▶ journal "begin" ─▶ adapter mutation ─▶ read back + verify ─▶ journal "finish"
                         │
                         ├─ live.LiveResolveAdapter ─▶ bridge.BridgeClient ─▶ in-app bridge 127.0.0.1 ─▶ Resolve
                         └─ fake.FakeResolve (offline tests, CI)
Renders: render.run_render ─▶ job readback ─▶ monitor ─▶ probe.ffprobe ─▶ export report
```

| Module | Role |
|---|---|
| `timebase.py` | Exact frame/time arithmetic (`Fraction`, integers). Half-open `FrameRange`, rate parsing, floor rescaling, timecode (non-drop only). |
| `operations.py` | The request vocabulary. There is no "call this method" request. |
| `validation.py` | Deterministic checks: identifiers, rates, ranges, transform keys and bounds, media and output allowlists (symlink-resolved), forbidden media names. Imports no adapter. |
| `capabilities.py` | Inventory with status VERIFIED / IMPLEMENTED / NOT_TESTED / UNSUPPORTED; the executor refuses the last two. Also the list of the 37 MCP tools. |
| `executor.py` | Lifecycle, preconditions, read-after-write checks, bounded retries, checkpoints, `resolve_uncertain`. |
| `render.py` | Render configuration, job readback, monitoring, timeout stop, file verification. |
| `probe.py` | ffprobe wrapper and comparison with the requested spec (fails closed without ffprobe). |
| `journal.py` | Append-only JSONL journal (fsynced `begin` before every mutation) and checkpoint snapshots. |
| `errors.py` | `FailureClass` and the recovery recommendation for each. |
| `adapter.py` | The narrow, data-only adapter interface. |
| `live.py`, `bridge.py` | Live adapter over the local bridge (HMAC-signed; token read from the machine-local config, held only in a closure, never logged or returned). |
| `fake.py` | In-memory simulation of the measured Resolve behaviour, with fault injection. |
| `live_suite.py` | The live integration suite (disposable project, synthetic media). |

## Using it

```python
from resolve_executor import *
from resolve_executor.live import LiveResolveAdapter
from resolve_executor.journal import Journal

adapter = LiveResolveAdapter.connect()                       # needs Workspace > Scripts > resolve_bridge running
policy = Policy(allowed_media_roots=(media_dir,), allowed_output_roots=(render_dir,),
                forbidden_media_names=("MVI_0879.MOV",))
ex = Executor(adapter, policy, Journal(journal_dir), expected_project="My project")
r = ex.execute(AppendSegment("seg-1", timeline="Main", media_path=clip, source=FrameRange(0, 100),
                             record_frame=90000, idempotency_key="plan42-seg1"))
if not r.ok:
    print(r.outcome, r.failure, r.message, r.recovery)
```

Outcomes: `SUCCEEDED` (readback shows exactly the request), `ALREADY_DONE` (idempotency key completed before;
nothing sent), `REFUSED` (nothing sent: validation, capability, identity, precondition, unresolved earlier attempt),
`FAILED` (sent; the resulting state is known from readback and is not the request), `UNCERTAIN` (sent; state could
not be read; the journal entry stays open and the key is blocked until `Executor.resolve_uncertain(key, applied,
note)` records what an inspection found).

Rules the executor enforces:

* An expected project must be set and open; a timeline's rate is read before any frame arithmetic.
* Media must already be in the media pool exactly once; the source range must lie inside the file; the media must
  have the requested video/audio part; the slot must be free (half-open overlap test); tracks must exist.
* A mutation whose effect is unknown is never repeated. A transport error is retried (once by default) only when
  the readback proves nothing changed.
* `DeleteItems` (the only destructive request; timeline items only) checkpoints first.
* Renders never overwrite, send every setting in its own payload, read the queued job back, judge completion by
  percentage and error (the JobStatus text is localized), stop on timeout, restore the page, and verify the FILE.

## Frame semantics (measured live, see `RESOLVE_INTEGRATION.md`)

* Source ranges are half-open `[startFrame, endFrame)` source frames from the first frame of the file:
  `[0, 100)` is 100 frames, `[99, 100)` is the 100th frame alone. Pixel-verified by decoding a render.
* Timeline positions are absolute frames including the start frame (90000 for 01:00:00:00 at 25 fps);
  `GetEnd()`/`GetEndFrame()` are exclusive.
* Mixed rates: a source range of `n` frames occupies `floor(n × timeline_rate / source_rate)` timeline frames.
* The historical "100 frames became 95" was a 25 fps clip in a 24 fps timeline with `endFrame = 99`
  (`floor(99 × 24/25) = 95`; `[0, 100)` gives 96). No constant correction exists anywhere.
* Render `MarkOut` is inclusive; the executor sends `mark_out - 1` for its half-open range.

## Tests

```
cd orchestrator
python -B -I -m unittest discover -s tests -p test_resolve_executor.py        # offline, CI
python -B -m resolve_executor.live_suite --out "<folder inside the bridge's allowed output roots>"   # live
```

The live suite creates new projects named `CS_T-RSV-002_<stamp>` (and `_ntsc`), writes only inside `--out`, uses
only ffmpeg-generated media, and leaves those projects in Resolve's project list (deleting them is left to a human).

## Not supported (explicit refusals)

Clip/track audio levels, fades, EQ, noise reduction (no API on 21.0), transitions (`AddTransition` exists only from
21.1, whose Free edition has no Python scripting), subtitle-track captions (reported to crash Resolve; burn-in
titles via `PlaceTitle` instead), insert/overwrite/ripple edits (no API). See `capabilities.py`.
