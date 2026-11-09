# Incoming — where new footage goes

Put the files for **one video project per folder**:

```text
CreatorStudio\Incoming\
  mushroom-grow-test\          <- one folder per video project (this name becomes the project slug)
    camera\                    <- camera footage            (optional subfolders; any other name = "unsorted")
    phone\                     <- phone footage
    screen\                    <- screen recordings
    audio\                     <- voice-overs, music, sound you recorded
    images\                    <- photos, screenshots, graphics
    notes\                     <- .txt / .md notes, .srt subtitles
```

Subfolders are optional. Loose files directly in the project folder are catalogued as "unsorted".
Recognised subfolder names: `camera`, `phone`, `screen` (also `screen_recordings`), `audio`, `images`
(also `photos`), `notes`. They only label the source; nothing else depends on them.

## What the system does with these files

- **Nothing is moved, renamed, copied or deleted.** Files stay exactly where you put them and are
  recorded by absolute path (an *inventory*), with size, checksum and ffprobe metadata when available.
- Everything generated (inventory, report, proxies) goes to `Projects\<slug>\`, never here.
- Once a project is ingested, **do not rename or move these files** or the inventory will report them missing.
  (It will tell you; it will not fix it for you.)
- Footage can also live **outside** CreatorStudio (another drive, a camera card copy):
  `python -m creator_state ingest "D:\Footage\trip"` records those paths in place.

## Commands (run in `CreatorStudio\orchestrator`)

```powershell
python -m creator_state incoming-list                       # which folders are waiting
python -m creator_state incoming-inspect mushroom-grow-test # dry run, changes nothing
python -m creator_state ingest mushroom-grow-test           # catalogue into Projects\mushroom-grow-test
python -m creator_state inventory mushroom-grow-test        # browse the inventory
python -m creator_state verify-sources mushroom-grow-test --deep   # are the originals still intact?
```

Folders whose names start with `_` or `.` are ignored by `incoming-list`. See
`AI_Creator_Studio_Documentation\INGESTION.md` for details.
