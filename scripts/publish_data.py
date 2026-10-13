"""Publish generated data with bounded retries and a recoverable failure patch."""
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args], text=True, encoding="utf-8", capture_output=True
    )
    if check and result.returncode:
        raise RuntimeError(f"git {args[0]} failed: {result.stderr.strip()}")
    return result


def publish(branch: str, recovery_dir: Path, attempts: int = 3) -> None:
    if attempts < 1:
        raise ValueError("attempts must be positive")
    git("check-ref-format", f"refs/heads/{branch}")
    if git("diff", "--cached", "--name-only").stdout.strip():
        raise RuntimeError("Refusing to publish with pre-existing staged changes")
    base = git("rev-parse", "HEAD").stdout.strip()
    git("add", "--", "data/")
    staged = git("diff", "--cached", "--quiet", check=False)
    if staged.returncode == 0:
        print("No data changes to commit.")
        return
    if staged.returncode != 1:
        raise RuntimeError(staged.stderr)
    git("commit", "-m", "chore(data): update daily commodity snapshots")

    # Save before any rebase; the patch includes binary changes and deletions.
    recovery_dir.mkdir(parents=True, exist_ok=True)
    (recovery_dir / "recovery.txt").write_text(
        f"Base commit: {base}\nData commit: {git('rev-parse', 'HEAD').stdout.strip()}\n"
        "Apply data.patch with git am --keep-cr on the base commit in a separate checkout.\n",
        encoding="utf-8",
    )
    with (recovery_dir / "data.patch").open("wb") as output:
        subprocess.run(
            ["git", "format-patch", "--binary", "--full-index", "--stdout", "-1", "HEAD"],
            stdout=output, check=True,
        )

    for attempt in range(1, attempts + 1):
        print(f"Synchronizing and pushing data (attempt {attempt}/{attempts}).", flush=True)
        git("fetch", "origin", f"refs/heads/{branch}")
        remote = git("rev-parse", "FETCH_HEAD").stdout.strip()
        if git("merge-base", "--is-ancestor", base, remote, check=False).returncode:
            raise RuntimeError("Remote history diverged; refusing to rewrite it")
        rebased = git("rebase", "--onto", remote, base, check=False)
        if rebased.returncode:
            git("rebase", "--abort", check=False)
            raise RuntimeError(
                f"Rebase failed; remote was not overwritten. Recovery: {recovery_dir}\n"
                f"{rebased.stdout}\n{rebased.stderr}"
            )
        base = remote
        pushed = git("push", "origin", f"HEAD:refs/heads/{branch}", check=False)
        if pushed.returncode == 0:
            print(pushed.stderr.strip())
            print("Data published successfully.")
            return
        print(pushed.stderr.strip(), flush=True)
    raise RuntimeError(f"Push failed after {attempts} attempts. Recovery: {recovery_dir}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--recovery-dir", required=True, type=Path)
    args = parser.parse_args()
    publish(args.branch, args.recovery_dir)
