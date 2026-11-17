# Creator Studio Dashboard (Windows desktop app)

A **read-only** Windows application that shows how Creator Studio development is going: overall progress, the current wave
and agents, blockers, one roadmap tab per development agent, the global roadmap, and the first-real-video filming gate.
It reads the **public** GitHub repository `KasluBuilds/CreatorStudio` when you open it and whenever you press **Refresh**.
No GitHub login, no token, no account.

Status labels: **VERIFIED** (observed in testing), **NOT TESTED**.

## Install

1. Open <https://github.com/KasluBuilds/CreatorStudio/releases> and download `CreatorStudio-Dashboard-<version>-portable.exe`
   from the latest `dashboard-v…` release (version 1.1.0 added dark mode). Check it against `SHA256SUMS.txt` on the same page:
   `Get-FileHash .\CreatorStudio-Dashboard-<version>-portable.exe -Algorithm SHA256`.
2. Put it anywhere (Desktop, a folder) and double-click it. There is no installer and nothing to uninstall: delete the file.
3. Windows SmartScreen may say "Windows protected your PC" because the file is **not code-signed** (a signing certificate costs
   money and is an open decision for Lukas). Choose **More info → Run anyway** only if the SHA-256 matches the release page.

It needs Windows 10 or 11 (64-bit) and an internet connection to refresh. It keeps a saved copy of the last data, so it still
opens offline.

## What you see

| Where | What |
|---|---|
| **Header** | Last successful synchronization time, the **source commit** it came from (a link), a *Live / Saved copy / Offline* chip, the **Light / Dark / System** appearance switch and the **Refresh** button. |
| **Banners** | Red: GitHub cannot be reached (the saved copy is shown). Amber: the data is more than an hour old. Green, pulsing: the filming gate is ready. Blue: a newer dashboard exists, or notes about missing/odd data. |
| **Overview** | The big progress ring (click it for the global roadmap), current major milestone, current wave and its lanes, active agents, blockers and open decisions, upcoming milestones, progress by agent, and the filming gate card. |
| **One tab per agent** | The agent's role and streams, implementation/verification rings, its **current assignment**, its **next substantial task**, and its **winding road** with its milestones. Click a milestone for its checklist, dependencies, blockers, wave lanes assigned to it, and evidence with GitHub links. |
| **Global roadmap** | All milestones in dependency order, grouped by phase, with the responsible agent on every label. |
| **Filming gate** | Pending or ready, what fires it, the five readiness criteria, the recorded procedure, and the full `FIRST_REAL_VIDEO_CHECKLIST.md`. |

Road colours: green check = implemented, gold star ring = verified on real input, blue pulsing = in progress, red `!` = blocked,
gold ▶ = ready to start, dashed = waiting on dependencies. The lit green stretch of the road is how far that agent has travelled.

## Where the numbers come from (nothing is invented)

- **Global implementation and verification percentages** are copied from the `computed` block of `PROJECT_STATUS.json`. The app never
  replaces them. It also recalculates them with the same rules as `orchestrator/governance/status.py` and shows a data note if the two
  disagree (the data may be mid-update, or the app may be out of date).
- **Per-milestone, per-agent and per-phase percentages** use exactly the same credit rules (implementation: not started 0, in progress ½,
  implemented 1; verification: unit ¼, integration ½, real-input reviewed 0.9, repeated real use 1, never more than implementation) and the
  same weights. The Python test `orchestrator/tests/test_dashboard_app.py` checks this against the governance code on the current records.
- **Checklists** are derived from the milestone record: work started, implemented, each verification level reached, each dependency, each
  open question, each blocker. **Wave lanes** show their acceptance-criteria count and report status. If an agent has no milestones the
  tab says **untracked** and shows checklist counts instead of a percentage.
- **Agents** come from `AGENT_REGISTRY.json` (the one extra canonical file; it holds no progress, only who is responsible for which
  milestones and which Claude streams belong to which agent). A new `active` entry gets a tab on the next refresh with no code change; a `planned`
  entry (Editor, Resolve Executor, QC) gets none until it is made `active`. If the file is missing the app derives tabs from `package_owners` and says so.
- **Waves** come from `wave/*` branches (manifest and lane reports) and `lane/*` branches (a pushed lane branch means the lane is working).
- **The filming gate** is `G-FIRST-REAL-VIDEO` from `PROJECT_STATUS.json`, evaluated with the same rule as `governance validate`: it fires when a
  trigger milestone (`M-LFE-03`) reaches an implemented state. The app does not trigger, change or acknowledge it, and it does not replace the weekly
  ChatGPT "Phase 6 Readiness Check" automation (REPORTED by Lukas, not verified). When the gate becomes ready the app shows the green banner, flashes
  the taskbar, puts "ACTION REQUIRED" in the window title and shows one Windows notification; the banner stays until the record is acknowledged.
  This is the point where development-only work should pause so real videos can be filmed and validated.

## Appearance: Light, Dark or System

The **Light / Dark / System** switch in the header changes only the colours. It does not touch any data, progress figure, synchronization, agent tab, filming-gate behaviour or notification.

- **System** (the default on first launch) follows Windows: if you switch Windows between light and dark while the app is open, the app follows at once.
- Your choice is **saved** (`%APPDATA%\Creator Studio Dashboard\theme.json`) and restored the next time the app starts, with no flash of the wrong theme. Deleting that file returns to System.
- The whole window follows the choice: overview, global roadmap, every agent roadmap, cards, milestone checklists, evidence, banners, the road itself, and also the native scrollbars and title bar. Tooltips are the operating system's own and follow Windows, and Windows notifications (the filming-gate toast) are drawn by Windows. The app has no dialog boxes of its own.
- The dark theme is designed, not an inversion: deep blue-slate surfaces, softened status colours, a glowing travelled road. Light and dark each meet WCAG contrast (4.5:1 for text, 7:1 for body text on surfaces, 3:1 for icons), measured by tests on the palette values and again on the rendered window for every screen. Status is never colour alone: implemented, verified, in progress, blocked and ready each have their own glyph, and waiting milestones are dashed.

## Refresh, offline and stale data

- On open the app shows the saved copy (if any) at once, then refreshes. It refreshes again every 10 minutes while open, or when you press
  **Refresh** (or F5). One refresh reads everything at **one commit of `main`**, so the figures are consistent with each other.
- **Offline / GitHub unreachable:** red banner, *Offline* chip, the last saved data stays on screen with its sync time and source commit.
- **GitHub refuses (rate limit) or sends unusable data:** an error banner; a bad answer never replaces a good saved copy.
- **Stale:** data older than one hour gets an amber banner.
- Saved copy location: `%APPDATA%\Creator Studio Dashboard\last-snapshot.json`. Deleting it is harmless.

## Update

The app checks the public "latest release" when it refreshes and shows a blue banner when a newer `dashboard-v…` exists. Download the new `.exe`
from the release page and replace the old file. The app never updates itself and never downloads executables.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| "Windows protected your PC" | The exe is unsigned. Verify the SHA-256 against `SHA256SUMS.txt`, then More info → Run anyway. |
| Starts slowly the first time | The portable exe unpacks itself to a temporary folder on every start (a few seconds). |
| Red "Offline" banner but the internet works | A firewall or proxy may block `github.com` or `raw.githubusercontent.com`. Open <https://github.com/KasluBuilds/CreatorStudio> in a browser to check; the app uses your system proxy. |
| "GitHub refused the request (HTTP 403/429)" | Rate limit or network policy. Wait a few minutes and press Refresh; the saved copy stays usable. |
| Tabs look different from before ("derived from package owners") | `AGENT_REGISTRY.json` could not be read from GitHub; check the data notes. |
| A number differs from `PROJECT_STATUS.json` | It should not: the global figures are copied. If a "do not match" note appears, the repository was mid-update or the app is out of date. Refresh, then update the app. |
| Window shows "Waiting for data" | No saved copy and no connection yet. Connect and press Refresh. |
| Want a clean start | Close the app and delete `%APPDATA%\Creator Studio Dashboard`. |

## Safety

The app is read-only: it sends only HTTPS `GET`-style reads to `github.com` and `raw.githubusercontent.com`, has no login and holds no credential,
writes only its own saved copy, and cannot change any project record. The window is sandboxed with a strict content policy, no remote code, text
from the repository is only ever shown as text, and links open only this project's GitHub pages in your browser.

## Build, test, run from source

```powershell
cd dashboard\desktop
npm ci                          # electron + electron-builder (dev only)
node node_modules\electron\install.js   # if npm skipped Electron's download
npm test                        # 79 unit tests, Node only, no network
npm start                       # run the app
npm run dist                    # builds dist\CreatorStudio-Dashboard-<version>-portable.exe
```

Test the **packaged** exe (this is how releases are checked). It drives the real window through every screen, checks what is on screen against the data
it loaded, switches Light, Dark and System, audits the contrast of every visible text element on four screens in each theme, saves screenshots, writes `selftest-report.json` and exits 0 when everything passed. `--leave-theme` and `--expect-theme=dark` let two separate launches prove that the appearance choice persists:

```powershell
.\dist\CreatorStudio-Dashboard-1.1.0-portable.exe --selftest --userdata=C:\temp\ud --out=C:\temp\report            # live GitHub
.\dist\CreatorStudio-Dashboard-1.1.0-portable.exe --selftest --offline --userdata=C:\temp\ud --out=C:\temp\report2 # offline, uses the saved copy
.\dist\...portable.exe --selftest --fixture=C:\path\to\checkout --userdata=C:\temp\ud2 --out=C:\temp\report3        # local files, no network
```

The older static page `dashboard\index.html` and `dashboard\build_snapshot.py` (a scrubbed point-in-time snapshot) are unchanged and still work.

## Known limits

Windows only (NOT TESTED on macOS or Linux). Unsigned. No auto-update. The project-wide percentage inherits the caveat on the page: the weights are
proposals until Lukas reviews them, and implementation means code exists, not that it is verified. A lane's progress is only as fine-grained as its
report (`DONE`, `IN_PROGRESS`, `BLOCKED`) and whether it has pushed its branch.
