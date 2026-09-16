"""Mocked Docker cleanup tests; no Docker daemon or real container is touched."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentic_sim.runners import owned_docker


OWNER = "a" * 32
CONTAINER_ID = "b" * 64
OTHER_OWNER = "c" * 32


def completed(command: list[str], *, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
    return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr=stderr)


def listed(owner: str = OWNER) -> list[str]:
    return [
        "docker",
        "ps",
        "-aq",
        "--no-trunc",
        "--filter",
        f"label={owned_docker.OWNER_LABEL}={owner}",
    ]


def inspected(owner: str, *, running: bool) -> bytes:
    return json.dumps(
        {
            "id": CONTAINER_ID,
            "name": "fixture-container",
            "image": "fixture-image@sha256:" + "d" * 64,
            "created": "2026-09-07T00:00:00Z",
            "owner": owner,
            "running": running,
            "status": "running" if running else "exited",
            "started": "2026-09-07T00:00:01Z",
            "finished": None if running else "2026-09-07T00:00:02Z",
            "exit_code": 0,
            "pid": 1234 if running else 0,
        }
    ).encode("utf-8")


class OwnedDockerCleanupTests(unittest.TestCase):
    def test_unowned_label_is_rejected_without_mutation(self):
        """A full ID from a label query is still re-verified before mutation."""

        calls: list[list[str]] = []

        def fake_run(command, **kwargs):
            calls.append(command)
            timeout = 8 if command[1] in {"stop", "kill"} else 15
            self.assertEqual(kwargs, {"capture_output": True, "timeout": timeout, "check": False})
            if command == listed():
                return completed(command, stdout=(CONTAINER_ID + "\n").encode("ascii"))
            if command[1:3] == ["inspect", "--format"]:
                return completed(command, stdout=inspected(OTHER_OWNER, running=True))
            self.fail(f"unexpected Docker operation after ownership mismatch: {command!r}")

        with tempfile.TemporaryDirectory(prefix="owned-docker-unowned-") as temporary:
            with patch.object(owned_docker.subprocess, "run", side_effect=fake_run):
                report = owned_docker.cleanup_owned_containers(OWNER, Path(temporary))

            self.assertFalse(report["cleanup_complete"])
            self.assertTrue(report["retained"])
            self.assertEqual(report["containers"], [])
            self.assertEqual(calls[0], listed())
            self.assertEqual(calls[1][:3], ["docker", "inspect", "--format"])
            self.assertEqual(calls[1][-1], CONTAINER_ID)
            self.assertFalse(any(command[1] in {"stop", "kill", "rm", "rmi"} for command in calls))
            self.assertTrue(any("identity or label mismatch" in error["reason"] for error in report["errors"]))

    def test_owned_stopped_container_is_logged_and_retained(self):
        """An exactly owned stopped container is evidenced but never removed."""

        calls: list[list[str]] = []

        def fake_run(command, **kwargs):
            calls.append(command)
            timeout = 8 if command[1] in {"stop", "kill"} else 15
            self.assertEqual(kwargs, {"capture_output": True, "timeout": timeout, "check": False})
            if command == listed():
                return completed(command, stdout=(CONTAINER_ID + "\n").encode("ascii"))
            if command[1:3] == ["inspect", "--format"]:
                return completed(command, stdout=inspected(OWNER, running=False))
            if command[1:3] == ["logs", "--timestamps"]:
                return completed(command, stdout=b"fixture stopped output\n")
            self.fail(f"unexpected Docker operation: {command!r}")

        with tempfile.TemporaryDirectory(prefix="owned-docker-stopped-") as temporary:
            artifact_dir = Path(temporary)
            with patch.object(owned_docker.subprocess, "run", side_effect=fake_run):
                report = owned_docker.cleanup_owned_containers(OWNER, artifact_dir)

            self.assertTrue(report["cleanup_complete"], report)
            self.assertTrue(report["retained"])
            self.assertEqual(report["errors"], [])
            self.assertEqual(report["containers"][0]["id"], CONTAINER_ID)
            self.assertEqual(
                [command[1] for command in calls],
                ["ps", "inspect", "logs", "inspect", "logs"],
            )
            self.assertFalse(any(command[1] in {"stop", "kill", "rm", "rmi"} for command in calls))
            evidence_dirs = [path for path in artifact_dir.iterdir() if path.is_dir()]
            self.assertEqual(len(evidence_dirs), 1)
            cleanup_json = json.loads((evidence_dirs[0] / "cleanup.json").read_text(encoding="utf-8"))
            self.assertTrue(cleanup_json["cleanup_complete"])
            self.assertEqual(cleanup_json["containers"][0]["after"]["running"], False)

    def test_log_failure_is_preserved_and_prevents_complete_cleanup(self):
        """Evidence command failures remain visible even after stop succeeds."""

        calls: list[list[str]] = []

        def fake_run(command, **kwargs):
            calls.append(command)
            timeout = 8 if command[1] in {"stop", "kill"} else 15
            self.assertEqual(kwargs, {"capture_output": True, "timeout": timeout, "check": False})
            if command == listed():
                return completed(command, stdout=(CONTAINER_ID + "\n").encode("ascii"))
            if command[1:3] == ["inspect", "--format"]:
                # The first and second inspect are distinguished by call count.
                running = sum(item[1] == "inspect" for item in calls) == 1
                return completed(command, stdout=inspected(OWNER, running=running))
            if command[1:3] == ["logs", "--timestamps"]:
                return completed(command, stdout=b"partial log bytes\n", stderr=b"log backend unavailable\n", returncode=19)
            if command[1:4] == ["stop", "--time", "2"]:
                return completed(command)
            self.fail(f"unexpected Docker operation: {command!r}")

        with tempfile.TemporaryDirectory(prefix="owned-docker-log-failure-") as temporary:
            artifact_dir = Path(temporary)
            with patch.object(owned_docker.subprocess, "run", side_effect=fake_run):
                report = owned_docker.cleanup_owned_containers(OWNER, artifact_dir)

            self.assertFalse(report["cleanup_complete"], report)
            self.assertEqual(report["containers"][0]["after"]["running"], False)
            self.assertEqual([error["operation"] for error in report["errors"]], [f"{CONTAINER_ID}.logs.before", f"{CONTAINER_ID}.logs.after"])
            self.assertIn("stop", [command[1] for command in calls])
            self.assertFalse(any(command[1] in {"kill", "rm", "rmi"} for command in calls))

            evidence_dirs = [path for path in artifact_dir.iterdir() if path.is_dir()]
            self.assertEqual(len(evidence_dirs), 1)
            evidence = evidence_dirs[0]
            for phase in ("before", "after"):
                self.assertEqual(
                    (evidence / f"{CONTAINER_ID}.logs.{phase}.stdout").read_bytes(),
                    b"partial log bytes\n",
                )
                self.assertEqual(
                    (evidence / f"{CONTAINER_ID}.logs.{phase}.stderr").read_bytes(),
                    b"log backend unavailable\n",
                )


if __name__ == "__main__":
    unittest.main()
