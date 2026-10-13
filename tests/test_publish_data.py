from __future__ import annotations

import importlib.util
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from contextlib import chdir
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "publish_data.py"
SPEC = importlib.util.spec_from_file_location("publish_data", SCRIPT)
publisher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publisher)


class PublishDataTests(unittest.TestCase):
    def git(self, cwd: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True,
            text=True, encoding="utf-8",
        )
        if result.returncode:
            self.fail(f"git {args}: {result.stdout}\n{result.stderr}")
        return result.stdout.strip()

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="publish-data-test-"))
        self.addCleanup(self.cleanup)
        self.remote = self.root / "remote.git"
        self.worker = self.root / "worker"
        self.other = self.root / "other"
        self.recovery = self.root / "recovery"
        self.git(self.root, "init", "--bare", "--initial-branch=main", str(self.remote))
        self.git(self.root, "clone", str(self.remote), str(self.worker))
        self.configure(self.worker)
        (self.worker / "data").mkdir()
        (self.worker / "data" / "latest.json").write_text('"old"\n')
        (self.worker / "data" / "history.bin").write_bytes(b"\x00old\xff")
        self.commit(self.worker, "initial")
        self.git(self.worker, "push", "origin", "main")
        self.git(self.root, "clone", str(self.remote), str(self.other))
        self.configure(self.other)
        self.base = self.git(self.worker, "rev-parse", "HEAD")
        # Match actions/checkout's detached HEAD behavior.
        self.git(self.worker, "checkout", "--detach")

    def cleanup(self) -> None:
        target = self.root.resolve()
        if target.parent != Path(tempfile.gettempdir()).resolve() or not target.name.startswith("publish-data-test-"):
            raise RuntimeError(f"Refusing to clean unexpected test directory: {target}")
        def writable_remove(func, path, exc):
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            func(path)
        shutil.rmtree(target, onexc=writable_remove)

    def configure(self, repo: Path) -> None:
        self.git(repo, "config", "user.name", "Test")
        self.git(repo, "config", "user.email", "test@example.invalid")
        self.git(repo, "config", "core.autocrlf", "false")

    def commit(self, repo: Path, message: str) -> None:
        self.git(repo, "add", ".")
        self.git(repo, "commit", "-m", message)

    def advance_remote(self, name: str = "workflow.txt") -> str:
        (self.other / name).write_text("remote change\n")
        self.commit(self.other, name)
        self.git(self.other, "push", "origin", "main")
        return self.git(self.other, "rev-parse", "HEAD")

    def generate(self) -> None:
        (self.worker / "data" / "latest.json").write_text('"new"\n')
        (self.worker / "data" / "history.bin").write_bytes(b"\x00new\xfe")

    def test_no_changes_creates_no_commit_or_artifact(self) -> None:
        with chdir(self.worker):
            publisher.publish("main", self.recovery)
        self.assertEqual(self.git(self.remote, "rev-parse", "main"), self.base)
        self.assertFalse(self.recovery.exists())

    def test_remote_change_is_preserved_and_data_published(self) -> None:
        advanced = self.advance_remote()
        self.generate()
        with chdir(self.worker):
            publisher.publish("main", self.recovery)
        self.git(self.remote, "merge-base", "--is-ancestor", advanced, "main")
        self.assertEqual(self.git(self.remote, "show", "main:data/latest.json"), '"new"')
        self.assertEqual(self.git(self.remote, "show", "main:workflow.txt"), "remote change")
        self.assertEqual(self.git(self.remote, "rev-parse", "main"), self.git(self.worker, "rev-parse", "HEAD"))

    def test_race_between_fetch_and_push_is_retried(self) -> None:
        self.generate()
        real_git = publisher.git
        pushes = []

        def racing_git(*args, **kwargs):
            if args[0] == "push":
                pushes.append(args)
                if len(pushes) == 1:
                    self.advance_remote()
            return real_git(*args, **kwargs)

        with chdir(self.worker), patch.object(publisher, "git", side_effect=racing_git):
            publisher.publish("main", self.recovery)
        self.assertEqual(len(pushes), 2)
        self.assertEqual(self.git(self.remote, "show", "main:workflow.txt"), "remote change")
        self.assertEqual(self.git(self.remote, "show", "main:data/latest.json"), '"new"')

    def test_conflict_stops_and_patch_recovers_binary_data(self) -> None:
        advanced = self.advance_remote("data/latest.json")
        self.generate()
        with chdir(self.worker), self.assertRaisesRegex(RuntimeError, "Rebase failed"):
            publisher.publish("main", self.recovery)
        self.assertEqual(self.git(self.remote, "rev-parse", "main"), advanced)
        self.assertFalse((self.worker / ".git" / "rebase-merge").exists())
        self.git(self.other, "checkout", "--detach", self.base)
        self.git(self.other, "am", "--keep-cr", str(self.recovery / "data.patch"))
        self.assertEqual((self.other / "data" / "latest.json").read_text(), '"new"\n')
        self.assertEqual((self.other / "data" / "history.bin").read_bytes(), b"\x00new\xfe")

    def test_repeated_races_fail_after_three_attempts(self) -> None:
        self.generate()
        real_git = publisher.git
        advances = []

        def racing_git(*args, **kwargs):
            if args[0] == "push":
                advances.append(self.advance_remote(f"change-{len(advances)}.txt"))
            return real_git(*args, **kwargs)

        with chdir(self.worker), patch.object(publisher, "git", side_effect=racing_git):
            with self.assertRaisesRegex(RuntimeError, "failed after 3 attempts"):
                publisher.publish("main", self.recovery)
        self.assertEqual(len(advances), 3)
        self.assertEqual(self.git(self.remote, "rev-parse", "main"), advances[-1])
        self.assertTrue((self.recovery / "data.patch").is_file())


if __name__ == "__main__":
    unittest.main()
