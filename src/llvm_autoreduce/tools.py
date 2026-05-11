"""Build manager for LLVM toolchain — clone, update, rollback."""

import logging
import subprocess
import time

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


def _wait_interruptible(proc, timeout):
    """Wait for subprocess with 1-second shutdown polling.

    Calls config.check_shutdown() each second; raises SystemExit if
    a signal was received. On timeout, kills the process and raises
    TimeoutExpired.
    """
    from . import config

    deadline = time.time() + timeout
    while True:
        config.check_shutdown()
        try:
            proc.wait(timeout=1)
            return
        except subprocess.TimeoutExpired as exc:
            if time.time() >= deadline:
                proc.kill()
                proc.wait()
                raise subprocess.TimeoutExpired(proc.args, timeout) from exc


def _run_git(args, *, cwd=None):
    cmd = ["git", *args]
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_interruptible(proc, 300)
    except subprocess.TimeoutExpired:
        raise
    except SystemExit:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        raise
    out, err = proc.communicate()  # drain any remaining output
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd, out, err)
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


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
    build_log = WORK_DIR / "build.log"
    with open(build_log, "w") as f:
        proc = subprocess.Popen(
            ["bash", str(UPDATE_SCRIPT), "--skip-git"],
            cwd=str(PROJECT_ROOT),
            stdout=f,
            stderr=subprocess.STDOUT,
        )
        try:
            _wait_interruptible(proc, 3600)
        except subprocess.TimeoutExpired:
            log.error("toolchain update timed out")
            raise
        except SystemExit:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            raise
    if proc.returncode == 2:
        with open(build_log) as f_log:
            log.warning("toolchain update rolled back to known-good:\n%s", f_log.read())
        return
    if proc.returncode != 0:
        with open(build_log) as f_log:
            err = f_log.read()
        log.error("toolchain update failed:\n%s", err)
        raise BuildError(err)
    log.info("toolchain update ok")
