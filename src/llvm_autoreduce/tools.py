"""Build manager for LLVM toolchain — clone, update, rollback."""

import logging
import subprocess

from tenacity import retry, stop_after_attempt, wait_exponential_jitter

from .config import PROJECT_ROOT

log = logging.getLogger(__name__)

WORK_DIR = PROJECT_ROOT / "work"

UPDATE_SCRIPT = PROJECT_ROOT / "scripts" / "update-tools.sh"

CLONE_TARGETS = [
    ("https://github.com/llvm/llvm-project", WORK_DIR / "llvm-trunk"),
    ("https://github.com/AliveToolkit/alive2", WORK_DIR / "alive2-trunk"),
    ("https://github.com/dtcxzyw/llvm-ub-aware-interpreter", WORK_DIR / "llubi-trunk"),
]

FETCH_TARGETS = [
    (WORK_DIR / "llvm-trunk", "origin", "main"),
    (WORK_DIR / "alive2-trunk", "origin", "master"),
    (WORK_DIR / "llubi-trunk", "origin", "main"),
]

GIT_RETRY = {
    "stop": stop_after_attempt(5),
    "wait": wait_exponential_jitter(initial=1, max=10),
    "reraise": True,
}


class BuildError(Exception):
    pass


def _run_git(args, *, cwd=None):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
        check=True,
    )


@retry(**GIT_RETRY)
def _clone(url, dest):
    log.info("cloning %s → %s", url, dest)
    _run_git(["clone", url, str(dest)])


@retry(**GIT_RETRY)
def _fetch(path, remote, branch):
    log.info("fetching %s/%s in %s", remote, branch, path)
    _run_git(["fetch", remote, branch], cwd=path)


def _sync_git():
    for url, dest in CLONE_TARGETS:
        if not (dest / ".git").is_dir():
            _clone(url, dest)
    for path, remote, branch in FETCH_TARGETS:
        _fetch(path, remote, branch)


def update_all():
    log.info("toolchain update start")
    _sync_git()
    proc = subprocess.run(
        ["bash", str(UPDATE_SCRIPT), "--skip-git"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=1800,
    )
    if proc.returncode == 2:
        log.warning("toolchain update rolled back to known-good:\n%s\n%s", proc.stdout, proc.stderr)
        return
    if proc.returncode != 0:
        log.error("toolchain update failed:\n%s\n%s", proc.stdout, proc.stderr)
        raise BuildError(proc.stderr)
    log.info("toolchain update ok")
