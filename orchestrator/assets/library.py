"""Asset library: index, search, safe storage, rights-gated state.

Layout under the library root (e.g. Assets/ for global, Projects/<slug>/assets/ for a project):
    index.json            all records (atomic write; refuses to overwrite a damaged index)
    files/<category>/<asset_id><ext>    managed copies only (created exclusively, never overwritten)

Guarantees: no delete API exists; originals are only read (hashed / copied), never moved or
changed; a new asset never overwrites an existing id or file; state `approved` can only be
reached through evaluate_rights() (there is no manual approve). Single-process use assumed
(no cross-process locking).
"""
from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Callable, Iterable, Optional

from .evaluation import AssetEvaluation, AssetRequest, TargetSpec, evaluate
from .models import (ATTEST_METHODS, AssetExistsError, AssetRecord, Attestation, DuplicateAssetError,
                     Evidence, ManifestError, RightsBasis, SCHEMA_VERSION, State, UnsafePathError,
                     Verification, check_asset_id, norm_tag, safe_suffix, sha256_file, PROJECT_RE,
                     path_problem)
from .rights import PERMITTED, REJECT, Reason, RightsDecision, UseContext, evaluate_rights, rights_fingerprint

_RIGHTS_FIELDS = {"rights_basis", "license_name", "license_url", "commercial_use_allowed", "modification_allowed",
                  "attribution_required", "attribution_text", "restrictions", "allowed_uses", "source",
                  "source_url", "creator", "watermark_status", "is_preview"}


_HOLD = "manual:"   # state_reason prefix of a hold (manual quarantine/reject or integrity failure)


def _is_link(p: Path) -> bool:
    """A symlink or (Python 3.12+) a Windows junction. Does not follow the link."""
    try:
        return p.is_symlink() or bool(getattr(p, "is_junction", lambda: False)())
    except OSError:
        return True   # cannot tell -> treat as unsafe


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class AssetLibrary:
    def __init__(self, root: Path | str, clock: Callable[[], str] = _now):
        self.root = Path(root).resolve()
        self.clock = clock
        self._records: dict[str, AssetRecord] = {}
        self._load()

    # ---- persistence ---------------------------------------------------------------------
    @property
    def index_path(self) -> Path:
        return self.root / "index.json"

    def _load(self) -> None:
        if not self.index_path.exists():
            return
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise ManifestError(f"index.json unreadable ({type(e).__name__}); not overwriting it")
        if not isinstance(data, dict) or type(data.get("schema_version")) is not int \
                or data.get("schema_version") != SCHEMA_VERSION \
                or not isinstance(data.get("assets"), list):
            raise ManifestError("index.json has an invalid structure or schema_version; not overwriting it")
        problems, recs = [], {}
        for i, raw in enumerate(data["assets"]):
            try:
                r = AssetRecord.from_dict(raw)
            except ManifestError as e:
                problems.append(f"assets[{i}]: {e}")
                continue
            if r.asset_id in recs:
                problems.append(f"assets[{i}]: duplicate asset_id {r.asset_id!r}")
            if r.checksum_sha256 and any(o.checksum_sha256 == r.checksum_sha256 for o in recs.values()):
                problems.append(f"assets[{i}]: duplicate content checksum")
            recs[r.asset_id] = r
        if problems:
            raise ManifestError(problems)
        # Never trust a stored 'approved': re-derive it. Loading can only DOWNGRADE, never upgrade,
        # so a hand-edited index (e.g. changed licence, forged state) cannot grant approval.
        for r in recs.values():
            if r.state is State.APPROVED:
                if r.state_reason.startswith(_HOLD):          # a hold never coexists with 'approved'
                    self._set_hold(r, r.state_reason[len(_HOLD):].strip() or "hold", "load_downgrade")
                    continue
                d = evaluate_rights(r)
                if not d.permitted:
                    self._apply_decision(r, d, "load_downgrade")
        self._records = recs

    def _save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": SCHEMA_VERSION,
                   "assets": [r.to_dict() for r in sorted(self._records.values(), key=lambda r: r.asset_id)]}
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".index-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.index_path)
        except BaseException:
            try:
                os.unlink(tmp)   # our own temp file only
            except OSError:
                pass
            raise

    # ---- helpers -------------------------------------------------------------------------
    def _log(self, rec: AssetRecord, event: str, **kw) -> None:
        rec.history.append({"at": self.clock(), "event": event, **kw})

    @staticmethod
    def _held(rec: AssetRecord) -> bool:
        return rec.state is not State.APPROVED and rec.state_reason.startswith(_HOLD)

    def _set_hold(self, rec: AssetRecord, reason: str, event: str, reject: bool = False) -> None:
        """Place or refresh a hold. A hold never lowers severity: a REJECTED asset stays REJECTED."""
        new = State.REJECTED if (reject or rec.state is State.REJECTED) else State.QUARANTINED
        text = f"{_HOLD} {reason}".strip()[:600]
        if new is not rec.state or text != rec.state_reason:
            self._log(rec, event, state_from=rec.state.value, state_to=new.value, reason=reason[:500],
                      previous_reason=rec.state_reason[:200])
        rec.state, rec.state_reason = new, text

    def _apply_decision(self, rec: AssetRecord, decision: RightsDecision, event: str) -> None:
        new = {PERMITTED: State.APPROVED, REJECT: State.REJECTED}.get(decision.verdict, State.QUARANTINED)
        if self._held(rec):
            # A hold persists through re-evaluation, evidence, attestation and rights updates; only
            # release_hold() lifts it. Evaluation may make a hold STRICTER (quarantine -> reject), never looser.
            if rec.state is State.QUARANTINED and new is State.REJECTED:
                self._log(rec, event, state_from="quarantined", state_to="rejected",
                          reason="; ".join(r.code for r in decision.reasons if r.severity != "info") + " (hold kept)")
                rec.state = State.REJECTED
            return
        reason = "; ".join(f"{r.code}" for r in decision.reasons if r.severity != "info") or "all rights checks passed"
        if new is not rec.state or reason != rec.state_reason:
            self._log(rec, event, state_from=rec.state.value, state_to=new.value, reason=reason)
        rec.state, rec.state_reason = new, reason

    def _must(self, asset_id: str) -> AssetRecord:
        try:
            return self._records[asset_id]
        except KeyError:
            raise KeyError(f"unknown asset {asset_id!r}") from None

    def _dest_for(self, rec: AssetRecord, src: Path) -> Path:
        dest = self.root / "files" / rec.category.value / (rec.asset_id + safe_suffix(src.name))
        self._plain_dirs(dest.parent)
        dest = dest.resolve()
        if self.root not in dest.parents:
            raise UnsafePathError("managed destination escapes the library root")
        return dest

    def _plain_dirs(self, d: Path) -> None:
        """No symlink/junction may sit between the library root and a managed directory."""
        cur = self.root
        for part in d.relative_to(self.root).parts:
            cur = cur / part
            if cur.is_symlink() or getattr(cur, "is_junction", lambda: False)():
                raise UnsafePathError(f"{part!r} is a symlink/junction inside the library; refusing to write through it")

    @staticmethod
    def _check_source_string(raw: str) -> None:
        """String-level checks BEFORE touching the filesystem (UNC would trigger network access)."""
        s = str(raw)
        if not s or any(ord(c) < 32 for c in s):
            raise UnsafePathError("empty path or control characters")
        if s.startswith(("\\\\", "//")):
            raise UnsafePathError("UNC / device paths are not accepted")
        if len(s) > 2 and ":" in s[2:]:
            raise UnsafePathError("alternate data streams / device syntax are not accepted")

    @staticmethod
    def _link_in_chain(path: Path) -> Optional[Path]:
        """First symlink/junction among the path and ALL of its parent directories, or None."""
        for p in (path, *path.parents):
            if p.parent == p:
                break
            if _is_link(p):
                return p
        return None

    def _file_problem(self, rec: AssetRecord) -> Optional[str]:
        """Why the indexed file cannot be relied on, or None. Checks path safety (no traversal, no
        symlink/junction on any component), existence and checksum. Never modifies anything."""
        if rec.file_path is None:
            return "no file recorded"
        if path_problem(rec.file_path, rec.managed_copy):
            return "unsafe path"
        try:
            if rec.managed_copy:
                full = self.root / rec.file_path
                self._plain_dirs(full.parent)
                if self.root not in full.resolve().parents:
                    return "managed path escapes library"
                if _is_link(full):
                    return "file is a symlink/junction"
            else:
                full = Path(rec.file_path)
                if self._link_in_chain(full) is not None:
                    return "symlink/junction on the file path"
            if not full.is_file():
                return "file missing"
            if sha256_file(full) != rec.checksum_sha256:
                return "checksum changed"
        except UnsafePathError:
            return "symlink/junction inside the library"
        except OSError:
            return "file unreadable"
        return None

    # ---- adding --------------------------------------------------------------------------
    def add(self, rec: AssetRecord, source_file: Path | str, mode: str = "reference",
            ctx: Optional[UseContext] = None) -> AssetRecord:
        """Index an asset. `reference` records the original's path; `copy` makes a managed copy
        (never for rejected assets). The original is only read. Raises AssetExistsError for a
        known id, DuplicateAssetError for known content; nothing is overwritten either way."""
        if mode not in ("reference", "copy"):
            raise ValueError("mode must be 'reference' or 'copy'")
        check_asset_id(rec.asset_id)
        if rec.asset_id in self._records:
            raise AssetExistsError(f"asset {rec.asset_id!r} already exists")
        # Re-validate through the strict loader: a record built in code can carry wrong types
        # (e.g. commercial_use_allowed="yes") that the dataclass constructor never checks.
        try:
            raw = rec.to_dict()
            raw["file_path"], raw["managed_copy"] = None, False   # set by the library from the real file
            clean = AssetRecord.from_dict(raw)
        except (AttributeError, TypeError, ValueError) as e:
            raise ManifestError(f"record is malformed ({type(e).__name__})") from None
        # Intake is untrusted: no pre-set state, verification or attestations may come in. Rights
        # must be (re)established through attest(). Imported attestations are kept as history only.
        imported = [dataclasses.asdict(a) for a in clean.attestations]
        clean.state, clean.state_reason = State.QUARANTINED, ""
        clean.verification_status, clean.attestations = Verification.UNVERIFIED, []
        rec = clean
        if imported:
            self._log(rec, "imported_attestations_discarded", count=len(imported), attestations=imported)
        for pr in rec.projects:
            if not PROJECT_RE.fullmatch(pr):
                raise ManifestError(f"invalid project slug {pr!r}")
        self._check_source_string(str(source_file))
        # Lexical normalisation only (no symlink resolution), then EVERY component is inspected, so the
        # path that is hashed and copied is exactly the path that was checked.
        norm = os.path.abspath(str(source_file))
        self._check_source_string(norm)
        src = Path(norm)
        link = self._link_in_chain(src)
        if link is not None:
            raise UnsafePathError(f"{link.name!r} on the source path is a symlink/junction; refused")
        if not src.is_file():
            raise UnsafePathError("source must be an existing regular file")
        digest = sha256_file(src)
        if rec.checksum_sha256 and rec.checksum_sha256 != digest:
            raise ManifestError("checksum_sha256 does not match the file")
        for other in self._records.values():
            if other.checksum_sha256 == digest:
                # Keep the second submission's provenance (history only: rights fields and the
                # existing verification are untouched). A rejected file cannot be re-submitted
                # under cleaner metadata to get in.
                self._log(other, "duplicate_submission", offered_id=rec.asset_id, source=rec.source,
                          source_url=rec.source_url, creator=rec.creator, license_name=rec.license_name,
                          rights_basis=rec.rights_basis.value)
                self._save()
                raise DuplicateAssetError(other.asset_id)
        rec.checksum_sha256, rec.file_size = digest, src.stat().st_size
        rec.file_path, rec.managed_copy = str(src), False
        self._log(rec, "added", mode=mode)
        self._apply_decision(rec, evaluate_rights(rec, ctx), "rights_evaluated")
        dest: Optional[Path] = None
        if mode == "copy" and rec.state is not State.REJECTED:
            dest = self._dest_for(rec, src)
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                with open(src, "rb") as fi, open(dest, "xb") as fo:   # 'x' = fail if it exists
                    shutil.copyfileobj(fi, fo, 1 << 20)
            except FileExistsError:
                raise AssetExistsError(f"destination file already exists: {dest.name}") from None
            if sha256_file(dest) != digest:
                dest.unlink()   # our own failed partial copy; never the original
                raise AssetExistsError("managed copy failed checksum verification; nothing indexed")
            rec.file_path, rec.managed_copy = dest.relative_to(self.root).as_posix(), True
        self._records[rec.asset_id] = rec
        try:
            self._save()
        except BaseException:
            del self._records[rec.asset_id]
            raise
        return copy.deepcopy(rec)

    # ---- rights lifecycle ----------------------------------------------------------------
    def add_evidence(self, asset_id: str, ev: Evidence, ctx: Optional[UseContext] = None) -> AssetRecord:
        rec = self._must(asset_id)
        problems: list[str] = []
        try:
            ev = Evidence.from_dict(dataclasses.asdict(ev), problems)
        except (TypeError, AttributeError):
            raise ManifestError("evidence is malformed") from None
        if problems:
            raise ManifestError(problems)
        rec.evidence.append(ev)
        rec.verification_status = Verification.UNVERIFIED   # a reviewer has not seen this evidence
        self._log(rec, "evidence_added", kind=ev.kind, strength=ev.strength)
        self._apply_decision(rec, evaluate_rights(rec, ctx), "rights_evaluated")
        self._save()
        return copy.deepcopy(rec)

    def attest(self, asset_id: str, verifier: str, method: str, note: str,
               ctx: Optional[UseContext] = None) -> AssetRecord:
        """Record that a NAMED human/user confirmed the rights facts. This is the only way an
        asset becomes `verified`; the library never verifies on its own."""
        if method not in ATTEST_METHODS:
            raise ValueError(f"method must be one of {ATTEST_METHODS}")
        if not verifier or not verifier.strip() or not note or not note.strip():
            raise ValueError("verifier and note are required")
        rec = self._must(asset_id)
        rec.attestations.append(Attestation(verifier.strip()[:200], method, note.strip()[:2000], self.clock(),
                                            rights_fingerprint(rec)))
        rec.verification_status = Verification.VERIFIED
        self._log(rec, "attested", verifier=verifier.strip()[:200], method=method)
        self._apply_decision(rec, evaluate_rights(rec, ctx), "rights_evaluated")
        self._save()
        return copy.deepcopy(rec)

    def update_rights(self, asset_id: str, /, ctx: Optional[UseContext] = None, **changes) -> AssetRecord:
        """Change rights-relevant fields. Verification is RESET (the attestation covered the old
        facts); prior attestations and evidence are kept as history."""
        bad = set(changes) - _RIGHTS_FIELDS
        if bad:
            raise ValueError(f"not rights fields: {sorted(bad)}")
        rec = self._must(asset_id)
        data = rec.to_dict()
        before = {k: data[k] for k in changes}
        data.update({k: (v.value if hasattr(v, "value") else v) for k, v in changes.items()})
        new = AssetRecord.from_dict(data)   # full strict validation of the change
        for k in changes:
            setattr(rec, k, getattr(new, k))
        rec.verification_status = Verification.UNVERIFIED
        self._log(rec, "rights_updated", changed=sorted(changes),
                  before={k: (v if isinstance(v, (str, bool, type(None), list)) else str(v)) for k, v in before.items()})
        self._apply_decision(rec, evaluate_rights(rec, ctx), "rights_evaluated")
        self._save()
        return copy.deepcopy(rec)

    def dispute(self, asset_id: str, reason: str) -> AssetRecord:
        rec = self._must(asset_id)
        rec.verification_status = Verification.DISPUTED
        self._log(rec, "disputed", reason=reason[:500])
        self._apply_decision(rec, evaluate_rights(rec), "rights_evaluated")
        self._save()
        return copy.deepcopy(rec)

    def quarantine(self, asset_id: str, reason: str) -> AssetRecord:
        """Manual hold. Persists through re-evaluation, evidence, attestation and rights updates; only
        release_hold() lifts it. An already REJECTED asset stays REJECTED."""
        rec = self._must(asset_id)
        self._set_hold(rec, reason, "manual_quarantine")
        self._save()
        return copy.deepcopy(rec)

    def reject(self, asset_id: str, reason: str) -> AssetRecord:
        """Manual rejection hold (same persistence rules as quarantine)."""
        rec = self._must(asset_id)
        self._set_hold(rec, reason, "manual_reject", reject=True)
        self._save()
        return copy.deepcopy(rec)

    def reevaluate(self, asset_id: str, ctx: Optional[UseContext] = None) -> RightsDecision:
        """Re-run the rights decision (e.g. after evidence/attestation). A manual
        hold is not lifted by this; use release_hold()."""
        rec = self._must(asset_id)
        decision = evaluate_rights(rec, ctx)
        self._apply_decision(rec, decision, "rights_evaluated")
        self._save()
        return decision

    def release_hold(self, asset_id: str, reviewer: str, note: str, ctx: Optional[UseContext] = None) -> AssetRecord:
        """The ONLY way to lift a hold. First verifies path safety, file existence and checksum: an
        integrity failure keeps (or re-applies) the quarantine. Then rights are re-evaluated, so this
        cannot approve an asset whose rights checks fail."""
        if not reviewer.strip() or not note.strip():
            raise ValueError("reviewer and note are required")
        rec = self._must(asset_id)
        why = self._file_problem(rec)
        if why is not None:
            self._log(rec, "hold_release_refused", reviewer=reviewer.strip()[:200], note=note.strip()[:500], reason=why)
            self._set_hold(rec, f"integrity check failed: {why}", "integrity_quarantine")
            self._save()
            return copy.deepcopy(rec)
        self._log(rec, "hold_released", reviewer=reviewer.strip()[:200], note=note.strip()[:500])
        rec.state_reason = ""
        self._apply_decision(rec, evaluate_rights(rec, ctx), "rights_evaluated")
        self._save()
        return copy.deepcopy(rec)

    def link_project(self, asset_id: str, project: str) -> AssetRecord:
        """Record that a project uses this asset (e.g. after a DuplicateAssetError)."""
        if not PROJECT_RE.fullmatch(project):
            raise UnsafePathError("invalid project slug")
        rec = self._must(asset_id)
        if project not in rec.projects:
            rec.projects.append(project)
            self._log(rec, "project_linked", project=project)
            self._save()
        return copy.deepcopy(rec)

    def verify_files(self) -> list[dict]:
        """Re-check every indexed file (path safety, existence, checksum). A problem places a hold
        (a REJECTED asset stays REJECTED); nothing is ever deleted, moved or repaired."""
        problems = []
        for rec in self._sorted():
            why = self._file_problem(rec)
            if why is None:
                continue
            problems.append({"asset_id": rec.asset_id, "problem": why})
            self._set_hold(rec, why, "integrity_quarantine")
        if problems:
            self._save()
        return problems

    # ---- query ---------------------------------------------------------------------------
    def get(self, asset_id: str) -> AssetRecord:
        """A COPY: editing it cannot change the library's state (use the library methods)."""
        return copy.deepcopy(self._must(asset_id))

    def all(self) -> list[AssetRecord]:
        return [copy.deepcopy(r) for r in self._sorted()]

    def _sorted(self) -> list[AssetRecord]:
        return sorted(self._records.values(), key=lambda r: r.asset_id)

    def search(self, tags: Iterable[str] = (), any_tags: Iterable[str] = (), category=None, media_type=None,
               project: Optional[str] = None, include_global: bool = True, text: str = "",
               states: Iterable[State] = (State.APPROVED,)) -> list[AssetRecord]:
        """Default returns only APPROVED assets. For `project`: assets scoped to or linked with
        that project, plus global (reusable) assets unless include_global=False."""
        want_all = {norm_tag(t) for t in tags}
        want_any = {norm_tag(t) for t in any_tags}
        states = set(states)
        out = []
        for r in self._sorted():
            if r.state not in states:
                continue
            if category is not None and r.category != category:
                continue
            if media_type is not None and r.media_type != media_type:
                continue
            if want_all and not want_all <= set(r.tags):
                continue
            if want_any and not want_any & set(r.tags):
                continue
            if text and text.lower() not in (r.title + " " + " ".join(r.tags)).lower():
                continue
            if project is not None:
                mine = r.scope == f"project:{project}" or project in r.projects
                if not mine and not (include_global and r.scope == "global"):
                    continue
            out.append(copy.deepcopy(r))
        return out

    def evaluate(self, asset_id: str, request: Optional[AssetRequest] = None, spec: Optional[TargetSpec] = None,
                 ctx: Optional[UseContext] = None) -> AssetEvaluation:
        """Evaluation that respects the STORED state (a quarantined/rejected record is never usable)
        and, for anything that would otherwise pass, the file's current integrity."""
        rec = self._must(asset_id)
        ev = evaluate(rec, request, spec, ctx, self._records.values())
        if ev.verdict not in ("reject", "quarantine"):
            why = self._file_problem(rec)
            if why is not None:
                ev.verdict = "quarantine"
                ev.reasons.append(Reason("FILE_INTEGRITY", f"file cannot be relied on: {why}", "quarantine"))
        return ev

    def cleared_for(self, asset_id: str, ctx: UseContext) -> bool:
        """Is the asset usable for THIS use? Needs state approved, rights permitted under ctx AND the
        file on disk safe and unchanged. A problem places a hold and returns False."""
        rec = self._must(asset_id)
        if rec.state is not State.APPROVED or not evaluate_rights(rec, ctx).permitted:
            return False
        why = self._file_problem(rec)
        if why is not None:
            self._set_hold(rec, why, "integrity_quarantine")
            self._save()
            return False
        return True

    def obligations(self, asset_id: str, ctx: UseContext) -> list[str]:
        """Duties (attribution text, share-alike) attached to using this asset under `ctx`."""
        return list(evaluate_rights(self._must(asset_id), ctx).obligations)
