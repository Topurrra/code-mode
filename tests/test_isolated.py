# SPDX-License-Identifier: MIT
"""Cases the real CLI cannot reach on purpose: races and half-written files.

Run from the project root:  python -m unittest discover -s tests -v

Everything that can be shown end to end is in ``e2e.py``. What is here needs
a state that a command line cannot produce reliably: two threads finding the
same stale lock, a log cut in the middle of a line, a worker that died
before it started. Each case quotes its entry in ``tests/FAILURE_MODES.md``.
These cases use real files in a temporary directory and nothing else.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from codemode_runtime import runtime as runtime_module  # noqa: E402
from codemode_runtime.catalog import Catalog, ToolListCache  # noqa: E402
from codemode_runtime.config import DEMO_CONFIG, config_from_dict  # noqa: E402
from codemode_runtime.connectors import Connector  # noqa: E402
from codemode_runtime.errors import BusyError, ConfigError, NotFoundError, StateError, ToolFailure  # noqa: E402
from codemode_runtime.helpers import load_helpers  # noqa: E402
from codemode_runtime.policy import Policy  # noqa: E402
from codemode_runtime.procutil import pid_alive  # noqa: E402
from codemode_runtime.records import ExecutionStore  # noqa: E402
from codemode_runtime.runtime import Runtime  # noqa: E402


def dead_pid() -> int:
    """The id of a process that has exited."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


class Temp(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="codemode-isolated-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        self.store = ExecutionStore(self.home)


class LogReading(Temp):
    def write(self, exec_id: str, data: bytes) -> None:
        folder = self.home / "executions"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / (exec_id + ".jsonl")).write_bytes(data)

    def test_R7_a_half_written_last_line_is_ignored(self):
        whole = b'{"type":"start","id":"ex_a","ts":"2026-01-01T00:00:00Z"}\n{"type":"pass_start","pass":1,"pid":1}\n'
        for tail in (b'{"type":"call","seq":1,"kind":"tool","na', b'{"type":"end","status":"comp', b"{", b'{"type":'):
            self.write("ex_a", whole + tail)
            entries = self.store.read("ex_a")
            self.assertEqual([e["type"] for e in entries], ["start", "pass_start"], tail)
            self.assertEqual(self.store.view("ex_a").status, "running")

    def test_R7_a_damaged_line_in_the_middle_is_an_error_that_names_the_file(self):
        self.write("ex_b", b'{"type":"start","id":"ex_b"}\n{"type":"call","seq":1,"ki\n{"type":"end","status":"completed"}\n')
        with self.assertRaises(NotFoundError) as caught:
            self.store.read("ex_b")
        self.assertIn("line 2", caught.exception.message)
        self.assertIn("ex_b.jsonl", caught.exception.hint)

    def test_R7_blank_lines_and_a_missing_final_newline_are_fine(self):
        self.write("ex_c", b'{"type":"start","id":"ex_c"}\n\n\r\n{"type":"end","status":"completed"}')
        self.assertEqual(self.store.view("ex_c").status, "completed")


class Locks(Temp):
    def lock_file(self, exec_id: str) -> Path:
        folder = self.home / "executions"
        folder.mkdir(parents=True, exist_ok=True)
        return folder / (exec_id + ".lock")

    def test_R8_the_lock_of_a_dead_process_is_cleared(self):
        self.lock_file("ex_a").write_text(str(dead_pid()))
        with self.store.lock("ex_a"):
            self.assertEqual(self.store.holder("ex_a"), os.getpid())
        self.assertFalse(self.lock_file("ex_a").exists())

    def test_R8_the_lock_of_a_live_process_is_never_cleared(self):
        self.lock_file("ex_b").write_text(str(os.getpid()))
        for _ in range(3):
            with self.assertRaises(BusyError):
                with self.store.lock("ex_b"):
                    self.fail("took a lock that a live process holds")
        self.assertEqual(self.lock_file("ex_b").read_text(), str(os.getpid()))

    def test_R8_a_lock_file_with_no_readable_owner_counts_as_stale(self):
        self.lock_file("ex_c").write_text("not a pid")
        with self.store.lock("ex_c"):
            pass

    def test_R8_many_threads_finding_one_stale_lock_never_hold_it_together(self):
        for round_no in range(20):
            exec_id = "ex_race%d" % round_no
            self.lock_file(exec_id).write_text(str(dead_pid()))
            inside = []
            most = [0]
            guard = threading.Lock()
            start = threading.Barrier(8)

            def contend() -> None:
                start.wait()
                try:
                    with self.store.lock(exec_id):
                        with guard:
                            inside.append(1)
                            most[0] = max(most[0], len(inside))
                        threading.Event().wait(0.01)
                        with guard:
                            inside.pop()
                except BusyError:
                    pass

            threads = [threading.Thread(target=contend) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(most[0], 1, "round %d: %d holders at once" % (round_no, most[0]))

    def test_pid_alive(self):
        self.assertTrue(pid_alive(os.getpid()))
        self.assertFalse(pid_alive(dead_pid()))
        for odd in (0, -1, None, "x", ""):
            self.assertFalse(pid_alive(odd), repr(odd))


class StubConnector(Connector):
    """Counts how often its tools are listed, which is what starts a server."""

    cacheable = True

    def __init__(self, names, stamp="stamp-1"):
        self.names = list(names)
        self.listed = 0
        self.current_stamp = stamp

    def list_tools(self):
        self.listed += 1
        return [{"name": name, "description": "", "inputSchema": {"type": "object", "properties": {}}} for name in self.names]

    def stamp(self):
        return self.current_stamp


class ToolListCaching(Temp):
    def catalog(self, connector, ttl=300.0):
        return Catalog({"srv": connector}, Policy(default="allow"), lambda: [], cache=ToolListCache(self.home / "cache", ttl))

    def test_M14_a_second_process_does_not_list_the_tools_again(self):
        first = StubConnector(["alpha", "beta"])
        self.assertEqual(sorted(e["path"] for e in self.catalog(first).search("")), ["srv.alpha", "srv.beta"])
        self.assertEqual(first.listed, 1)
        second = StubConnector(["alpha", "beta"])
        catalog = self.catalog(second)
        self.assertEqual(sorted(e["path"] for e in catalog.search("")), ["srv.alpha", "srv.beta"])
        self.assertEqual(catalog.resolve("srv.alpha").name, "alpha")
        self.assertEqual(second.listed, 0)

    def test_M14_a_changed_stamp_drops_the_cached_list(self):
        self.catalog(StubConnector(["alpha"])).search("")
        edited = StubConnector(["alpha", "gamma"], stamp="stamp-2")
        self.assertEqual(sorted(e["path"] for e in self.catalog(edited).search("")), ["srv.alpha", "srv.gamma"])
        self.assertEqual(edited.listed, 1)

    def test_M14_a_tool_the_cached_list_lacks_is_looked_up_on_the_server(self):
        self.catalog(StubConnector(["alpha"])).search("")
        grown = StubConnector(["alpha", "new_tool"])          # same stamp: the cache cannot know
        catalog = self.catalog(grown)
        self.assertEqual(catalog.resolve("srv.new_tool").name, "new_tool")
        self.assertEqual(grown.listed, 1)
        same = StubConnector(["alpha"])
        catalog = self.catalog(same)
        with self.assertRaises(ToolFailure) as caught:
            catalog.resolve("srv.missing")
        self.assertEqual(caught.exception.kind, "UnknownTool")   # asked the server once, then said so
        self.assertEqual(same.listed, 1)

    def test_M14_an_old_entry_or_a_cache_that_is_off_is_not_used(self):
        self.catalog(StubConnector(["alpha"])).search("")
        off = StubConnector(["alpha"])
        self.catalog(off, ttl=0).search("")
        self.assertEqual(off.listed, 1)
        cache_file = self.home / "cache" / "tools-srv.json"
        entry = json.loads(cache_file.read_text(encoding="ascii"))
        entry["saved"] -= 1000
        cache_file.write_text(json.dumps(entry), encoding="ascii")
        stale = StubConnector(["alpha"])
        self.catalog(stale, ttl=300).search("")
        self.assertEqual(stale.listed, 1)
        cache_file.write_text("{ not json", encoding="ascii")
        broken = StubConnector(["alpha"])
        self.catalog(broken).search("")
        self.assertEqual(broken.listed, 1)                       # a damaged cache file is only a slower start

    def test_M14_a_connector_that_starts_nothing_is_not_cached(self):
        plain = StubConnector(["alpha"])
        plain.cacheable = False
        self.catalog(plain).search("")
        self.assertFalse((self.home / "cache").exists())

    def test_M16_a_changed_server_is_listed_again_and_stopped(self):
        connector = StubConnector(["alpha"])
        closed = []
        connector.close = lambda: closed.append(True)            # type: ignore[method-assign]
        catalog = self.catalog(connector, ttl=0)
        catalog.search("")
        self.assertEqual(catalog.refresh_changed(), [])
        connector.current_stamp = "stamp-2"
        connector.names.append("beta")
        self.assertEqual(catalog.refresh_changed(), ["srv"])
        self.assertEqual(closed, [True])
        self.assertEqual(sorted(e["path"] for e in catalog.search("")), ["srv.alpha", "srv.beta"])


class HelperFiles(Temp):
    def folder(self, **files: str) -> Path:
        folder = self.home / "codemode_helpers"
        folder.mkdir()
        for name, text in files.items():
            (folder / name).write_text(text, encoding="utf-8")
        return folder

    def test_P4_which_files_are_helper_modules(self):
        folder = self.folder(**{"ci.py": "A = 1\n", "git_tools.py": "B = 2\n", "_private.py": "raise RuntimeError\n", "notes.txt": "not python"})
        (folder / "sub").mkdir()
        (folder / "sub" / "deeper.py").write_text("C = 3\n")
        self.assertEqual(load_helpers(folder, 10 ** 6), {"ci": "A = 1\n", "git_tools": "B = 2\n"})

    def test_P4_a_name_that_cannot_be_a_module_is_an_error_that_suggests_one(self):
        for bad in ("Bad-Name.py", "2fast.py", "CamelCase.py"):
            folder = self.home / ("h_" + bad.replace(".", "_"))
            folder.mkdir()
            (folder / bad).write_text("x = 1\n")
            with self.assertRaises(ConfigError) as caught:
                load_helpers(folder, 10 ** 6)
            self.assertIn(bad, caught.exception.message)
            self.assertIn(".py", caught.exception.hint)

    def test_P4_a_missing_folder_and_helpers_over_the_limit(self):
        with self.assertRaises(ConfigError) as caught:
            load_helpers(self.home / "nowhere", 10 ** 6)
        self.assertIn("does not exist", caught.exception.message)
        folder = self.folder(**{"big.py": "x = '%s'\n" % ("y" * 5000)})
        with self.assertRaises(ConfigError) as caught:
            load_helpers(folder, 1000)
        self.assertIn("limit", caught.exception.message)

    def test_P4_a_helper_file_with_a_byte_order_mark_and_crlf(self):
        folder = self.home / "codemode_helpers"
        folder.mkdir()
        (folder / "win.py").write_bytes(b"\xef\xbb\xbfdef f():\r\n    return 1\r\n")
        (folder / "unix.py").write_bytes(b"def f():\n    return 1\n")
        loaded = load_helpers(folder, 10 ** 6)
        self.assertEqual(loaded["win"], "def f():\n    return 1\n")
        self.assertEqual(loaded["win"], loaded["unix"])     # the same helper, whatever the checkout's line ends

    def test_P3_one_set_of_helpers_is_stored_once(self):
        folder = self.folder(**{"ci.py": "def f():\n    return 1\n"})
        config = config_from_dict(dict(DEMO_CONFIG, helpers=str(folder)), "test")
        runtime = Runtime(config, self.home, agent="test")
        self.addCleanup(runtime.close)
        first, second = runtime.start("1"), runtime.start("2")
        stored = list((self.home / "helpers").glob("*.json"))
        self.assertEqual(len(stored), 1)
        self.assertEqual(runtime._helpers_of(runtime.store.view(first).start), {"ci": "def f():\n    return 1\n"})
        (folder / "ci.py").write_text("def f():\n    return 2\n", encoding="utf-8")
        third = runtime.start("3")
        self.assertEqual(len(list((self.home / "helpers").glob("*.json"))), 2)
        self.assertEqual(runtime._helpers_of(runtime.store.view(second).start)["ci"], "def f():\n    return 1\n")   # what it started with
        self.assertEqual(runtime._helpers_of(runtime.store.view(third).start)["ci"], "def f():\n    return 2\n")
        stored[0].unlink()
        with self.assertRaises(StateError):
            runtime._helpers_of(runtime.store.view(first).start)


class NeverStarted(Temp):
    def runtime(self) -> Runtime:
        return Runtime(config_from_dict(DEMO_CONFIG, "test"), self.home, agent="test")

    def test_R6_an_execution_whose_worker_is_gone_ends_as_interrupted_with_the_workers_output(self):
        runtime = self.runtime()
        self.addCleanup(runtime.close)
        exec_id = runtime.start("1 + 1")
        runtime.store.worker_log(exec_id).write_text("error: the config could not be read\n", encoding="utf-8")
        waiting = runtime.get(exec_id)
        self.assertEqual((waiting["status"], waiting["progress"]["phase"]), ("running", "starting"))  # inside the grace period
        payload = runtime.get(exec_id, worker_gone=True)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["error"]["type"], "Interrupted")
        self.assertIn("never started", payload["error"]["message"])
        self.assertIn("the config could not be read", payload["error"]["message"])
        # Settled once: asking again reads the same outcome, and a late
        # worker could not run it any more.
        self.assertEqual(runtime.get(exec_id)["error"]["type"], "Interrupted")

    def test_R6_the_grace_period_ends(self):
        runtime = self.runtime()
        self.addCleanup(runtime.close)
        exec_id = runtime.start("1 + 1")
        saved = runtime_module._START_GRACE_S
        runtime_module._START_GRACE_S = 0.0
        try:
            self.assertEqual(runtime.get(exec_id)["error"]["type"], "Interrupted")
        finally:
            runtime_module._START_GRACE_S = saved

    def test_R5_a_pass_whose_process_died_is_interrupted_and_its_lock_does_not_block(self):
        runtime = self.runtime()
        self.addCleanup(runtime.close)
        exec_id = runtime.start("tools.demo.list_notes()")
        gone = dead_pid()
        runtime.store.append(exec_id, {"type": "pass_start", "pass": 1, "pid": gone})
        (self.home / "executions" / (exec_id + ".lock")).write_text(str(gone))
        payload = runtime.get(exec_id)
        self.assertEqual(payload["error"]["type"], "Interrupted")
        self.assertIn("pid %d" % gone, payload["error"]["message"])
        self.assertFalse((self.home / "executions" / (exec_id + ".lock")).exists())


if __name__ == "__main__":
    unittest.main()
