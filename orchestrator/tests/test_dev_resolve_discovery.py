"""Offline tests for dev_supervisor.resolve_discovery. Synthetic data only; sockets are blocked.

Run from orchestrator/:  python -B -I -m unittest discover -s tests -p test_dev_resolve_discovery.py
"""
import io
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dev_supervisor import resolve_discovery as rd  # noqa: E402

PID = "11111111-2222-3333-4444-555555555555"


class FakeSock:
    closed = False

    def close(self):
        self.closed = True


def sock_ok(host, port, timeout):
    return FakeSock()


def sock_refused(host, port, timeout):
    raise ConnectionRefusedError()


def sock_timeout(host, port, timeout):
    raise socket.timeout()


def sock_other(host, port, timeout):
    raise OSError("boom")


def procs_open():
    return ["System", "Resolve.exe", "python.exe"]


def procs_closed():
    return ["System", "python.exe"]


def procs_fail():
    raise RuntimeError("no tasklist")


def make_mcp(project="Proj A", pid=PID, timeline="TL1", projects=None, db="Local Database", calls=None,
             wrap=True):
    def w(x):
        return {"result": x} if wrap else x
    answers = {
        ("resolve_control", "runtime_mode"): w({"running": True, "database": {"DbName": db, "DbType": "Disk"}}),
        ("project_manager", "get_current"): w({"name": project, **({"id": pid} if pid else {})}),
        ("project_manager", "list"): w({"projects": projects if projects is not None else [project, "Other"]}),
        ("timeline", "get_name"): w({"name": timeline}),
    }

    def call(tool, action):
        if calls is not None:
            calls.append((tool, action))
        return answers[(tool, action)]
    return call


def mcp_raises(exc):
    def call(tool, action):
        raise exc
    return call


def run(config=None, mcp=None, procs=procs_open, sock=sock_ok):
    return rd.discover(procs, sock, mcp, config)


class ProbeTests(unittest.TestCase):
    def test_running_true(self):
        self.assertEqual(rd.check_running(procs_open), {"status": "OBSERVED", "value": True})

    def test_running_false(self):
        self.assertEqual(rd.check_running(procs_closed)["value"], False)

    def test_running_case_insensitive(self):
        self.assertTrue(rd.check_running(lambda: ["RESOLVE.EXE"])["value"])

    def test_running_lister_fails_unavailable(self):
        r = rd.check_running(procs_fail)
        self.assertEqual(r["status"], "UNAVAILABLE")
        self.assertIn("RuntimeError", r["reason"])

    def test_similar_process_name_not_matched(self):
        self.assertFalse(rd.check_running(lambda: ["ResolveHelper.exe"])["value"])

    def test_bridge_listening(self):
        self.assertEqual(rd.check_bridge(sock_ok), {"status": "OBSERVED", "value": True})

    def test_bridge_closes_socket(self):
        s = FakeSock()
        rd.check_bridge(lambda h, p, t: s)
        self.assertTrue(s.closed)

    def test_bridge_refused_is_observed_false(self):
        self.assertEqual(rd.check_bridge(sock_refused), {"status": "OBSERVED", "value": False})

    def test_bridge_timeout_is_observed_false(self):
        self.assertEqual(rd.check_bridge(sock_timeout)["value"], False)

    def test_bridge_other_oserror_unavailable(self):
        self.assertEqual(rd.check_bridge(sock_other)["status"], "UNAVAILABLE")

    def test_bridge_loopback_only(self):
        with self.assertRaises(ValueError):
            rd.check_bridge(sock_ok, host="192.168.1.5")

    def test_bridge_uses_loopback_and_port(self):
        seen = []
        rd.check_bridge(lambda h, p, t: seen.append((h, p)) or FakeSock())
        self.assertEqual(seen, [("127.0.0.1", 49632)])

    def test_bridge_sends_nothing(self):
        class Strict(FakeSock):
            def send(self, *a):
                raise AssertionError("must not send")
            sendall = send
        rd.check_bridge(lambda h, p, t: Strict())


class DiscoverTests(unittest.TestCase):
    def test_full_observation(self):
        r = run(mcp=make_mcp())
        self.assertEqual(r["project_name"], {"status": "OBSERVED", "value": "Proj A"})
        self.assertEqual(r["project_id"]["value"], PID)
        self.assertEqual(r["timeline_name"]["value"], "TL1")
        self.assertEqual(r["project_list"]["value"], ["Proj A", "Other"])
        self.assertEqual(r["database"]["value"], "Local Database")
        self.assertTrue(r["mcp_answers"]["value"])
        self.assertTrue(r["read_only"])

    def test_unwrapped_replies_accepted(self):
        r = run(mcp=make_mcp(wrap=False))
        self.assertEqual(r["project_name"]["value"], "Proj A")

    def test_folder_always_unavailable(self):
        self.assertEqual(run(mcp=make_mcp())["folder"]["status"], "UNAVAILABLE")

    def test_every_field_labelled(self):
        r = run(mcp=make_mcp())
        for k in ("resolve_running", "bridge_listening", "mcp_answers", "database", "folder", "project_name",
                  "project_id", "timeline_name", "project_list"):
            self.assertIn(r[k]["status"], ("OBSERVED", "UNAVAILABLE"), k)

    def test_only_whitelisted_calls_made(self):
        calls = []
        run(mcp=make_mcp(calls=calls))
        self.assertTrue(set(calls) <= set(rd.READ_ONLY_CALLS))
        self.assertEqual(len(calls), 4)

    def test_whitelist_has_no_mutating_action(self):
        banned = ("load", "create", "save", "delete", "set_", "import", "close", "render", "open", "quit")
        for _tool, action in rd.READ_ONLY_CALLS:
            self.assertFalse(any(action.startswith(b) for b in banned), action)

    def test_non_whitelisted_call_refused(self):
        with self.assertRaises(PermissionError):
            rd._read(make_mcp(), "project_manager", "load")

    def test_mcp_none_all_unavailable(self):
        r = run(mcp=None)
        for k in ("mcp_answers", "project_name", "project_id", "timeline_name", "project_list", "database"):
            self.assertEqual(r[k]["status"], "UNAVAILABLE", k)

    def test_mcp_timeout_reported(self):
        r = run(mcp=mcp_raises(TimeoutError("slow")))
        self.assertEqual(r["mcp_answers"]["status"], "UNAVAILABLE")
        self.assertIn("TimeoutError", r["project_name"]["reason"])

    def test_mcp_connection_error_reported(self):
        r = run(mcp=mcp_raises(ConnectionError("Not connected")))
        self.assertEqual(r["timeline_name"]["status"], "UNAVAILABLE")

    def test_no_unique_id_falls_to_name_database_basis(self):
        r = run(mcp=make_mcp(pid=None))
        self.assertEqual(r["project_id"]["status"], "UNAVAILABLE")
        self.assertEqual(r["identity_basis"], "name+database(+folder unavailable)")

    def test_id_basis(self):
        self.assertEqual(run(mcp=make_mcp())["identity_basis"], "unique_id")

    def test_resolve_closed_bridge_down(self):
        r = run(mcp=None, procs=procs_closed, sock=sock_refused)
        self.assertFalse(r["resolve_running"]["value"])
        self.assertFalse(r["bridge_listening"]["value"])
        self.assertIn("resolve_bridge", r["manual_fallback"])

    def test_bridge_down_but_mcp_ok_gives_fallback(self):
        r = run(mcp=make_mcp(), sock=sock_refused)
        self.assertIn("manual_fallback", r)

    def test_bridge_up_match_no_fallback(self):
        r = run(config={"project_id": PID}, mcp=make_mcp())
        self.assertNotIn("manual_fallback", r)

    def test_report_is_json_serialisable(self):
        json.dumps(run(config={"project_id": PID}, mcp=make_mcp()))

    def test_no_project_open(self):
        mcp = make_mcp()

        def call(tool, action):
            if (tool, action) == ("project_manager", "get_current"):
                return {"result": {}}
            return mcp(tool, action)
        r = run(config={"project_id": PID}, mcp=call)
        self.assertEqual(r["verdict"], "UNAVAILABLE")


class IdentityTests(unittest.TestCase):
    def test_unconfigured_none(self):
        self.assertEqual(run(mcp=make_mcp())["verdict"], "UNCONFIGURED")

    def test_unconfigured_empty_dict(self):
        self.assertEqual(run(config={}, mcp=make_mcp())["verdict"], "UNCONFIGURED")

    def test_name_only_config_is_unconfigured(self):
        self.assertEqual(run(config={"project_name": "Proj A"}, mcp=make_mcp())["verdict"], "UNCONFIGURED")

    def test_match_by_id(self):
        self.assertEqual(run(config={"project_id": PID}, mcp=make_mcp())["verdict"], "MATCH")

    def test_mismatch_by_id_even_with_same_name(self):
        r = run(config={"project_id": "other-id", "project_name": "Proj A", "database": "Local Database"},
                mcp=make_mcp())
        self.assertEqual(r["verdict"], "MISMATCH")
        self.assertEqual(r["identity"]["compared"], "unique_id")

    def test_id_configured_but_not_exposed_unavailable_no_name_fallback(self):
        r = run(config={"project_id": PID, "project_name": "Proj A", "database": "Local Database"},
                mcp=make_mcp(pid=None))
        self.assertEqual(r["verdict"], "UNAVAILABLE")

    def test_match_by_name_and_database(self):
        r = run(config={"project_name": "Proj A", "database": "Local Database"}, mcp=make_mcp(pid=None))
        self.assertEqual(r["verdict"], "MATCH")

    def test_mismatch_wrong_name(self):
        r = run(config={"project_name": "Proj B", "database": "Local Database"}, mcp=make_mcp(pid=None))
        self.assertEqual(r["verdict"], "MISMATCH")

    def test_mismatch_wrong_database(self):
        r = run(config={"project_name": "Proj A", "database": "Cloud DB"}, mcp=make_mcp(pid=None))
        self.assertEqual(r["verdict"], "MISMATCH")

    def test_folder_configured_unavailable(self):
        r = run(config={"project_name": "Proj A", "database": "Local Database", "folder": "/x"},
                mcp=make_mcp(pid=None))
        self.assertEqual(r["verdict"], "UNAVAILABLE")

    def test_unavailable_when_mcp_down(self):
        self.assertEqual(run(config={"project_id": PID}, mcp=None)["verdict"], "UNAVAILABLE")

    def test_unconfigured_wins_over_unavailable(self):
        self.assertEqual(run(config=None, mcp=None)["verdict"], "UNCONFIGURED")

    def test_require_match_refuses_mismatch(self):
        r = run(config={"project_id": "x"}, mcp=make_mcp())
        with self.assertRaises(rd.IdentityMismatch):
            rd.require_match(r)

    def test_require_match_passes_match(self):
        rd.require_match(run(config={"project_id": PID}, mcp=make_mcp()))

    def test_require_match_does_not_raise_on_unavailable(self):
        rd.require_match({"verdict": "UNAVAILABLE"})


class ConfigTests(unittest.TestCase):
    def test_load_none_without_path_or_env(self):
        self.assertIsNone(rd.load_config(None, environ={}))

    def test_load_missing_file_is_none(self):
        self.assertIsNone(rd.load_config("does-not-exist.json", environ={}))

    def test_load_from_env(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "id.json")
            p.write_text(json.dumps({"project_id": PID}))
            self.assertEqual(rd.load_config(None, environ={rd.CONFIG_ENV: str(p)}), {"project_id": PID})

    def test_load_corrupt_raises(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "id.json")
            p.write_text("{nope")
            with self.assertRaises(ValueError):
                rd.load_config(str(p), environ={})

    def test_load_non_object_raises(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "id.json")
            p.write_text("[1]")
            with self.assertRaises(ValueError):
                rd.load_config(str(p), environ={})


class CliTests(unittest.TestCase):
    def _run(self, args, procs=procs_open, sock=sock_ok, runner=None):
        out = io.StringIO()
        code = rd.main(args, process_lister=procs, socket_factory=sock, runner=runner or mock.Mock(), out=out)
        return code, out.getvalue()

    def _files(self, d, cfg, snap):
        c, s = Path(d, "c.json"), Path(d, "s.json")
        c.write_text(json.dumps(cfg))
        s.write_text(json.dumps(snap))
        return ["--config", str(c), "--mcp-snapshot", str(s)]

    @staticmethod
    def _snap(pid=PID):
        return {"runtime_mode": {"database": {"DbName": "Local Database"}},
                "current_project": {"name": "Proj A", "id": pid},
                "projects": ["Proj A"], "timeline": {"name": "TL1"}}

    def test_cli_match_exit_0_and_json(self):
        with tempfile.TemporaryDirectory() as d:
            code, out = self._run(self._files(d, {"project_id": PID}, self._snap()))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["verdict"], "MATCH")

    def test_cli_mismatch_nonzero(self):
        with tempfile.TemporaryDirectory() as d:
            code, _ = self._run(self._files(d, {"project_id": "zzz"}, self._snap()))
        self.assertEqual(code, 1)

    def test_cli_unconfigured_nonzero(self):
        with tempfile.TemporaryDirectory() as d:
            s = Path(d, "s.json")
            s.write_text(json.dumps(self._snap()))
            with mock.patch.dict("os.environ", {}, clear=True):
                code, out = self._run(["--mcp-snapshot", str(s)])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["verdict"], "UNCONFIGURED")

    def test_cli_unavailable_nonzero(self):
        with tempfile.TemporaryDirectory() as d:
            c = Path(d, "c.json")
            c.write_text(json.dumps({"project_id": PID}))
            code, out = self._run(["--config", str(c)], sock=sock_refused)
        self.assertEqual(code, 3)
        self.assertIn("manual_fallback", json.loads(out))

    def test_cli_bad_config_unavailable(self):
        with tempfile.TemporaryDirectory() as d:
            c = Path(d, "c.json")
            c.write_text("{bad")
            code, _ = self._run(["--config", str(c)])
        self.assertEqual(code, 3)

    def test_start_bridge_does_not_launch_when_running(self):
        runner = mock.Mock()
        self._run(["--start-bridge", "--resolve-exe", "x.exe"], runner=runner)
        runner.assert_not_called()

    def test_start_bridge_launches_resolve_only_when_closed_and_prints_manual(self):
        runner = mock.Mock()
        code, out = self._run(["--start-bridge", "--resolve-exe", "x.exe"], procs=procs_closed,
                              sock=sock_refused, runner=runner)
        runner.assert_called_once_with(["x.exe"])
        rep = json.loads(out)
        self.assertTrue(rep["launch_resolve"])
        self.assertIn("resolve_bridge", rep["bridge_start"])

    def test_start_bridge_without_exe_launches_nothing(self):
        runner = mock.Mock()
        self._run(["--start-bridge"], procs=procs_closed, runner=runner)
        runner.assert_not_called()

    def test_no_flag_never_launches(self):
        runner = mock.Mock()
        self._run([], procs=procs_closed, runner=runner)
        runner.assert_not_called()

    def test_default_socket_blocked_in_tests(self):
        with mock.patch("socket.create_connection", side_effect=OSError("blocked")):
            r = rd.check_bridge(rd.default_socket_factory)
        self.assertEqual(r["status"], "UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
