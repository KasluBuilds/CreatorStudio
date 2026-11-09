"""Analysis copies (e.g. a mono 16 kHz WAV for transcription / audio analysis).

Safety rules enforced here:
  * the original is only read (ffmpeg input); its size/mtime are re-checked after the run;
  * output goes to the registry directory, never beside the original, never over any
    existing file (ffmpeg -n, `.part` file, rename only after verification);
  * every copy is recorded with source identity, settings, tool and output checksum;
  * a valid copy is reused; a copy whose source changed is reported stale and is NOT
    deleted or overwritten - a new copy gets a new file name (source fingerprint is in it).

Video proxies for editing already exist in `creator_state.proxy`; this registry can
record such files with `register_existing` but does not generate them.
Nothing is created unless `ensure()` is called: there are no automatic proxies.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import wave
from pathlib import Path
from typing import Callable, Mapping

from .models import (CapabilityError, MediaAnalysisError, MediaRef, atomic_write_json, read_json,
                     stable_hash, utc_now)

REGISTRY_FILE = "analysis_copies.json"
SCHEMA = "media_analysis.copies/1"
DEFAULT_SETTINGS = {"audio_wav": {"sample_rate": 16000, "channels": 1}}
Runner = Callable[[list], tuple]


def default_runner(argv: list) -> tuple:
    cp = subprocess.run(argv, capture_output=True)
    return cp.returncode, cp.stdout.decode("utf-8", "replace"), cp.stderr.decode("utf-8", "replace")


def find_ffmpeg(explicit: str | None = None, which=shutil.which) -> str | None:
    return explicit or os.environ.get("CREATOR_FFMPEG") or which("ffmpeg")


def source_fingerprint(ref: MediaRef) -> str:
    return stable_hash(ref.sha256 or [ref.size, ref.mtime_ns], n=8)


def sha256_file(path: Path) -> str:
    from creator_state import util
    return util.sha256_file(path)


class AnalysisCopyRegistry:
    def __init__(self, directory: str | os.PathLike, ffmpeg: str | None = None,
                 runner: Runner = default_runner):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.registry_path = self.directory / REGISTRY_FILE
        self.copies_dir = self.directory / "copies"
        self._ffmpeg, self._runner = ffmpeg, runner
        self.records: list[dict] = []
        if self.registry_path.exists():
            data = read_json(self.registry_path)
            if data.get("schema") != SCHEMA or not isinstance(data.get("records"), list):
                raise MediaAnalysisError(f"{self.registry_path}: unrecognised registry")
            self.records = data["records"]

    # ----------------------------------------------------------------- planning
    @staticmethod
    def resolve_settings(kind: str, settings: Mapping | None) -> dict:
        if kind not in DEFAULT_SETTINGS:
            raise MediaAnalysisError(f"unknown analysis-copy kind {kind!r}; known: {sorted(DEFAULT_SETTINGS)}")
        merged = {**DEFAULT_SETTINGS[kind], **(settings or {})}
        extra = set(merged) - set(DEFAULT_SETTINGS[kind])
        if extra:
            raise MediaAnalysisError(f"unknown settings for {kind}: {sorted(extra)}")
        if not (isinstance(merged["sample_rate"], int) and 8000 <= merged["sample_rate"] <= 96000):
            raise MediaAnalysisError("sample_rate must be an integer within 8000..96000")
        if merged["channels"] not in (1, 2):
            raise MediaAnalysisError("channels must be 1 or 2")
        return merged

    def copy_id(self, ref: MediaRef, kind: str, settings: Mapping) -> str:
        return f"{ref.media_id}_{kind}_{stable_hash(dict(settings), n=8)}_{source_fingerprint(ref)}"

    def plan(self, ref: MediaRef, kind: str, settings: Mapping | None = None) -> dict:
        s = self.resolve_settings(kind, settings)
        cid = self.copy_id(ref, kind, s)
        return {"copy_id": cid, "kind": kind, "settings": s,
                "output": str(self.copies_dir / f"{cid}.wav"), "exists": self.find(cid) is not None}

    def find(self, copy_id: str) -> dict | None:
        for r in self.records:
            if r["copy_id"] == copy_id:
                return r
        return None

    # ------------------------------------------------------------------- checks
    def check(self, record: Mapping, deep: bool = False) -> dict:
        """state: valid | source_missing | stale_source | output_missing | output_changed"""
        src = record["source"]
        try:
            st = os.stat(src["path"])
        except OSError:
            return {"state": "source_missing", "reasons": ["original not found"]}
        reasons = []
        if src.get("size") is not None and src["size"] != st.st_size:
            reasons.append("source size changed")
        if src.get("mtime_ns") is not None and src["mtime_ns"] != st.st_mtime_ns:
            reasons.append("source modification time changed")
        if deep and src.get("sha256") and sha256_file(Path(src["path"])) != src["sha256"]:
            reasons.append("source sha256 changed")
        if reasons:
            return {"state": "stale_source", "reasons": reasons}
        out = self._output_path(record)
        try:
            ost = out.stat()
        except OSError:
            return {"state": "output_missing", "reasons": [f"{out} not found"]}
        if ost.st_size != record["output"]["size"] or (deep and sha256_file(out) != record["output"]["sha256"]):
            return {"state": "output_changed", "reasons": ["analysis copy differs from its recorded checksum/size"]}
        return {"state": "valid", "reasons": []}

    def _output_path(self, record: Mapping) -> Path:
        p = Path(record["output"]["path"])
        return p if p.is_absolute() else self.directory / p

    # --------------------------------------------------------------- generating
    def ensure(self, ref: MediaRef, kind: str = "audio_wav", settings: Mapping | None = None,
               deep: bool = False) -> tuple[dict, bool]:
        """Return (record, reused). Creates a copy only if no valid one exists."""
        plan = self.plan(ref, kind, settings)
        existing = self.find(plan["copy_id"])
        if existing and self.check(existing, deep)["state"] == "valid":
            return existing, True
        return self._generate(ref, kind, plan), False

    def _generate(self, ref: MediaRef, kind: str, plan: dict) -> dict:
        ffmpeg = find_ffmpeg(self._ffmpeg)
        if not ffmpeg:
            raise CapabilityError("ffmpeg is not available: cannot create analysis copies "
                                  "(install FFmpeg or set CREATOR_FFMPEG); nothing was created")
        src = Path(ref.path)
        try:
            before = src.stat()
        except OSError as exc:
            raise MediaAnalysisError(f"original not readable: {exc}") from exc
        if (ref.size is not None and ref.size != before.st_size) or \
           (ref.mtime_ns is not None and ref.mtime_ns != before.st_mtime_ns):
            raise MediaAnalysisError("original differs from the manifest (size/mtime); refusing to derive from it")
        out = Path(plan["output"])
        if out.resolve().parent == src.resolve().parent:
            raise MediaAnalysisError("analysis copies must not be written next to the original")
        if out.exists():
            raise MediaAnalysisError(f"{out} exists but is not registered; refusing to overwrite it")
        self.copies_dir.mkdir(parents=True, exist_ok=True)
        part = out.with_name(out.name + ".part")
        if part.exists():
            part.unlink()     # leftover of an interrupted run of ours
        s = plan["settings"]
        argv = [ffmpeg, "-hide_banner", "-nostdin", "-n", "-i", str(src), "-vn", "-ac", str(s["channels"]),
                "-ar", str(s["sample_rate"]), "-c:a", "pcm_s16le", "-f", "wav", str(part)]
        try:
            rc, _, err = self._runner(argv)
            if rc != 0:
                raise MediaAnalysisError(f"ffmpeg failed (exit {rc}): {err.strip()[-300:]}")
            self._verify_wav(part, s, ref.duration_s)
            after = src.stat()
            if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
                raise MediaAnalysisError("the original changed while the copy was being made")
            os.replace(part, out)
        except BaseException:
            if part.exists():
                part.unlink()
            raise
        rec = {"copy_id": plan["copy_id"], "kind": kind, "source": ref.to_dict(),
               "settings": s, "settings_hash": stable_hash(s, n=8),
               "output": {"path": str(out.relative_to(self.directory)), "size": out.stat().st_size,
                          "sha256": sha256_file(out)},
               "tool": {"name": "ffmpeg", "path": ffmpeg, "argv_template": argv[:-1] + ["<output>"]},
               "created_at": utc_now(), "external": False}
        self._append(rec)
        return rec

    @staticmethod
    def _verify_wav(path: Path, s: Mapping, src_duration: float | None) -> None:
        try:
            with wave.open(str(path), "rb") as w:
                rate, ch, n = w.getframerate(), w.getnchannels(), w.getnframes()
        except (wave.Error, EOFError, OSError) as exc:
            raise MediaAnalysisError(f"generated file is not a valid WAV: {exc}") from exc
        if rate != s["sample_rate"] or ch != s["channels"] or n == 0:
            raise MediaAnalysisError(f"generated WAV does not match settings (rate={rate}, channels={ch}, frames={n})")
        if src_duration:
            got = n / rate
            if abs(got - src_duration) > max(0.5, 0.02 * src_duration):
                raise MediaAnalysisError(f"generated duration {got:.2f}s differs from source {src_duration:.2f}s")

    def register_existing(self, ref: MediaRef, path: str | os.PathLike, kind: str, settings: Mapping,
                          tool: Mapping) -> dict:
        """Record a derived file produced elsewhere (e.g. a creator_state proxy). Not generated here."""
        p = Path(path).resolve()
        if not p.is_file() or p.stat().st_size == 0:
            raise MediaAnalysisError(f"{p} is missing or empty")
        if p == Path(ref.path).resolve():
            raise MediaAnalysisError("an analysis copy cannot be the original itself")
        cid = f"{ref.media_id}_{kind}_{stable_hash(dict(settings), n=8)}_{source_fingerprint(ref)}"
        if self.find(cid):
            raise MediaAnalysisError(f"{cid} is already registered")
        rec = {"copy_id": cid, "kind": kind, "source": ref.to_dict(), "settings": dict(settings),
               "settings_hash": stable_hash(dict(settings), n=8),
               "output": {"path": str(p), "size": p.stat().st_size, "sha256": sha256_file(p)},
               "tool": dict(tool), "created_at": utc_now(), "external": True}
        self._append(rec)
        return rec

    def _append(self, rec: dict) -> None:
        self.records.append(rec)
        atomic_write_json(self.registry_path, {"schema": SCHEMA, "records": self.records})

    def audit(self, deep: bool = False) -> list[dict]:
        return [{"copy_id": r["copy_id"], **self.check(r, deep)} for r in self.records]
