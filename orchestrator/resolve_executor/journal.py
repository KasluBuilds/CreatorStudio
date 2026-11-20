"""Append-only operation journal and checkpoints.

One JSON object per line. A mutation writes `begin` (with the pre-state) BEFORE anything is sent to Resolve and
`finish` (with the readback evidence) after verification, each flushed and fsynced. A `begin` without a `finish` is
therefore the durable mark of an attempt whose effect is unknown; the executor refuses to repeat it until someone
records what actually happened (`resolved`). The journal never contains credentials: requests and readback only.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from typing import Any, Callable, Dict, Iterator, List, Optional

JOURNAL_FILE = "resolve_operations.jsonl"
SCHEMA = "resolve_executor.journal/1"


def _utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Journal:
    def __init__(self, directory: str, *, now: Callable[[], str] = _utc) -> None:
        self.directory = directory
        self.path = os.path.join(directory, JOURNAL_FILE)
        self.now = now
        os.makedirs(directory, exist_ok=True)
        self._seq = sum(1 for _ in self.records())

    def records(self) -> Iterator[Dict[str, Any]]:
        if not os.path.isfile(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as fh:
            for n, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    # A torn last line (power loss mid-write) is reported, never silently skipped.
                    yield {"event": "corrupt", "line": n}
                    continue
                if isinstance(rec, dict):
                    yield rec

    def append(self, event: str, **fields: Any) -> Dict[str, Any]:
        self._seq += 1
        rec = {"schema": SCHEMA, "seq": self._seq, "time": self.now(), "event": event, **fields}
        line = json.dumps(rec, sort_keys=True, ensure_ascii=False, default=str)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return rec

    # ── idempotency ──
    def state_of(self, key: str) -> Dict[str, Any]:
        """{"state": "new" | "done" | "failed" | "uncertain", "record": last relevant record}.

        done: a finish with outcome SUCCEEDED, or a `resolved` with applied=True.
        uncertain: a begin with no later finish/resolved.
        failed: a finish with any other outcome, or resolved applied=False (may be attempted again).
        """
        state, last = "new", None
        for rec in self.records():
            if rec.get("key") != key:
                continue
            ev = rec.get("event")
            if ev == "begin":
                state, last = "uncertain", rec
            elif ev == "finish":
                state = "done" if rec.get("outcome") == "SUCCEEDED" else "failed"
                last = rec
            elif ev == "resolved":
                state = "done" if rec.get("applied") else "failed"
                last = rec
        return {"state": state, "record": last}

    def uncertain_keys(self) -> List[str]:
        open_keys: Dict[str, bool] = {}
        for rec in self.records():
            k = rec.get("key")
            if not k:
                continue
            if rec.get("event") == "begin":
                open_keys[k] = True
            elif rec.get("event") in ("finish", "resolved"):
                open_keys[k] = False
        return [k for k, v in open_keys.items() if v]

    def corrupt_lines(self) -> List[int]:
        return [r["line"] for r in self.records() if r.get("event") == "corrupt"]

    def write_checkpoint(self, label: str, snapshot: Dict[str, Any]) -> str:
        folder = os.path.join(self.directory, "checkpoints")
        os.makedirs(folder, exist_ok=True)
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in label)[:60]
        path = os.path.join(folder, f"{self._seq + 1:05d}_{safe}.json")
        with open(path, "x", encoding="utf-8") as fh:
            json.dump(snapshot, fh, indent=2, sort_keys=True, default=str)
        self.append("checkpoint", label=label, file=os.path.basename(path))
        return path


class MemoryJournal(Journal):
    """Same semantics, nothing on disk (tests and dry runs)."""

    def __init__(self, *, now: Callable[[], str] = _utc) -> None:
        self.directory = ""
        self.path = ""
        self.now = now
        self._seq = 0
        self._records: List[Dict[str, Any]] = []
        self.checkpoints: Dict[str, Dict[str, Any]] = {}

    def records(self) -> Iterator[Dict[str, Any]]:
        yield from list(self._records)

    def append(self, event: str, **fields: Any) -> Dict[str, Any]:
        self._seq += 1
        rec = json.loads(json.dumps({"schema": SCHEMA, "seq": self._seq, "time": self.now(), "event": event, **fields},
                                    default=str))
        self._records.append(rec)
        return rec

    def write_checkpoint(self, label: str, snapshot: Dict[str, Any]) -> str:
        name = f"{self._seq + 1:05d}_{label}"
        self.checkpoints[name] = json.loads(json.dumps(snapshot, default=str))
        self.append("checkpoint", label=label, file=name)
        return name
