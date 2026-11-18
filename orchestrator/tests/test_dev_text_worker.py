"""Text worker tests (T-DEV-019). Synthetic only; sockets blocked, fake transport.

Run from orchestrator/:  python -B -I -m unittest discover -s tests -p test_dev_text_worker.py
"""
import io
import json
import socket
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dev_supervisor import omniroute as om  # noqa: E402
from dev_supervisor import text_worker as tw  # noqa: E402

R1, R2, R3 = "gemini/gemini-3.1-flash-lite", "gemini/gemini-3-flash-preview", "groq/openai/gpt-oss-20b"
FAKE_KEY = "sk-" + "fake" * 8  # built at runtime


def no_net(*a, **k):
    raise AssertionError("network used in a unit test")


def chat_body(text):
    return json.dumps({"choices": [{"message": {"content": text}}],
                       "usage": {"prompt_tokens": 5, "completion_tokens": 7}}).encode()


class Fake:
    """Per-route scripted responses: route -> list of (status, text) consumed in order (last repeats)."""

    def __init__(self, script):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls = []
        self.headers = []
        self.bodies = []

    def __call__(self, method, url, headers, body, timeout):
        payload = json.loads(body.decode())
        route = payload["model"]
        self.calls.append(route)
        self.headers.append(dict(headers))
        self.bodies.append(payload)
        seq = self.script.get(route, [(404, "")])
        item = seq.pop(0) if len(seq) > 1 else seq[0]
        if item == "timeout":
            raise om.TransportError("timeout")
        status, text = item
        return status, (chat_body(text) if status == 200 else b'{"error":"x"}')


def make(script, **kw):
    t = Fake(script)
    kw.setdefault("key_provider", lambda: FAKE_KEY)
    kw.setdefault("environ", {})
    return om.OmniRouteClient(t, **kw), t


def task(kind="summarise", **kw):
    base = {"summarise": {"input": "Heavy rain flooded the river and closed two bridges."},
            "classify": {"input": "I love this", "labels": ["positive", "negative"]},
            "extract_json": {"input": "Anna is 31.", "required_keys": ["name", "age"]},
            "review_notes": {"input": "todo: fix tests; ship docs"}}[kind]
    d = {"kind": kind}
    d.update(base)
    d.update(kw)
    return d


OK_TEXT = {"summarise": "Floods closed two bridges.", "classify": "positive",
           "extract_json": '{"name": "Anna", "age": 31}', "review_notes": "- fix tests\n- ship docs"}


class Base(unittest.TestCase):
    def setUp(self):
        for name in ("socket", "create_connection"):
            p = mock.patch.object(socket, name, no_net)
            p.start()
            self.addCleanup(p.stop)
        self.slept = []

    def run_t(self, script, routes, t=None, **kw):
        c, tr = make(script)
        res = tw.run_task(t or task(), c, routes, sleep=self.slept.append, **kw)
        return res, tr


class CheckerTests(Base):
    def test_summarise_ok(self):
        self.assertEqual(tw.run_checker(task(), "Floods closed two bridges."), (True, "ok"))

    def test_summarise_too_short(self):
        self.assertEqual(tw.run_checker(task(), "Floods."), (False, "too_short"))

    def test_summarise_too_long(self):
        self.assertEqual(tw.run_checker(task(max_words=5), "one two three four five six"), (False, "too_long"))

    def test_classify_ok_and_normalised(self):
        self.assertTrue(tw.run_checker(task("classify"), " Positive.\n")[0])
        self.assertTrue(tw.run_checker(task("classify"), '"negative"')[0])

    def test_classify_not_allowed(self):
        self.assertEqual(tw.run_checker(task("classify"), "neutral"), (False, "label_not_allowed"))
        self.assertFalse(tw.run_checker(task("classify"), "positive and negative")[0])

    def test_json_ok_and_fenced(self):
        self.assertTrue(tw.run_checker(task("extract_json"), '{"name":"A","age":1}')[0])
        self.assertTrue(tw.run_checker(task("extract_json"), '```json\n{"name":"A","age":1}\n```')[0])

    def test_json_invalid(self):
        self.assertEqual(tw.run_checker(task("extract_json"), "name: Anna"), (False, "invalid_json"))

    def test_json_not_object(self):
        self.assertEqual(tw.run_checker(task("extract_json"), "[1,2]"), (False, "not_an_object"))

    def test_json_missing_keys(self):
        self.assertEqual(tw.run_checker(task("extract_json"), '{"name":"A"}'), (False, "missing_keys"))

    def test_review_ok(self):
        self.assertEqual(tw.run_checker(task("review_notes"), OK_TEXT["review_notes"]), (True, "ok"))

    def test_review_empty_and_lines(self):
        self.assertEqual(tw.run_checker(task("review_notes"), "  \n "), (False, "empty"))
        self.assertEqual(tw.run_checker(task("review_notes", max_lines=2), "- a b\n- c d\n- e f"),
                         (False, "too_many_lines"))

    def test_review_too_long(self):
        self.assertEqual(tw.run_checker(task("review_notes", max_words=4), "- a b c d e f"), (False, "too_long"))


class TaskValidationTests(Base):
    def test_bad_kind(self):
        with self.assertRaises(tw.TaskError):
            tw.load_task({"kind": "code", "input": "x"})

    def test_empty_input(self):
        with self.assertRaises(tw.TaskError):
            tw.load_task({"kind": "summarise", "input": "  "})

    def test_oversize_input(self):
        with self.assertRaises(tw.TaskError):
            tw.load_task(task(input="x" * (tw.MAX_INPUT_CHARS + 1)))

    def test_classify_needs_labels(self):
        with self.assertRaises(tw.TaskError):
            tw.load_task({"kind": "classify", "input": "x", "labels": ["one"]})

    def test_json_needs_keys(self):
        with self.assertRaises(tw.TaskError):
            tw.load_task({"kind": "extract_json", "input": "x"})

    def test_bad_limit(self):
        with self.assertRaises(tw.TaskError):
            tw.load_task(task(max_words=0))

    def test_prompt_contains_labels_and_keys(self):
        self.assertIn("positive, negative", tw.build_prompt(task("classify")))
        self.assertIn("name, age", tw.build_prompt(task("extract_json")))


class RunTests(Base):
    def test_each_kind_checked(self):
        for kind in tw.KINDS:
            res, _ = self.run_t({R1: [(200, OK_TEXT[kind])]}, [R1], task(kind))
            self.assertEqual(res["label"], "CHECKED", kind)
            self.assertEqual(res["route"], R1)
            self.assertEqual(res["attempt_count"], 1)

    def test_never_verified(self):
        res, _ = self.run_t({R1: [(200, OK_TEXT["summarise"])]}, [R1])
        self.assertNotIn("VERIFIED", json.dumps(res))

    def test_fallback_on_rate_limited(self):
        res, tr = self.run_t({R1: [(429, "")], R2: [(200, OK_TEXT["summarise"])]}, [R1, R2])
        self.assertEqual(tr.calls, [R1, R2])
        self.assertEqual(res["route"], R2)
        self.assertIn("rate_limited", res["failure_classes"])

    def test_fallback_on_not_found_and_auth(self):
        res, tr = self.run_t({R3: [(403, "")], R1: [(404, "")], R2: [(200, OK_TEXT["summarise"])]}, [R3, R1, R2])
        self.assertEqual(tr.calls, [R3, R1, R2])
        self.assertEqual(res["failure_classes"], ["auth_error", "not_found"])

    def test_timeout_retried_once_then_fallback(self):
        res, tr = self.run_t({R1: ["timeout"], R2: [(200, OK_TEXT["summarise"])]}, [R1, R2])
        self.assertEqual(tr.calls, [R1, R1, R2])
        self.assertEqual(len(self.slept), 1)
        self.assertEqual(res["route"], R2)

    def test_timeout_then_success_same_route(self):
        res, tr = self.run_t({R1: ["timeout", (200, OK_TEXT["summarise"])]}, [R1])
        self.assertEqual(tr.calls, [R1, R1])
        self.assertEqual(res["label"], "CHECKED")

    def test_rate_limited_not_retried_same_route(self):
        _, tr = self.run_t({R1: [(429, "")], R2: [(429, "")]}, [R1, R2])
        self.assertEqual(tr.calls, [R1, R2])

    def test_check_failed_falls_back_without_retry(self):
        res, tr = self.run_t({R1: [(200, "positive and also")], R2: [(200, "negative")]}, [R1, R2],
                             task("classify"))
        self.assertEqual(tr.calls, [R1, R2])
        self.assertEqual(res["route"], R2)
        self.assertIn("check_failed", res["failure_classes"])

    def test_all_routes_fail(self):
        res, _ = self.run_t({R1: [(429, "")], R2: [(404, "")]}, [R1, R2])
        self.assertEqual(res["label"], "REPORTED")
        self.assertEqual(res["status"], "FAILED")
        self.assertIsNone(res["route"])
        self.assertEqual(res["checker"]["reason"], "no_output")

    def test_check_failed_stays_reported(self):
        res, _ = self.run_t({R1: [(200, "neutral")]}, [R1], task("classify"))
        self.assertEqual(res["label"], "REPORTED")
        self.assertEqual(res["status"], "CHECK_FAILED")
        self.assertEqual(res["route"], R1)
        self.assertFalse(res["checker"]["passed"])

    def test_total_attempts_bounded(self):
        routes = [R1, R2, R3]
        res, tr = self.run_t({R1: ["timeout"], R2: ["timeout"], R3: ["timeout"]}, routes, max_total_attempts=3)
        self.assertEqual(len(tr.calls), 3)

    def test_stops_after_success(self):
        _, tr = self.run_t({R1: [(200, OK_TEXT["summarise"])], R2: [(200, "x")]}, [R1, R2])
        self.assertEqual(tr.calls, [R1])

    def test_no_tools_in_request(self):
        _, tr = self.run_t({R1: [(200, OK_TEXT["summarise"])]}, [R1])
        self.assertNotIn("tools", tr.bodies[0])
        self.assertNotIn("tool_choice", tr.bodies[0])

    def test_attempt_records_have_latency_and_status(self):
        res, _ = self.run_t({R1: [(429, "")], R2: [(200, OK_TEXT["summarise"])]}, [R1, R2])
        for a in res["attempts"]:
            self.assertIn("latency_s", a)
            self.assertIn("status", a)
        self.assertIsInstance(res["latency_total_s"], float)

    def test_no_key_in_result(self):
        res, tr = self.run_t({R1: [(200, OK_TEXT["summarise"])]}, [R1])
        self.assertIn("Authorization", tr.headers[0])  # key was used on the wire only
        self.assertNotIn(FAKE_KEY, json.dumps(res))

    def test_non_free_route_rejected(self):
        c, _ = make({})
        with self.assertRaises(tw.TaskError):
            tw.run_task(task(), c, ["openai/gpt-4o", "anthropic/claude"])

    def test_non_free_routes_filtered_in_run(self):
        res, tr = self.run_t({R1: [(200, OK_TEXT["summarise"])]}, ["openai/gpt-4o", R1])
        self.assertEqual(tr.calls, [R1])


class RouteOrderTests(Base):
    BENCH = {"routes": [
        {"route": R2, "pass_rate": 0.8, "latency_mean_s": 4.5},
        {"route": R1, "pass_rate": 1.0, "latency_mean_s": 8.2},
        {"route": "gemini/gemini-2.5-flash", "pass_rate": 0.0, "latency_mean_s": None},
        {"route": "gemini/fast", "pass_rate": 1.0, "latency_mean_s": 2.0},
        {"route": "paid/model", "pass_rate": 1.0, "latency_mean_s": 1.0},
    ]}

    def test_benchmark_order(self):
        self.assertEqual(tw.order_routes(self.BENCH), ["gemini/fast", R1, R2])

    def test_default_order(self):
        self.assertEqual(tw.order_routes(), list(tw.DEFAULT_ROUTES))

    def test_explicit_wins_over_benchmark(self):
        self.assertEqual(tw.order_routes(self.BENCH, [R2, R1, R2]), [R2, R1])

    def test_paid_filtered_from_explicit(self):
        self.assertEqual(tw.order_routes(None, ["openai/gpt-4", R1]), [R1])

    def test_benchmark_without_passing_rows_uses_default(self):
        self.assertEqual(tw.order_routes({"routes": [{"route": R1, "pass_rate": 0.0}]}), list(tw.DEFAULT_ROUTES))


class CliTests(Base):
    def _files(self, d, t):
        tp, op = Path(d) / "t.json", Path(d) / "o.json"
        tp.write_text(json.dumps(t), encoding="utf-8")
        return str(tp), str(op)

    def _main(self, args, transport):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = tw.main(args, transport=transport, sleep=lambda s: None)
        return rc, buf.getvalue()

    def test_cli_success_writes_result(self):
        with tempfile.TemporaryDirectory() as d:
            tp, op = self._files(d, task())
            tr = Fake({R1: [(200, OK_TEXT["summarise"])]})
            rc, out = self._main(["--task", tp, "--out", op, "--routes", R1], tr)
            self.assertEqual(rc, 0)
            res = json.loads(Path(op).read_text(encoding="utf-8"))
            self.assertEqual(res["label"], "CHECKED")
            self.assertIn("route=" + R1, out)

    def test_cli_remote_url_refused(self):
        with tempfile.TemporaryDirectory() as d:
            tp, op = self._files(d, task())
            tr = Fake({})
            rc, out = self._main(["--task", tp, "--out", op, "--base-url", "https://example.com"], tr)
            self.assertEqual(rc, 2)
            self.assertIn("nonlocal_refused", out)
            self.assertEqual(tr.calls, [])
            self.assertFalse(Path(op).exists())

    def test_cli_remote_allowed_explicitly(self):
        with tempfile.TemporaryDirectory() as d:
            tp, op = self._files(d, task())
            tr = Fake({R1: [(200, OK_TEXT["summarise"])]})
            rc, _ = self._main(["--task", tp, "--out", op, "--base-url", "https://example.com",
                                "--allow-remote", "--routes", R1], tr)
            self.assertEqual(rc, 0)

    def test_cli_bad_task_refused(self):
        with tempfile.TemporaryDirectory() as d:
            tp, op = self._files(d, {"kind": "nope", "input": "x"})
            rc, _ = self._main(["--task", tp, "--out", op], Fake({}))
            self.assertEqual(rc, 2)

    def test_cli_only_paid_routes_refused(self):
        with tempfile.TemporaryDirectory() as d:
            tp, op = self._files(d, task())
            tr = Fake({})
            rc, out = self._main(["--task", tp, "--out", op, "--routes", "openai/gpt-4o"], tr)
            self.assertEqual(rc, 2)
            self.assertEqual(tr.calls, [])

    def test_cli_benchmark_ordering(self):
        with tempfile.TemporaryDirectory() as d:
            tp, op = self._files(d, task())
            bp = Path(d) / "b.json"
            bp.write_text(json.dumps(RouteOrderTests.BENCH), encoding="utf-8")
            tr = Fake({"gemini/fast": [(429, "")], R1: [(200, OK_TEXT["summarise"])]})
            rc, _ = self._main(["--task", tp, "--out", op, "--benchmark", str(bp)], tr)
            self.assertEqual(tr.calls, ["gemini/fast", R1])
            self.assertEqual(rc, 0)

    def test_cli_failure_exit_1_no_key_in_file(self):
        with tempfile.TemporaryDirectory() as d:
            tp, op = self._files(d, task())
            tr = Fake({R1: [(429, "")], R2: [(429, "")]})
            rc, _ = self._main(["--task", tp, "--out", op], tr)
            self.assertEqual(rc, 1)
            self.assertNotIn(FAKE_KEY, Path(op).read_text(encoding="utf-8"))

    def test_docstring_records_tool_limitation(self):
        self.assertIn("thought_signature", tw.__doc__)


if __name__ == "__main__":
    unittest.main()
