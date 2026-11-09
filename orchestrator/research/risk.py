"""Claim-risk classification: a deliberately small, explicit POLICY, not a knowledge base.

A claim about toxicity, edibility, health or physical danger is SAFETY_CRITICAL. The classifier sets a
FLOOR: a model cannot declare such a claim ORDINARY; only a valid `risk_downgrade` attestation (a human
review recorded by the orchestrator's review boundary) can. Matching is by accent-insensitive word stems
(English and Croatian), so it is a tripwire for the obvious cases, not a guarantee: a safety-relevant claim
phrased without these words will not be caught, and a reviewer can always DECLARE a claim safety-critical.

Treat the stem list as policy data to be owned and tuned by the project, not as medical knowledge.
"""
from __future__ import annotations

import re
import unicodedata

# Prefix stems (a trailing '*' in the policy below means "word starts with"). Keep this list short.
RISK_STEMS = (
    # toxicity / poison / lethality
    "poison", "toxic", "toxin", "lethal", "fatal", "deadly", "venom", "carcinogen", "hazard", "overdos", "dosage",
    "otrov", "toksic", "smrtonosn", "smrtn", "opasn",
    # edibility
    "edible", "inedible", "jestiv", "nejestiv",
    # health / treatment claims
    "cure", "curing", "allerg", "poisoning", "symptom", "medicin", "lijec", "lijek",
)
RISK_PHRASES = ("safe to eat", "safe to consume", "safe to drink", "safe for children", "sigurno za jelo",
                "sigurno jesti", "ne smije se jesti", "can kill", "may kill", "will kill")

_TRANSLATE = str.maketrans({"đ": "d", "Đ": "d"})


def _tokens(text: str) -> list[str]:
    s = unicodedata.normalize("NFKD", (text or "").translate(_TRANSLATE))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.findall(r"\w+", s.casefold())


def classify_risk(text: str) -> bool:
    """True if the text trips the safety-critical floor."""
    toks = _tokens(text)
    if any(t.startswith(stem) for t in toks for stem in RISK_STEMS):
        return True
    joined = " ".join(toks)
    return any(p in joined for p in RISK_PHRASES)
