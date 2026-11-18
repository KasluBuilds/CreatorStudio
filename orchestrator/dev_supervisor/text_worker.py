"""Bounded TEXT-ONLY worker over the local OmniRoute gateway (T-DEV-019, wave w06 lane L2). Stdlib only.

Runs one small task (summarise, classify, extract_json, review_notes) on a free route through the existing
dev_supervisor.omniroute client, falls back to the next route on failure, and checks the output with a
deterministic checker (no LLM judge).

Limits and rules:
- TEXT ONLY, no tool use. OBSERVED 2026-11-18: Claude Code through OmniRoute fails tool calls (Gemini error
  'missing thought_signature'), so nothing here sends tools or expects a tool loop.
- Local gateway only (omniroute.py refuses non-local base URLs unless --allow-remote is given).
- Free routes only: a route must start with an allowed free prefix (gemini/, groq/). No paid route is ever used.
- The API key is handled entirely by omniroute.py at call time; this module never reads, stores or prints it.
- Result label: REPORTED until the checker passes, then CHECKED. Never VERIFIED (a checker is not real-input proof).

Run (from orchestrator/):
  python -B -m dev_supervisor.text_worker --task <task.json> --out <result.json> [--routes r1,r2] [--benchmark <results.json>]
Task file: {"kind": "...", "input": "text", optional "labels": [...], "required_keys": [...],
            "min_words": n, "max_words": n, "max_lines": n, "max_tokens": n}
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable, Optional

from . import omniroute as om

KINDS = ("summarise", "classify", "extract_json", "review_notes")
FREE_PREFIXES = ("gemini/", "groq/")
DEFAULT_ROUTES = ("gemini/gemini-3.1-flash-lite", "gemini/gemini-3-flash-preview")
FALLBACK_ERRORS = (om.E_RATE, om.E_TIMEOUT, om.E_NOT_FOUND, om.E_AUTH)
MAX_RETRIES_PER_ROUTE = 1       # extra attempts on the same route, only for retryable classes
MAX_TOTAL_ATTEMPTS = 8
MAX_INPUT_CHARS = 8000
LABEL_REPORTED = "REPORTED"
LABEL_CHECKED = "CHECKED"
SCHEMA = "text-worker-result/1"

DEFAULT_LIMITS = {"summarise": {"min_words": 3, "max_words": 40},
                  "review_notes": {"min_words": 3, "max_words": 150, "max_lines": 8}}


class TaskError(ValueError):
    """Invalid task definition (message never contains response text)."""


# ---------------------------------------------------------------- task

def load_task(data: dict) -> dict:
    if not isinstance(data, dict):
        raise TaskError("task must be an object")
    kind = data.get("kind")
    if kind not in KINDS:
        raise TaskError("kind must be one of " + ", ".join(KINDS))
    text = data.get("input")
    if not isinstance(text, str) or not text.strip():
        raise TaskError("input must be a non-empty string")
    if len(text) > MAX_INPUT_CHARS:
        raise TaskError("input longer than %d characters" % MAX_INPUT_CHARS)
    task = dict(data)
    if kind == "classify":
        labels = data.get("labels")
        if (not isinstance(labels, list) or len(labels) < 2
                or not all(isinstance(x, str) and x.strip() for x in labels)):
            raise TaskError("classify needs labels: a list of at least 2 non-empty strings")
    if kind == "extract_json":
        keys = data.get("required_keys")
        if not isinstance(keys, list) or not keys or not all(isinstance(x, str) and x for x in keys):
            raise TaskError("extract_json needs required_keys: a non-empty list of strings")
    for n in ("min_words", "max_words", "max_lines", "max_tokens"):
        if n in data and (not isinstance(data[n], int) or isinstance(data[n], bool) or data[n] < 1):
            raise TaskError(n + " must be a positive integer")
    return task


def _limits(task: dict) -> dict:
    lim = {"min_words": 1, "max_words": 10_000, "max_lines": 10_000}
    lim.update(DEFAULT_LIMITS.get(task["kind"], {}))
    for n in ("min_words", "max_words", "max_lines"):
        if n in task:
            lim[n] = task[n]
    return lim


def build_prompt(task: dict) -> str:
    kind, text = task["kind"], task["input"]
    lim = _limits(task)
    if kind == "summarise":
        return ("Summarise the text below in at most %d words. Reply with the summary only.\n\nTEXT:\n%s"
                % (lim["max_words"], text))
    if kind == "classify":
        return ("Classify the text below into exactly one of these labels: %s. Reply with the label only, "
                "nothing else.\n\nTEXT:\n%s" % (", ".join(task["labels"]), text))
    if kind == "extract_json":
        return ("Extract the requested fields from the text below. Reply with only one JSON object containing "
                "the keys: %s.\n\nTEXT:\n%s" % (", ".join(task["required_keys"]), text))
    return ("Review the notes below. Reply with at most %d short bullet lines (one per line, starting with '- '), "
            "at most %d words in total.\n\nNOTES:\n%s" % (lim["max_lines"], lim["max_words"], text))


# ---------------------------------------------------------------- checkers (deterministic)

def check_summarise(text: str, task: dict) -> "tuple[bool, str]":
    lim = _limits(task)
    n = len(text.split())
    if n < lim["min_words"]:
        return False, "too_short"
    if n > lim["max_words"]:
        return False, "too_long"
    return True, "ok"


def check_classify(text: str, task: dict) -> "tuple[bool, str]":
    got = text.strip().strip("\"'`.").strip().lower()
    allowed = {x.strip().lower() for x in task["labels"]}
    return (True, "ok") if got in allowed else (False, "label_not_allowed")


def check_extract_json(text: str, task: dict) -> "tuple[bool, str]":
    try:
        doc = json.loads(om._strip_fences(text))
    except ValueError:
        return False, "invalid_json"
    if not isinstance(doc, dict):
        return False, "not_an_object"
    missing = [k for k in task["required_keys"] if k not in doc]
    return (False, "missing_keys") if missing else (True, "ok")


def check_review_notes(text: str, task: dict) -> "tuple[bool, str]":
    lim = _limits(task)
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return False, "empty"
    if len(lines) > lim["max_lines"]:
        return False, "too_many_lines"
    n = len(text.split())
    if n < lim["min_words"]:
        return False, "too_short"
    if n > lim["max_words"]:
        return False, "too_long"
    return True, "ok"


CHECKERS = {"summarise": check_summarise, "classify": check_classify,
            "extract_json": check_extract_json, "review_notes": check_review_notes}


def run_checker(task: dict, text: str) -> "tuple[bool, str]":
    try:
        return CHECKERS[task["kind"]](text, task)
    except Exception:
        return False, "checker_error"


# ---------------------------------------------------------------- route order

def is_free_route(route: str) -> bool:
    return isinstance(route, str) and route.startswith(FREE_PREFIXES) and len(route) > 6


def order_routes(benchmark: Optional[dict] = None, routes=None) -> "list[str]":
    """Explicit routes win. Else benchmark rows by pass rate (desc) then mean latency (asc); routes that
    never passed are dropped. Else the default list. Non-free routes are always dropped."""
    if routes:
        chosen = list(routes)
    elif benchmark:
        rows = [r for r in benchmark.get("routes", [])
                if isinstance(r, dict) and isinstance(r.get("route"), str)
                and isinstance(r.get("pass_rate"), (int, float)) and r["pass_rate"] > 0]
        rows.sort(key=lambda r: (-r["pass_rate"],
                                 r["latency_mean_s"] if isinstance(r.get("latency_mean_s"), (int, float)) else 1e9,
                                 r["route"]))
        chosen = [r["route"] for r in rows] or list(DEFAULT_ROUTES)
    else:
        chosen = list(DEFAULT_ROUTES)
    seen, out = set(), []
    for r in chosen:
        if is_free_route(r) and r not in seen:
            seen.add(r)
            out.append(r)
    return out


# ---------------------------------------------------------------- run

def run_task(task: dict, client: om.OmniRouteClient, routes: "list[str]",
             sleep: Callable[[float], None] = time.sleep, max_total_attempts: int = MAX_TOTAL_ATTEMPTS) -> dict:
    task = load_task(task)
    routes = [r for r in routes if is_free_route(r)]
    if not routes:
        raise TaskError("no usable free route")
    prompt = build_prompt(task)
    max_tokens = task.get("max_tokens", 512)
    attempts, last_text, last_route, last_verdict = [], "", None, None
    passed_route = passed_text = None
    for route in routes:
        for n in range(MAX_RETRIES_PER_ROUTE + 1):
            if len(attempts) >= max_total_attempts:
                break
            res = client.chat(route, prompt, max_tokens=max_tokens)
            rec = {"route": route, "attempt": len(attempts) + 1, "ok": res.ok, "status": res.status,
                   "latency_s": round(res.latency_s, 3), "error_class": res.error_class, "checker": None}
            attempts.append(rec)
            if res.ok:
                good, why = run_checker(task, res.text)
                rec["checker"] = why
                last_text, last_route, last_verdict = res.text, route, (good, why)
                if good:
                    passed_route, passed_text = route, res.text
                else:
                    rec["error_class"] = "check_failed"
                break  # wrong output: next route, no same-route retry
            if res.error_class in (om.E_RATE, om.E_NOT_FOUND, om.E_AUTH):
                break  # rate-limited free route is not hammered; the others will not recover
            if res.error_class in om.RETRYABLE and n < MAX_RETRIES_PER_ROUTE:
                sleep(1.0 + n)
                continue
            break
        if passed_route or len(attempts) >= max_total_attempts:
            break
    checked = passed_route is not None
    verdict = {"passed": checked,
               "reason": "ok" if checked else (last_verdict[1] if last_verdict else "no_output")}
    return {"schema": SCHEMA, "kind": task["kind"], "label": LABEL_CHECKED if checked else LABEL_REPORTED,
            "status": "OK" if checked else ("CHECK_FAILED" if last_verdict else "FAILED"),
            "route": passed_route if checked else last_route, "route_order": list(routes),
            "attempts": attempts, "attempt_count": len(attempts),
            "latency_total_s": round(sum(a["latency_s"] for a in attempts), 3),
            "failure_classes": sorted({a["error_class"] for a in attempts if a["error_class"]}),
            "checker": verdict, "output": passed_text if checked else last_text}


def main(argv=None, transport=om.urllib_transport, sleep=time.sleep) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Bounded text-only task on free local OmniRoute routes.")
    ap.add_argument("--task", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--routes", default=None, help="comma separated route ids (overrides --benchmark)")
    ap.add_argument("--benchmark", default=None, help="benchmark results JSON (omniroute-benchmark/1)")
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--allow-remote", action="store_true")
    ap.add_argument("--timeout", type=float, default=45.0)
    a = ap.parse_args(argv)
    try:
        task = load_task(json.loads(Path(a.task).read_text(encoding="utf-8")))
        bench = json.loads(Path(a.benchmark).read_text(encoding="utf-8")) if a.benchmark else None
        explicit = [r.strip() for r in a.routes.split(",") if r.strip()] if a.routes else None
        client = om.OmniRouteClient(transport, a.base_url, a.allow_remote, timeout_s=a.timeout)
    except om.OmniRouteError as e:
        print("refused:", e.error_class)
        return 2
    except (OSError, ValueError) as e:  # TaskError and JSON errors are ValueErrors
        print("refused:", type(e).__name__, str(e)[:120])
        return 2
    routes = order_routes(bench, explicit)
    if not routes:
        print("refused: no usable free route")
        return 2
    result = run_task(task, client, routes, sleep=sleep)
    Path(a.out).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(result["kind"], result["label"], "route=%s" % result["route"], "attempts=%d" % result["attempt_count"],
          "checker=%s" % result["checker"]["reason"], "latency_s=%s" % result["latency_total_s"])
    return 0 if result["label"] == LABEL_CHECKED else 1


if __name__ == "__main__":
    raise SystemExit(main())
