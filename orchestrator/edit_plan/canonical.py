"""Canonical serialisation and plan hash.

Canonical form: sorted keys, compact separators, ASCII-escaped, strings NFC-normalised, frame rates reduced to
lowest terms (30000/1001 and 60000/2002 hash alike), integral floats written as ints, non-finite floats refused.
The hash is sha256 over the UTF-8 bytes of that JSON, so it is identical across runs, platforms and key order.
"""
from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from math import gcd
from typing import Any

from .schema import EditPlan


def _norm(v: Any) -> Any:
    if isinstance(v, bool) or v is None or isinstance(v, int):
        return v
    if isinstance(v, float):
        if not math.isfinite(v):
            raise ValueError("non-finite number cannot be serialised canonically")
        return int(v) if v == int(v) else v
    if isinstance(v, str):
        return unicodedata.normalize("NFC", v)
    if isinstance(v, (list, tuple)):
        return [_norm(x) for x in v]
    if isinstance(v, dict):
        return {unicodedata.normalize("NFC", str(k)): _norm(x) for k, x in v.items()}
    raise TypeError(f"cannot serialise {type(v).__name__}")


def _reduce_rate(r: dict) -> dict:
    if isinstance(r.get("num"), int) and isinstance(r.get("den"), int) and r["num"] > 0 and r["den"] > 0:
        g = gcd(r["num"], r["den"])
        return {"num": r["num"] // g, "den": r["den"] // g}
    return r


def canonical_dict(plan: EditPlan) -> dict:
    d = plan.to_dict()
    d["timeline"]["frame_rate"] = _reduce_rate(d["timeline"]["frame_rate"])
    for s in d["sources"]:
        s["frame_rate"] = _reduce_rate(s["frame_rate"])
    return _norm(d)


def canonical_json(plan: EditPlan) -> str:
    return json.dumps(canonical_dict(plan), sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False)


def plan_hash(plan: EditPlan) -> str:
    """sha256 hex of the canonical JSON. Any change to any field (including ids and rationale) changes it."""
    return hashlib.sha256(canonical_json(plan).encode("utf-8")).hexdigest()
