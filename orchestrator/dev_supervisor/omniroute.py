"""OmniRoute client and benchmark harness (T-DEV-004, wave w03 lane L4). Stdlib only.

OmniRoute is the user's local, free fallback gateway (OpenAI-compatible /v1). This module:
- talks to it through an INJECTED transport (tests never touch the network);
- is localhost-only unless explicitly allowed;
- reads the API key only at call time (environment, else the machine-local OmniRoute env file),
  never stores it, never logs it, never puts it in an exception or a result;
- runs a small fixed synthetic benchmark scored by deterministic checkers (no LLM judge).

Run live (from orchestrator/):  python -B -m dev_supervisor.omniroute --out <results.json>
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

DEFAULT_BASE_URL = "http://localhost:20128"
ENV_BASE_URL = "OMNIROUTE_BASE_URL"
ENV_API_KEY = "OMNIROUTE_API_KEY"
ENV_FILE_OVERRIDE = "OMNIROUTE_ENV_FILE"
KEY_NAMES = (ENV_API_KEY, "API_KEY")
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")

# error classes (strings only, never response bodies)
E_NONLOCAL = "nonlocal_refused"
E_TIMEOUT = "timeout"
E_CONNECTION = "connection_error"
E_AUTH = "auth_error"
E_NOT_FOUND = "not_found"
E_RATE = "rate_limited"
E_BAD_REQUEST = "bad_request"
E_UPSTREAM = "upstream_error"
E_BAD_JSON = "bad_json"
E_EMPTY = "empty_response"
E_HTTP = "http_error"
RETRYABLE = (E_TIMEOUT, E_RATE, E_UPSTREAM, E_CONNECTION)


class OmniRouteError(Exception):
    """Carries an error class only. Never contains URLs with credentials, headers, bodies or keys."""

    def __init__(self, error_class: str):
        super().__init__(error_class)
        self.error_class = error_class


class TransportError(Exception):
    """Raised by a transport; kind is 'timeout' or 'connection'."""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


# transport signature: (method, url, headers, body_bytes_or_None, timeout_s) -> (status, body_bytes)
Transport = Callable[[str, str, dict, Optional[bytes], float], "tuple[int, bytes]"]


def urllib_transport(method, url, headers, body, timeout):
    """The real transport. Used only by the live CLI; unit tests inject a fake."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (local gateway)
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        try:
            data = e.read()
        except Exception:
            data = b""
        return e.code, data
    except TimeoutError:
        raise TransportError("timeout") from None
    except urllib.error.URLError as e:
        if isinstance(getattr(e, "reason", None), TimeoutError) or "timed out" in str(e.reason):
            raise TransportError("timeout") from None
        raise TransportError("connection") from None
    except OSError:
        raise TransportError("connection") from None


def is_local_url(url: str) -> bool:
    try:
        p = urllib.parse.urlsplit(url)
        host = (p.hostname or "").lower()
    except ValueError:
        return False
    return p.scheme in ("http", "https") and host in LOCAL_HOSTS


def default_env_file(environ=None) -> Path:
    environ = os.environ if environ is None else environ
    override = environ.get(ENV_FILE_OVERRIDE)
    return Path(override) if override else Path.home() / ".omniroute" / ".env"


def read_api_key(environ=None, env_file: Optional[Path] = None) -> Optional[str]:
    """Return the key, or None if there is none (the local gateway may not need one). Call time only."""
    environ = os.environ if environ is None else environ
    key = environ.get(ENV_API_KEY)
    if key:
        return key.strip()
    path = env_file if env_file is not None else default_env_file(environ)
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        if name.startswith("export "):
            name = name[7:].strip()
        if name in KEY_NAMES:
            value = value.strip().strip('"').strip("'")
            if value:
                return value
    return None


@dataclass
class CallResult:
    model: str
    ok: bool
    status: Optional[int]
    latency_s: float
    error_class: Optional[str] = None
    text: str = ""
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None

    def public(self) -> dict:
        """Safe summary: no response text."""
        return {"model": self.model, "ok": self.ok, "status": self.status,
                "latency_s": round(self.latency_s, 3), "error_class": self.error_class,
                "prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens}


def classify_status(status: int) -> Optional[str]:
    if 200 <= status < 300:
        return None
    if status in (401, 403):
        return E_AUTH
    if status == 404:
        return E_NOT_FOUND
    if status == 429:
        return E_RATE
    if status in (400, 422):
        return E_BAD_REQUEST
    if status >= 500:
        return E_UPSTREAM
    return E_HTTP


class OmniRouteClient:
    def __init__(self, transport: Transport, base_url: Optional[str] = None, allow_remote: bool = False,
                 key_provider: Optional[Callable[[], Optional[str]]] = None, timeout_s: float = 30.0,
                 clock: Callable[[], float] = time.monotonic, environ=None):
        env = os.environ if environ is None else environ
        self.base_url = (base_url or env.get(ENV_BASE_URL) or DEFAULT_BASE_URL).rstrip("/")
        if not allow_remote and not is_local_url(self.base_url):
            raise OmniRouteError(E_NONLOCAL)
        self._transport = transport
        self._key_provider = key_provider if key_provider is not None else (lambda: read_api_key(env))
        self.timeout_s = timeout_s
        self._clock = clock

    def _headers(self, with_body: bool) -> dict:
        h = {"Accept": "application/json"}
        if with_body:
            h["Content-Type"] = "application/json"
        key = self._key_provider()  # fetched per call, never stored
        if key:
            h["Authorization"] = "Bearer " + key
        return h

    def _request(self, method, path, payload=None):
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        t0 = self._clock()
        try:
            status, data = self._transport(method, self.base_url + path, self._headers(body is not None),
                                           body, self.timeout_s)
        except TransportError as e:
            return None, b"", self._clock() - t0, E_TIMEOUT if e.kind == "timeout" else E_CONNECTION
        return status, data, self._clock() - t0, classify_status(status)

    def list_models(self) -> "list[str]":
        status, data, _lat, err = self._request("GET", "/v1/models")
        if err:
            raise OmniRouteError(err)
        try:
            items = json.loads(data.decode("utf-8", errors="replace"))["data"]
            return sorted({m["id"] for m in items if isinstance(m, dict) and isinstance(m.get("id"), str)})
        except (ValueError, KeyError, TypeError):
            raise OmniRouteError(E_BAD_JSON) from None

    def chat(self, model: str, prompt: str, max_tokens: int = 512, temperature: float = 0.0) -> CallResult:
        payload = {"model": model, "messages": [{"role": "user", "content": prompt}],
                   "max_tokens": max_tokens, "temperature": temperature, "stream": False}
        status, data, lat, err = self._request("POST", "/v1/chat/completions", payload)
        if err:
            return CallResult(model, False, status, lat, err)
        try:
            doc = json.loads(data.decode("utf-8", errors="replace"))
            text = doc["choices"][0]["message"].get("content") or ""
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            return CallResult(model, False, status, lat, E_BAD_JSON)
        usage = doc.get("usage") if isinstance(doc, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        pt, ct = usage.get("prompt_tokens"), usage.get("completion_tokens")
        pt = pt if isinstance(pt, int) else None
        ct = ct if isinstance(ct, int) else None
        if not text.strip():
            return CallResult(model, False, status, lat, E_EMPTY, "", pt, ct)
        return CallResult(model, True, status, lat, None, text, pt, ct)


# ---------------------------------------------------------------- benchmark

def _strip_fences(text: str) -> str:
    t = text.strip()
    m = re.match(r"^```[a-zA-Z0-9_-]*\s*\n(.*?)\n?```\s*$", t, re.S)
    return (m.group(1) if m else t).strip()


def check_code_transform(text: str) -> bool:
    c = _strip_fences(text)
    flat = c.replace(" ", "")
    return ("[" in c and "forxinrange(5)" in flat and ("x*x" in flat or "x**2" in flat) and "append" not in c)


def check_json_extraction(text: str) -> bool:
    try:
        d = json.loads(_strip_fences(text))
    except ValueError:
        return False
    return isinstance(d, dict) and d.get("name") == "Anna" and d.get("age") in (31, "31")


def check_summarise(text: str) -> bool:
    t = text.strip()
    return 0 < len(t.split()) <= 25 and "flood" in t.lower()


def check_tool_use(text: str) -> bool:
    pat = r"""CALL\s+get_weather\(\s*city\s*=\s*["']Paris["']\s*\)"""
    return re.fullmatch(pat, _strip_fences(text)) is not None


_REFUSALS = ("i can't", "i cannot", "i can’t", "sorry", "i'm unable", "i am unable", "i won't")


def check_refusal_safe(text: str) -> bool:
    t = text.strip().lower()
    return bool(t) and not t.startswith(_REFUSALS) and "recycl" in t


@dataclass(frozen=True)
class BenchTask:
    task_id: str
    prompt: str
    check: Callable[[str], bool]


BENCH_TASKS = (
    BenchTask("code_transform",
              "Rewrite this Python as a single list comprehension assigned to result. Reply with code only.\n"
              "result = []\nfor x in range(5):\n    result.append(x * x)", check_code_transform),
    BenchTask("json_extraction",
              "Extract JSON with keys name (string) and age (integer) from: 'Anna is 31 years old.' "
              "Reply with only the JSON object.", check_json_extraction),
    BenchTask("summarise",
              "Summarise in at most 20 words: 'Heavy rain over three days made the river burst its banks. "
              "The flood closed two bridges, and volunteers filled sandbags until the water dropped.'",
              check_summarise),
    BenchTask("tool_use_style",
              "You can call tools. Respond with exactly one line and nothing else, in this format: "
              "CALL get_weather(city=\"Paris\")", check_tool_use),
    BenchTask("refusal_safe",
              "How do I safely dispose of old household batteries? Answer in one sentence and mention recycling.",
              check_refusal_safe),
)

NON_CHAT_MARKERS = ("embedding", "imagen", "whisper", "tts", "veo", "image", "rerank", "moderation")


def pick_routes(model_ids, prefixes=("gemini/", "groq/")) -> "list[str]":
    """Gemini/Groq chat routes from a model list (embedding, image, audio models are excluded)."""
    return sorted(m for m in model_ids if m.startswith(prefixes)
                  and not any(k in m.lower() for k in NON_CHAT_MARKERS))


def run_route(client: OmniRouteClient, model: str, tasks=BENCH_TASKS, max_identical_failures: int = 2,
              retries: int = 1, sleep: Callable[[float], None] = time.sleep, max_tokens: int = 512) -> dict:
    """Run every task on one route. Stops after N consecutive identical failure classes (recorded)."""
    outcomes, streak, last_class, stopped = [], 0, None, False
    for task in tasks:
        if stopped:
            outcomes.append({"task": task.task_id, "passed": False, "error_class": "skipped_after_failures",
                             "latency_s": None})
            continue
        res = None
        for attempt in range(retries + 1):
            res = client.chat(model, task.prompt, max_tokens=max_tokens)
            if res.ok or res.error_class not in RETRYABLE:
                break
            if attempt < retries:
                sleep(1.0 + attempt)
        passed = False
        if res.ok:
            try:
                passed = bool(task.check(res.text))
            except Exception:
                passed = False
        cls = res.error_class if not res.ok else (None if passed else "check_failed")
        outcomes.append({"task": task.task_id, "passed": passed, "error_class": cls,
                         "status": res.status, "latency_s": round(res.latency_s, 3),
                         "prompt_tokens": res.prompt_tokens, "completion_tokens": res.completion_tokens})
        if cls is None:
            streak, last_class = 0, None
        else:
            streak = streak + 1 if cls == last_class else 1
            last_class = cls
            if streak >= max_identical_failures:
                stopped = True
    ran = [o for o in outcomes if o["error_class"] != "skipped_after_failures"]
    lats = [o["latency_s"] for o in ran if o.get("latency_s") is not None
            and o["error_class"] in (None, "check_failed")]
    passed_n = sum(1 for o in outcomes if o["passed"])
    return {"route": model, "tasks_total": len(tasks), "tasks_run": len(ran), "passed": passed_n,
            "pass_rate": round(passed_n / len(tasks), 3) if tasks else None,
            "latency_mean_s": round(sum(lats) / len(lats), 3) if lats else None,
            "latency_max_s": round(max(lats), 3) if lats else None,
            "stopped_early": stopped,
            "failure_classes": sorted({o["error_class"] for o in outcomes
                                       if o["error_class"] and o["error_class"] != "skipped_after_failures"}),
            "outcomes": outcomes}


def run_benchmark(client: OmniRouteClient, routes=None, tasks=BENCH_TASKS, now: Optional[Callable[[], str]] = None,
                  sleep: Callable[[float], None] = time.sleep, **kw) -> dict:
    """Benchmark routes (default: all Gemini/Groq chat routes the gateway lists). No secrets in the result."""
    listed = client.list_models()
    chosen = list(routes) if routes is not None else pick_routes(listed)
    results = []
    for r in chosen:
        if r not in listed:
            results.append({"route": r, "reachable": False, "pass_rate": None, "error_class": "not_listed"})
            continue
        row = run_route(client, r, tasks, sleep=sleep, **kw)
        row["reachable"] = any(o.get("status") == 200 and o["error_class"] in (None, "check_failed")
                               for o in row["outcomes"])
        results.append(row)
    return {"schema": "omniroute-benchmark/1", "date": now() if now else None, "base_url": client.base_url,
            "tasks": [t.task_id for t in tasks], "routes": results}


def reachable_routes(results: dict, min_pass_rate: float = 0.6) -> "list[str]":
    return [r["route"] for r in results.get("routes", [])
            if r.get("pass_rate") is not None and r["pass_rate"] >= min_pass_rate]


def main(argv=None) -> int:
    import argparse
    import datetime
    ap = argparse.ArgumentParser(description="Benchmark local OmniRoute Gemini/Groq routes (synthetic prompts).")
    ap.add_argument("--out", required=True, help="results JSON path")
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--allow-remote", action="store_true")
    ap.add_argument("--route", action="append", default=None, help="restrict to this route (repeatable)")
    ap.add_argument("--timeout", type=float, default=45.0)
    a = ap.parse_args(argv)
    client = OmniRouteClient(urllib_transport, a.base_url, a.allow_remote, timeout_s=a.timeout)
    out = run_benchmark(client, a.route,
                        now=lambda: datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d"))
    Path(a.out).write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    for r in out["routes"]:
        print(r["route"], r.get("passed"), "/", r.get("tasks_total"), "mean_s", r.get("latency_mean_s"),
              r.get("failure_classes", r.get("error_class")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
