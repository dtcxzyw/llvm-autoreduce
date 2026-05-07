"""opencode CLI subprocess wrapper."""

import contextlib
import logging
import os
import resource
import subprocess

from .config import PROJECT_ROOT

log = logging.getLogger(__name__)


def _env():
    # Start with a minimal environment — only preserve essential variables
    # plus any opencode-specific configuration. This prevents sensitive
    # host variables (AWS keys, DB passwords, etc.) from leaking to
    # subprocess agents that have bash access.
    # NOTE: LC_CTYPE, LC_MESSAGES and other per-category locale vars are
    # intentionally omitted. The broad LANG/LC_ALL pair covers all common
    # locale needs. Adding more would expand the attack surface with
    # negligible benefit.
    # NOTE: HOME is intentionally excluded. Agents with bash access can
    # still discover it via ~ or getent, but we should not hand it to
    # them as an env-var convenience.
    env = {}
    for key in ("USER", "PATH", "TERM", "SHELL", "LANG", "LC_ALL"):
        if key in os.environ:
            env[key] = os.environ[key]
    for key, val in os.environ.items():
        if key.startswith("OPENCODE_"):
            env[key] = val
    paths = [str(PROJECT_ROOT / "work" / "llvm-trunk" / "build" / "bin"),
             str(PROJECT_ROOT / "work" / "alive2-trunk" / "build" / "bin"),
             str(PROJECT_ROOT / "work" / "llubi-trunk" / "build" / "bin")]
    env["PATH"] = ":".join(paths + [os.environ.get("PATH", "")])
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

    # Pre-set RLIMIT_AS=8GB before fork so child inherits it; restore parent
    # limit immediately after. Replaces preexec_fn (deprecated in 3.11+).
    old = resource.getrlimit(resource.RLIMIT_AS)
    limit = 8 * 1024 ** 3
    try:
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except (ValueError, OSError):
        old = None

    log.info("opencode start agent=%s workdir=%s", agent, workdir)
    try:
        with open(log_path, "w") as f:
            proc = subprocess.Popen(
                cmd,
                cwd=str(PROJECT_ROOT),
                env=_env(),
                stdout=f,
                stderr=subprocess.STDOUT,
            )
    finally:
        if old is not None:
            with contextlib.suppress(ValueError, OSError):
                resource.setrlimit(resource.RLIMIT_AS, old)

    try:
        proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
    log.info("opencode done agent=%s exit=%d", agent, proc.returncode)
    return proc.returncode == 0
