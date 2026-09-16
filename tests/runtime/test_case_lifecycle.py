"""Synthetic tests for bounded process ownership and absolute deadlines."""

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from agentic_sim.runners.case_lifecycle import (
    CASE_DEADLINE_ENV,
    LifecycleError,
    deadline_environment,
    remaining_seconds,
    run_owned_process,
)


TREE_PROGRAM = r'''
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

root = Path(sys.argv[1])
token = sys.argv[2]
role = sys.argv[3]

def save(name, pid):
    (root / (token + "-" + name)).write_text(str(pid), encoding="ascii")

if role == "leader":
    nested = subprocess.Popen(
        [sys.executable, "-c", os.environ["CASE_LIFECYCLE_TREE_PROGRAM"], str(root), token, "nested"],
        start_new_session=True,
        close_fds=True,
    )
    save("leader", os.getpid())
    save("nested", nested.pid)
    # The leader exits immediately on TERM.  Its nested setsid child remains
    # alive, exercising cleanup after the direct parent has disappeared.
    signal.signal(signal.SIGTERM, lambda *_args: sys.exit(0))
    while True:
        time.sleep(0.05)
elif role == "nested":
    grandchild = subprocess.Popen(
        [sys.executable, "-c", os.environ["CASE_LIFECYCLE_TREE_PROGRAM"], str(root), token, "grandchild"],
        start_new_session=True,
        close_fds=True,
    )
    save("nested-live", os.getpid())
    save("grandchild", grandchild.pid)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:
        time.sleep(0.05)
else:
    save("grandchild-live", os.getpid())
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:
        time.sleep(0.05)
'''


DOUBLEFORK_PROGRAM = r'''
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

root = Path(sys.argv[1])
token = sys.argv[2]
role = sys.argv[3]

def save(name, pid):
    (root / (token + "-" + name)).write_text(str(pid), encoding="ascii")

if role == "leader":
    middle = subprocess.Popen(
        [sys.executable, "-c", os.environ["CASE_LIFECYCLE_DOUBLEFORK_PROGRAM"], str(root), token, "middle"],
        start_new_session=True,
        close_fds=True,
    )
    save("leader", os.getpid())
    save("middle", middle.pid)
    # Wait only for proof that the second fork exists, then exit normally
    # without waiting for the detached grandchild.
    deadline = time.monotonic() + 1
    while not (root / (token + "-grandchild")).exists() and time.monotonic() < deadline:
        time.sleep(0.01)
elif role == "middle":
    grandchild = subprocess.Popen(
        [sys.executable, "-c", os.environ["CASE_LIFECYCLE_DOUBLEFORK_PROGRAM"], str(root), token, "grandchild"],
        start_new_session=True,
        close_fds=True,
    )
    save("middle-live", os.getpid())
    save("grandchild", grandchild.pid)
    os._exit(0)
else:
    save("grandchild-live", os.getpid())
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:
        time.sleep(0.05)
'''


class CaseLifecycleTests(unittest.TestCase):
    def test_nested_setsid_tree_is_cleaned_when_term_leader_exits(self):
        """TERM of the root must not leave a detached stubborn descendant."""

        if not sys.platform.startswith("linux"):
            self.skipTest("process ownership scan is Linux-specific")
        with tempfile.TemporaryDirectory(prefix="case-lifecycle-") as temporary:
            root = Path(temporary)
            token = "fixture"
            process = None
            known_pids: list[int] = []
            try:
                process = run_owned_process(
                    [sys.executable, "-c", TREE_PROGRAM, str(root), token, "leader"],
                    env={**os.environ, "CASE_LIFECYCLE_TREE_PROGRAM": TREE_PROGRAM},
                    # A byte-exact execution snapshot deliberately excludes
                    # pyc caches. Its three fresh interpreters must finish
                    # startup before this test exercises deadline cleanup.
                    # Keep all three-process/no-survivor assertions strict.
                    timeout_seconds=1.0,
                    term_grace_seconds=0.25,
                    kill_grace_seconds=0.75,
                )
                for name in ("leader", "nested", "nested-live", "grandchild", "grandchild-live"):
                    marker = root / (token + "-" + name)
                    if marker.exists():
                        known_pids.append(int(marker.read_text(encoding="ascii")))
                self.assertTrue(known_pids, "fixture did not create a process tree")
                self.assertTrue(process.timed_out)
                self.assertTrue(process.cleanup["cleanup_complete"], process.cleanup)
                self.assertEqual(process.cleanup["survivors"], [])
                self.assertGreaterEqual(process.cleanup["known_processes"], 3)
                self.assertTrue(all(not self._running(pid) for pid in known_pids), known_pids)
            finally:
                # If the implementation under test fails, terminate only
                # fixture processes whose command line still contains the
                # unique temporary marker.  Never use a broad process-group
                # kill in test cleanup.
                for pid in known_pids:
                    self._kill_fixture_pid(pid, str(root))
                if process is not None:
                    process = None

    def test_immediate_doublefork_is_adopted_after_root_normal_exit(self):
        """A normally exiting root cannot orphan an untracked grandchild."""

        if not sys.platform.startswith("linux"):
            self.skipTest("dedicated subreaper is Linux-specific")
        with tempfile.TemporaryDirectory(prefix="case-doublefork-") as temporary:
            root = Path(temporary)
            token = "doublefork"
            outcome = run_owned_process(
                [sys.executable, "-c", DOUBLEFORK_PROGRAM, str(root), token, "leader"],
                env={**os.environ, "CASE_LIFECYCLE_DOUBLEFORK_PROGRAM": DOUBLEFORK_PROGRAM},
                timeout_seconds=1.5,
                term_grace_seconds=0.25,
                kill_grace_seconds=0.75,
            )
            pids = []
            for name in ("leader", "middle", "middle-live", "grandchild", "grandchild-live"):
                marker = root / (token + "-" + name)
                if marker.exists():
                    pids.append(int(marker.read_text(encoding="ascii")))
            self.assertTrue(pids, "double-fork fixture did not start")
            self.assertFalse(outcome.timed_out, outcome)
            self.assertTrue(outcome.cleanup["cleanup_complete"], outcome.cleanup)
            self.assertEqual(outcome.cleanup["survivors"], [])
            self.assertGreaterEqual(outcome.cleanup["known_processes"], 3)
            self.assertTrue(all(not self._running(pid) for pid in pids), pids)

    def test_unrelated_same_parent_decoy_survives_owned_cleanup(self):
        """Only the explicitly launched workload tree may be terminated."""

        if not sys.platform.startswith("linux"):
            self.skipTest("dedicated subreaper is Linux-specific")
        with tempfile.TemporaryDirectory(prefix="case-decoy-") as temporary:
            root = Path(temporary)
            marker = root / "decoy-ready"
            decoy = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text(str(__import__('os').getpid())); time.sleep(4)",
                    str(marker),
                ],
                start_new_session=True,
            )
            try:
                deadline = time.monotonic() + 1
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(marker.exists())
                outcome = run_owned_process(
                    [sys.executable, "-c", "import time; time.sleep(0.1)"],
                    timeout_seconds=1,
                )
                self.assertTrue(outcome.cleanup["cleanup_complete"], outcome.cleanup)
                self.assertIsNone(decoy.poll(), "unrelated same-parent process was terminated")
            finally:
                if decoy.poll() is None:
                    decoy.kill()
                decoy.wait(timeout=2)

    def test_expired_absolute_deadline_does_not_launch_workload(self):
        with tempfile.TemporaryDirectory(prefix="case-expired-") as temporary:
            marker = Path(temporary) / "must-not-launch"
            outcome = run_owned_process(
                [
                    sys.executable,
                    "-c",
                    "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('launched')",
                    str(marker),
                ],
                deadline_mono_ns=time.monotonic_ns() - 1,
            )
            self.assertTrue(outcome.timed_out)
            self.assertEqual(outcome.returncode, 124)
            self.assertFalse(marker.exists())
            self.assertFalse(outcome.cleanup["launched"])

    def test_deadline_environment_is_scoped_and_remaining_time_is_nonnegative(self):
        deadline = time.monotonic_ns() + 250_000_000
        original = os.environ.get(CASE_DEADLINE_ENV)
        with deadline_environment(deadline):
            self.assertEqual(os.environ[CASE_DEADLINE_ENV], str(deadline))
            remaining = remaining_seconds(deadline)
            self.assertGreaterEqual(remaining, 0.0)
            self.assertLessEqual(remaining, 0.25)
        self.assertEqual(os.environ.get(CASE_DEADLINE_ENV), original)

    def test_deadline_rejects_non_integer_or_expired_values(self):
        with self.assertRaises(LifecycleError):
            remaining_seconds(0)
        with self.assertRaises(LifecycleError):
            remaining_seconds("1.5")  # type: ignore[arg-type]

    @staticmethod
    def _running(pid: int) -> bool:
        try:
            state = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        except (FileNotFoundError, OSError):
            return False
        marker = state.rfind(")")
        if marker < 0:
            return False
        # A zombie has no live execution left; reaping may be asynchronous.
        return state[marker + 2 :].split()[0] != "Z"

    @classmethod
    def _kill_fixture_pid(cls, pid: int, marker: str) -> None:
        try:
            command_line = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except (FileNotFoundError, OSError):
            return
        if marker not in command_line:
            return
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            return


if __name__ == "__main__":
    unittest.main()
