"""opencode CLI subprocess wrapper."""

import logging
import os
import subprocess

from .config import PROJECT_ROOT

log = logging.getLogger(__name__)


def _env():
    env = os.environ.copy()
    # Never leak the GitHub token to subprocess agents that have bash access.
    env.pop("AUTOREDUCE_TOKEN", None)
    paths = [str(PROJECT_ROOT / "work" / "llvm-trunk" / "build" / "bin"),
             str(PROJECT_ROOT / "work" / "alive2-trunk" / "build" / "bin"),
             str(PROJECT_ROOT / "work" / "llubi-trunk" / "build" / "bin")]
    existing = env.get("PATH", "")
    env["PATH"] = ":".join(paths + [existing])
    return env


def run(agent, workdir, prompt, timeout):
    log_path = workdir / "log.txt"
    # ACCEPTED RISK (F6): Model name is hardcoded — not configurable via
    # environment variable or config file.
    cmd = [
        "opencode",
        "--model", "deepseek/deepseek-v4-pro",
        "--agent", agent,
        "--workdir", str(workdir),
        "-p", prompt,
    ]

    log.info("opencode start agent=%s workdir=%s", agent, workdir)
    with open(log_path, "w") as f:
        proc = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            env=_env(),
            stdout=f,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    log.info("opencode done agent=%s exit=%d", agent, proc.returncode)
    return proc.returncode == 0
