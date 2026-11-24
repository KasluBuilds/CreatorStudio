import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dev_supervisor import dispatch_routing as dr  # noqa: E402
from dev_supervisor import memory_guard as mg  # noqa: E402
from test_dev_dispatch_routing import GATEWAY, route, write_registry  # noqa: E402


class FloorTests(unittest.TestCase):
    def test_default_when_unset_or_invalid(self):
        self.assertEqual(mg.min_free_mb({}), mg.DEFAULT_MIN_FREE_MB)
        for bad in ("", "abc", "-5", "1.5", "99999999999", " "):
            self.assertEqual(mg.min_free_mb({mg.ENV_MIN_FREE_MB: bad}), mg.DEFAULT_MIN_FREE_MB, bad)

    def test_valid_values(self):
        self.assertEqual(mg.min_free_mb({mg.ENV_MIN_FREE_MB: "2048"}), 2048)
        self.assertEqual(mg.min_free_mb({mg.ENV_MIN_FREE_MB: " 512 "}), 512)
        self.assertEqual(mg.min_free_mb({mg.ENV_MIN_FREE_MB: "0"}), 0)


class CheckTests(unittest.TestCase):
    def test_at_and_above_floor_pass(self):
        self.assertEqual(mg.check(1024, 1024), (True, ""))
        self.assertTrue(mg.check(5000, 1024)[0])

    def test_below_floor_names_both_numbers(self):
        ok, why = mg.check(700, 1024)
        self.assertFalse(ok)
        self.assertIn("700 MB free", why)
        self.assertIn("1024 MB required", why)

    def test_unreadable_values_refuse(self):
        for bad in (None, True, "900", 1.5, -1):
            ok, why = mg.check(bad, 0)
            self.assertFalse(ok, repr(bad))
            self.assertIn("could not be read", why)


class ReaderTests(unittest.TestCase):
    def test_reader_never_raises_and_returns_none_or_nonnegative_int(self):
        value = mg.free_memory_mb()
        self.assertTrue(value is None or (isinstance(value, int) and value >= 0))

    def test_reader_failure_is_none(self):
        with mock.patch.object(mg, "_windows_available_mb", side_effect=OSError("x")), \
                mock.patch.object(mg, "_linux_available_mb", side_effect=OSError("x")):
            self.assertIsNone(mg.free_memory_mb())


class GateTests(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp())
        write_registry(self.state, [route()])

    def decide(self, **kw):
        args = dict(provider="gemini", model="auto/coding", active_workers=0, state_dir=self.state,
                    base_env=dict(GATEWAY))
        args.update(kw)
        return dr.decide_worker_dispatch(**args)

    def test_enough_memory_allows(self):
        self.assertTrue(self.decide(free_memory_mb=8192).allowed)

    def test_low_memory_refuses_with_reason(self):
        d = self.decide(free_memory_mb=300)
        self.assertFalse(d.allowed)
        self.assertIn("memory guard", d.reason)
        self.assertIn("300 MB free", d.reason)

    def test_unreadable_memory_refuses(self):
        self.assertFalse(self.decide(free_memory_mb=None).allowed)

    def test_env_floor_is_honoured(self):
        with mock.patch.dict("os.environ", {mg.ENV_MIN_FREE_MB: "4096"}):
            self.assertFalse(self.decide(free_memory_mb=2048).allowed)
            self.assertTrue(self.decide(free_memory_mb=4096).allowed)

    def test_reading_is_taken_from_the_machine_when_not_passed(self):
        with mock.patch.object(mg, "free_memory_mb", return_value=100):
            self.assertFalse(self.decide().allowed)
        with mock.patch.object(mg, "free_memory_mb", return_value=9000):
            self.assertTrue(self.decide().allowed)

    def test_concurrency_cap_still_applies_first(self):
        d = self.decide(active_workers=1, free_memory_mb=9000)
        self.assertFalse(d.allowed)
        self.assertIn("concurrency cap", d.reason)


if __name__ == "__main__":
    unittest.main()
