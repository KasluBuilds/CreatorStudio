"""Dev supervisor routing + OmniRoute client tests. Synthetic only; sockets blocked (no network).

Run from orchestrator/:  python -B -I -m unittest discover -s tests -p test_dev_routing.py
"""
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dev_supervisor import omniroute as om  # noqa: E402
from dev_supervisor import routing as rt  # noqa: E402
from dev_supervisor.routing import (CLAUDE_STANDARD, CLAUDE_STRONG, OMNI_GEMINI, OMNI_GROQ, PAID_API,  # noqa: E402
                                    UNAVAILABLE, RoutingConfig, TaskProfile, decide)

FAKE_KEY = "sk-test-SECRET-0123456789"


def no_net(*a, **k):
    raise AssertionError("network used in a unit test")


class FakeTransport:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, dict(headers), body, timeout))
        return self.handler(method, url, headers, body, timeout)


def chat_body(text, pt=5, ct=7):
    return json.dumps({"choices": [{"message": {"content": text}}],
                       "usage": {"prompt_tokens": pt, "completion_tokens": ct}}).encode()


def models_body(ids):
    return json.dumps({"data": [{"id": i} for i in ids]}).encode()


def client_for(handler, **kw):
    kw.setdefault("key_provider", lambda: FAKE_KEY)
    kw.setdefault("environ", {})
    return om.OmniRouteClient(FakeTransport(handler), **kw), None


class NoNetworkCase(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(socket, "socket", no_net)
        p.start()
        self.addCleanup(p.stop)
        p2 = mock.patch.object(socket, "create_connection", no_net)
        p2.start()
        self.addCleanup(p2.stop)


class ClientTests(NoNetworkCase):
    def test_default_base_url_local(self):
        c = om.OmniRouteClient(FakeTransport(lambda *a: (200, b"")), environ={})
        self.assertEqual(c.base_url, "http://localhost:20128")

    def test_env_base_url(self):
        c = om.OmniRouteClient(FakeTransport(lambda *a: (200, b"")), environ={"OMNIROUTE_BASE_URL": "http://127.0.0.1:9/"})
        self.assertEqual(c.base_url, "http://127.0.0.1:9")

    def test_refuses_remote(self):
        with self.assertRaises(om.OmniRouteError) as cm:
            om.OmniRouteClient(FakeTransport(lambda *a: (200, b"")), "https://example.com", environ={})
        self.assertEqual(cm.exception.error_class, om.E_NONLOCAL)

    def test_refuses_lookalike_host(self):
        for u in ("http://localhost.evil.com", "http://evil.com/localhost", "http://127.0.0.1.evil.com"):
            self.assertFalse(om.is_local_url(u), u)

    def test_allow_remote_flag(self):
        c = om.OmniRouteClient(FakeTransport(lambda *a: (200, b"")), "https://example.com", allow_remote=True, environ={})
        self.assertEqual(c.base_url, "https://example.com")

    def test_is_local_variants(self):
        self.assertTrue(om.is_local_url("http://localhost:20128"))
        self.assertTrue(om.is_local_url("http://[::1]:5"))
        self.assertFalse(om.is_local_url("ftp://localhost"))
        self.assertFalse(om.is_local_url("not a url"))

    def test_list_models(self):
        c, _ = client_for(lambda *a: (200, models_body(["b", "a", "a"])))
        self.assertEqual(c.list_models(), ["a", "b"])

    def test_list_models_auth_error(self):
        c, _ = client_for(lambda *a: (401, b"secret body " + FAKE_KEY.encode()))
        with self.assertRaises(om.OmniRouteError) as cm:
            c.list_models()
        self.assertEqual(cm.exception.error_class, om.E_AUTH)
        self.assertNotIn(FAKE_KEY, str(cm.exception))

    def test_list_models_bad_json(self):
        c, _ = client_for(lambda *a: (200, b"<html>"))
        with self.assertRaises(om.OmniRouteError) as cm:
            c.list_models()
        self.assertEqual(cm.exception.error_class, om.E_BAD_JSON)

    def test_chat_ok_with_tokens_and_latency(self):
        ticks = iter([10.0, 10.5])
        c = om.OmniRouteClient(FakeTransport(lambda *a: (200, chat_body("hello", 3, 4))),
                               key_provider=lambda: None, clock=lambda: next(ticks), environ={})
        r = c.chat("m", "hi")
        self.assertTrue(r.ok)
        self.assertEqual((r.status, r.latency_s, r.prompt_tokens, r.completion_tokens), (200, 0.5, 3, 4))

    def test_chat_without_usage(self):
        body = json.dumps({"choices": [{"message": {"content": "x"}}]}).encode()
        c, _ = client_for(lambda *a: (200, body))
        r = c.chat("m", "p")
        self.assertTrue(r.ok)
        self.assertIsNone(r.prompt_tokens)

    def test_chat_request_shape(self):
        c, _ = client_for(lambda *a: (200, chat_body("x")))
        c.chat("gemini/x", "prompt", max_tokens=9)
        method, url, headers, body, timeout = c._transport.calls[0]
        doc = json.loads(body)
        self.assertEqual((method, url), ("POST", "http://localhost:20128/v1/chat/completions"))
        self.assertEqual((doc["model"], doc["max_tokens"], doc["stream"]), ("gemini/x", 9, False))
        self.assertEqual(headers["Authorization"], "Bearer " + FAKE_KEY)

    def test_no_key_no_auth_header(self):
        c = om.OmniRouteClient(FakeTransport(lambda *a: (200, chat_body("x"))), key_provider=lambda: None, environ={})
        c.chat("m", "p")
        self.assertNotIn("Authorization", c._transport.calls[0][2])

    def test_error_classes(self):
        for status, cls in ((401, om.E_AUTH), (403, om.E_AUTH), (404, om.E_NOT_FOUND), (429, om.E_RATE),
                            (400, om.E_BAD_REQUEST), (503, om.E_UPSTREAM), (302, om.E_HTTP)):
            c, _ = client_for(lambda *a, s=status: (s, b"{}"))
            r = c.chat("m", "p")
            self.assertFalse(r.ok)
            self.assertEqual(r.error_class, cls, status)

    def test_transport_timeout_and_connection(self):
        def boom(kind):
            def h(*a):
                raise om.TransportError(kind)
            return h
        c, _ = client_for(boom("timeout"))
        self.assertEqual(c.chat("m", "p").error_class, om.E_TIMEOUT)
        c, _ = client_for(boom("connection"))
        self.assertEqual(c.chat("m", "p").error_class, om.E_CONNECTION)

    def test_empty_and_bad_json_chat(self):
        c, _ = client_for(lambda *a: (200, chat_body("   ")))
        self.assertEqual(c.chat("m", "p").error_class, om.E_EMPTY)
        c, _ = client_for(lambda *a: (200, b"{}"))
        self.assertEqual(c.chat("m", "p").error_class, om.E_BAD_JSON)

    def test_public_summary_has_no_text_or_key(self):
        c, _ = client_for(lambda *a: (200, chat_body("secret answer")))
        s = json.dumps(c.chat("m", "p").public())
        self.assertNotIn("secret answer", s)
        self.assertNotIn(FAKE_KEY, s)

    def test_key_not_stored_on_client(self):
        c, _ = client_for(lambda *a: (200, chat_body("x")))
        c.chat("m", "p")
        self.assertNotIn(FAKE_KEY, repr(vars(c)))


class KeyReadingTests(unittest.TestCase):
    def test_env_wins(self):
        self.assertEqual(om.read_api_key({"OMNIROUTE_API_KEY": " k1 "}, Path("nope")), "k1")

    def test_env_file_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".env"
            p.write_text("# c\nOTHER=1\nexport OMNIROUTE_API_KEY=\"filekey\"\n", encoding="utf-8")
            self.assertEqual(om.read_api_key({}, p), "filekey")

    def test_env_file_without_key_or_missing(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".env"
            p.write_text("STORAGE_ENCRYPTION_KEY=abc\n", encoding="utf-8")
            self.assertIsNone(om.read_api_key({}, p))
            self.assertIsNone(om.read_api_key({}, Path(d) / "missing"))

    def test_env_file_override_variable(self):
        self.assertEqual(om.default_env_file({"OMNIROUTE_ENV_FILE": "x/y"}), Path("x/y"))


class BenchmarkTests(NoNetworkCase):
    GOOD = {
        "code_transform": "```python\nresult = [x * x for x in range(5)]\n```",
        "json_extraction": '```json\n{"name": "Anna", "age": 31}\n```',
        "summarise": "A flood after heavy rain closed two bridges until volunteers helped.",
        "tool_use_style": 'CALL get_weather(city="Paris")',
        "refusal_safe": "Take them to a battery recycling point.",
    }

    def test_checkers_accept_good(self):
        for t in om.BENCH_TASKS:
            self.assertTrue(t.check(self.GOOD[t.task_id]), t.task_id)

    def test_checkers_reject_bad(self):
        bad = {"code_transform": "result = []\nfor x in range(5): result.append(x*x)",
               "json_extraction": "Anna 31", "summarise": "word " * 40 + "flood",
               "tool_use_style": "I will call get_weather for Paris", "refusal_safe": "I can't help with recycling"}
        for t in om.BENCH_TASKS:
            self.assertFalse(t.check(bad[t.task_id]), t.task_id)

    def test_five_task_kinds(self):
        self.assertEqual([t.task_id for t in om.BENCH_TASKS],
                         ["code_transform", "json_extraction", "summarise", "tool_use_style", "refusal_safe"])

    def test_pick_routes_filters(self):
        ids = ["gemini/gemini-2.5-flash", "gemini/gemini-embedding-001", "gemini/imagen-4", "groq/whisper-large-v3",
               "groq/openai/gpt-oss-20b", "oc/x"]
        self.assertEqual(om.pick_routes(ids), ["gemini/gemini-2.5-flash", "groq/openai/gpt-oss-20b"])

    def _prompt_to_task(self, body):
        prompt = json.loads(body)["messages"][0]["content"]
        for t in om.BENCH_TASKS:
            if t.prompt == prompt:
                return t.task_id

    def test_run_route_all_pass(self):
        def h(m, u, hd, body, to):
            return 200, chat_body(self.GOOD[self._prompt_to_task(body)])
        c, _ = client_for(h)
        row = om.run_route(c, "gemini/x", sleep=lambda s: None)
        self.assertEqual((row["passed"], row["pass_rate"], row["stopped_early"]), (5, 1.0, False))
        self.assertEqual(row["failure_classes"], [])

    def test_run_route_check_failed_counts(self):
        c, _ = client_for(lambda *a: (200, chat_body("nonsense")))
        row = om.run_route(c, "g/x", sleep=lambda s: None)
        self.assertEqual(row["passed"], 0)
        self.assertTrue(row["stopped_early"])  # 2 identical check_failed in a row
        self.assertEqual(row["tasks_run"], 2)

    def test_stops_after_two_identical_failures(self):
        c, _ = client_for(lambda *a: (429, b"{}"))
        row = om.run_route(c, "g/x", retries=0, sleep=lambda s: None)
        self.assertTrue(row["stopped_early"])
        self.assertEqual(row["tasks_run"], 2)
        self.assertEqual(row["failure_classes"], ["rate_limited"])
        self.assertEqual(len(c._transport.calls), 2)

    def test_retry_bounded(self):
        c, _ = client_for(lambda *a: (503, b"{}"))
        om.run_route(c, "g/x", retries=1, sleep=lambda s: None)
        self.assertEqual(len(c._transport.calls), 4)  # 2 tasks x (1 try + 1 retry)

    def test_retry_recovers(self):
        state = {"n": 0}

        def h(m, u, hd, body, to):
            state["n"] += 1
            if state["n"] == 1:
                return 503, b"{}"
            return 200, chat_body(self.GOOD[self._prompt_to_task(body)])
        c, _ = client_for(h)
        row = om.run_route(c, "g/x", sleep=lambda s: None)
        self.assertEqual(row["passed"], 5)

    def test_non_retryable_not_retried(self):
        c, _ = client_for(lambda *a: (400, b"{}"))
        om.run_route(c, "g/x", retries=3, sleep=lambda s: None)
        self.assertEqual(len(c._transport.calls), 2)

    def test_run_benchmark_results_json_no_secrets(self):
        def h(m, u, hd, body, to):
            if u.endswith("/v1/models"):
                return 200, models_body(["gemini/a", "groq/b", "oc/c", "gemini/embed-embedding"])
            if json.loads(body)["model"] == "groq/b":
                return 401, b"{}"
            return 200, chat_body(self.GOOD[self._prompt_to_task(body)])
        c, _ = client_for(h)
        out = om.run_benchmark(c, now=lambda: "2026-01-01", sleep=lambda s: None)
        s = json.dumps(out)
        self.assertNotIn(FAKE_KEY, s)
        self.assertNotIn("Bearer", s)
        rows = {r["route"]: r for r in out["routes"]}
        self.assertEqual(set(rows), {"gemini/a", "groq/b"})
        self.assertEqual(rows["gemini/a"]["pass_rate"], 1.0)
        self.assertTrue(rows["gemini/a"]["reachable"])
        self.assertFalse(rows["groq/b"]["reachable"])
        self.assertEqual(rows["groq/b"]["failure_classes"], ["auth_error"])
        self.assertEqual(om.reachable_routes(out), ["gemini/a"])

    def test_unlisted_route(self):
        c, _ = client_for(lambda *a: (200, models_body(["gemini/a"])))
        out = om.run_benchmark(c, ["gemini/zzz"], sleep=lambda s: None)
        self.assertEqual(out["routes"][0]["error_class"], "not_listed")

    def test_no_prompt_text_in_results(self):
        c, _ = client_for(lambda *a: (200, models_body([])) if a[1].endswith("models") else (200, chat_body("x")))
        out = om.run_benchmark(c, ["x"], sleep=lambda s: None)
        self.assertNotIn("Anna", json.dumps(out))


class RoutingTests(unittest.TestCase):
    def best(self, **kw):
        return decide(TaskProfile(**kw))

    def test_architecture_goes_to_strongest(self):
        d = self.best(complexity="architecture", criticality="normal")
        self.assertEqual(d.best, CLAUDE_STRONG)

    def test_integration_goes_to_strongest(self):
        self.assertEqual(self.best(complexity="integration").best, CLAUDE_STRONG)

    def test_high_criticality_strongest(self):
        self.assertEqual(self.best(criticality="high", complexity="mechanical").best, CLAUDE_STRONG)

    def test_critical_claude_only(self):
        d = self.best(criticality="critical", complexity="mechanical")
        self.assertEqual({r.route for r in d.ranking}, {CLAUDE_STRONG, CLAUDE_STANDARD})
        self.assertIn(OMNI_GEMINI, dict(d.excluded))

    def test_mechanical_goes_to_omniroute(self):
        d = self.best(complexity="mechanical", criticality="low")
        self.assertEqual(d.best, OMNI_GEMINI)
        self.assertEqual(d.ranking[1].route, OMNI_GROQ)

    def test_search_and_repetitive_cheap(self):
        for c in ("search", "repetitive", "routine"):
            self.assertIn(self.best(complexity=c).best, (OMNI_GEMINI, OMNI_GROQ), c)

    def test_mid_complexity_standard_claude(self):
        self.assertEqual(self.best(complexity="complex", criticality="normal").best, CLAUDE_STANDARD)

    def test_validation_failure_escalates(self):
        d = self.best(complexity="mechanical", criticality="low", validation_failed=True)
        self.assertEqual(d.best, CLAUDE_STRONG)
        self.assertIn("escalation", d.explain())

    def test_repeated_failures_escalate(self):
        self.assertEqual(self.best(complexity="mechanical", criticality="low", failures=2).best, CLAUDE_STRONG)
        self.assertNotEqual(self.best(complexity="mechanical", criticality="low", failures=1).best, CLAUDE_STRONG)

    def test_all_unavailable(self):
        d = decide(TaskProfile(criticality=UNAVAILABLE, complexity=UNAVAILABLE))
        self.assertEqual(d.best, CLAUDE_STRONG)  # fail safe
        text = d.explain()
        for word in ("criticality UNAVAILABLE", "complexity UNAVAILABLE", "estimated context UNAVAILABLE",
                     "context pressure UNAVAILABLE", "deadline UNAVAILABLE", "concurrency UNAVAILABLE",
                     "quota UNAVAILABLE"):
            self.assertIn(word, text)
        self.assertNotIn(PAID_API, [r.route for r in d.ranking])

    def test_signals_unavailable_but_profile_known(self):
        d = decide(TaskProfile(criticality="low", complexity="mechanical"))
        self.assertEqual(d.best, OMNI_GEMINI)

    def test_paid_route_off_by_default(self):
        for kw in ({}, {"criticality": "critical"}, {"complexity": "mechanical", "criticality": "low"}):
            d = self.best(**kw)
            self.assertNotIn(PAID_API, [r.route for r in d.ranking])
            self.assertIn(PAID_API, dict(d.excluded))

    def test_paid_route_only_with_flag_and_last(self):
        d = decide(TaskProfile(complexity="mechanical", criticality="low"), RoutingConfig(allow_paid_api=True))
        self.assertEqual(d.ranking[-1].route, PAID_API)
        self.assertNotEqual(d.best, PAID_API)

    def test_config_default_flag_false(self):
        self.assertIs(RoutingConfig().allow_paid_api, False)

    def test_quota_exhausted_excludes(self):
        d = self.best(complexity="mechanical", criticality="low", quota={OMNI_GEMINI: 0.0})
        self.assertIn(OMNI_GEMINI, dict(d.excluded))
        self.assertEqual(d.best, OMNI_GROQ)

    def test_quota_low_penalises(self):
        d = self.best(complexity="mechanical", criticality="low", quota={OMNI_GEMINI: 0.1})
        self.assertEqual(d.best, OMNI_GROQ)

    def test_quota_unavailable_neutral(self):
        d = self.best(complexity="mechanical", criticality="low", quota={OMNI_GEMINI: UNAVAILABLE})
        self.assertEqual(d.best, OMNI_GEMINI)

    def test_all_routes_exhausted_no_route(self):
        q = {r: 0.0 for r in rt.ROUTE_ORDER}
        d = self.best(quota=q)
        self.assertIsNone(d.best)
        self.assertIn("NO_ROUTE", d.explain())

    def test_context_exceeds_groq_limit(self):
        d = self.best(complexity="mechanical", criticality="low", est_context_tokens=500_000)
        self.assertIn(OMNI_GROQ, dict(d.excluded))
        self.assertEqual(d.best, OMNI_GEMINI)

    def test_context_exceeds_everything(self):
        d = self.best(est_context_tokens=5_000_000)
        self.assertIsNone(d.best)

    def test_large_context_favours_gemini(self):
        d = self.best(complexity="search", criticality="low", est_context_tokens=100_000)
        self.assertEqual(d.best, OMNI_GEMINI)

    def test_high_context_pressure_offloads_mid_work(self):
        calm = self.best(complexity="complex", context_pressure=0.1)
        busy = self.best(complexity="complex", context_pressure=0.9)
        self.assertEqual(calm.best, CLAUDE_STANDARD)
        self.assertIn(busy.best, (OMNI_GEMINI, OMNI_GROQ))

    def test_high_pressure_does_not_offload_strong_work(self):
        d = self.best(complexity="architecture", context_pressure=0.95)
        self.assertEqual(d.best, CLAUDE_STRONG)
        self.assertIn("stays on Claude", d.explain())

    def test_short_deadline_prefers_groq(self):
        d = self.best(complexity="mechanical", criticality="low", deadline_minutes=2,
                      est_context_tokens=1000)
        self.assertEqual(d.best, OMNI_GROQ)

    def test_long_deadline_neutral(self):
        d = self.best(complexity="mechanical", criticality="low", deadline_minutes=120)
        self.assertEqual(d.best, OMNI_GEMINI)

    def test_high_concurrency_boosts_omni(self):
        d = self.best(complexity="complex", concurrency=5)
        self.assertEqual(d.ranking[0].route, CLAUDE_STANDARD)
        omni = [r for r in d.ranking if r.route == OMNI_GEMINI][0]
        self.assertIn("high concurrency", " ".join(omni.reasons))

    def test_unreachable_omniroute_excluded(self):
        cfg = RoutingConfig(reachable_omniroute=(OMNI_GROQ,))
        d = decide(TaskProfile(complexity="mechanical", criticality="low"), cfg)
        self.assertEqual(d.best, OMNI_GROQ)
        self.assertIn(OMNI_GEMINI, dict(d.excluded))

    def test_reachable_none_empty_tuple(self):
        d = decide(TaskProfile(complexity="mechanical", criticality="low"), RoutingConfig(reachable_omniroute=()))
        self.assertTrue(all(r.route.startswith("claude") for r in d.ranking))

    def test_deterministic(self):
        p = TaskProfile(complexity="search", criticality="normal", concurrency=3, deadline_minutes=4)
        self.assertEqual(decide(p), decide(p))

    def test_no_fixed_split_answer_depends_on_profile(self):
        picks = {decide(TaskProfile(complexity=c, criticality=k)).best
                 for c in rt.COMPLEXITIES for k in rt.CRITICALITIES}
        self.assertGreaterEqual(len(picks), 3)

    def test_model_names_attached(self):
        d = self.best(complexity="mechanical", criticality="low")
        self.assertEqual(d.ranking[0].model, "gemini/gemini-2.5-flash")

    def test_explain_every_route_has_reasons(self):
        d = self.best(complexity="mechanical", criticality="low")
        for r in d.ranking:
            self.assertTrue(r.reasons)
        self.assertIn("1.", d.explain())

    def test_invalid_numeric_signals_treated_unavailable(self):
        d = self.best(complexity="mechanical", criticality="low", context_pressure="abc", deadline_minutes=True)
        self.assertEqual(d.best, OMNI_GEMINI)

    def test_unavailable_singleton_falsy(self):
        self.assertFalse(UNAVAILABLE)
        self.assertIs(rt._Unavailable(), UNAVAILABLE)
        self.assertEqual(repr(UNAVAILABLE), "UNAVAILABLE")

    def test_tie_break_order_stable(self):
        d = self.best(complexity="complex", criticality="normal",
                      quota={CLAUDE_STRONG: 0.0, CLAUDE_STANDARD: 0.0})
        self.assertTrue(all(r.route.startswith("omniroute") for r in d.ranking))


if __name__ == "__main__":
    unittest.main()
