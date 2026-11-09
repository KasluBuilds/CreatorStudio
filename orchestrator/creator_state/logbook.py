"""Activity log: one JSON line per event (machine) and a readable text line (human)."""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import util
from .redact import redact_obj, redact_text


class ActivityLog:
    def __init__(self, project_dir: Path):
        self.dir = Path(project_dir) / "logs"
        self.jsonl = self.dir / "activity.jsonl"
        self.text = self.dir / "activity.log"

    def event(self, kind: str, message: str, level: str = "INFO", stage: str | None = None, **data):
        """Append an event. Never raises: logging must not break a production step."""
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            ts = util.utcnow()
            rec = redact_obj({"ts": ts, "level": level, "kind": kind, "stage": stage,
                              "message": message, "data": data})
            line = json.dumps(rec, ensure_ascii=False)
            human = f"{ts}  {level:<5}  {kind:<22} " + (f"[{stage}] " if stage else "") + \
                    redact_text(message).replace("\n", " ")
            for path, content in ((self.jsonl, line), (self.text, human)):
                with open(path, "a", encoding="utf-8", newline="\n") as f:
                    f.write(content + "\n")
                    f.flush()
                    os.fsync(f.fileno())
        except OSError:
            pass

    def tail(self, n: int = 15) -> list:
        try:
            lines = self.text.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        return lines[-n:]
