"""Build manager for LLVM toolchain — clone, update, rollback."""

import logging
import subprocess

from .config import PROJECT_ROOT

log = logging.getLogger(__name__)

UPDATE_SCRIPT = PROJECT_ROOT / "scripts" / "update-tools.sh"


class BuildError(Exception):
    pass


def update_all():
    log.info("toolchain update start")
    proc = subprocess.run(
        ["bash", str(UPDATE_SCRIPT)],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=1800,
    )
    if proc.returncode == 2:
        log.warning("toolchain update rolled back to known-good:\n%s\n%s", proc.stdout, proc.stderr)
        return
    if proc.returncode != 0:
        log.error("toolchain update failed:\n%s\n%s", proc.stdout, proc.stderr)
        raise BuildError(proc.stderr)
    log.info("toolchain update ok")
