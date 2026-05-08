"""opencode CLI subprocess wrapper."""

import contextlib
import logging
import os
import resource
import subprocess

from .config import ALIVE2_BIN, LLUBI_BIN, LLVM_BIN, PROJECT_ROOT

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
    # ACCEPTED RISK (F42): SHELL is passed through to subprocess agents
    # alongside USER/PATH/TERM. The security-reviewer agent has bash: deny
    # and does not need SHELL, but the shared env construction does not
    # differentiate between agent types. The incremental exposure surface
    # is negligible given that the other agents (extractor, reducer) have
    # full bash access anyway (R1, R9).
    for key in ("USER", "PATH", "TERM", "SHELL", "LANG", "LC_ALL"):
        if key in os.environ:
            env[key] = os.environ[key]
    for key, val in os.environ.items():
        if key.startswith("OPENCODE_"):
            env[key] = val
    paths = [str(LLVM_BIN), str(ALIVE2_BIN.parent), str(LLUBI_BIN.parent)]
    # ACCEPTED RISK (F30): When PATH is absent from os.environ (rare,
    # e.g. minimal containers), the constructed PATH has a trailing colon
    # which POSIX interprets as "search current working directory".
    # The cwd is PROJECT_ROOT (controlled by the daemon operator) and
    # agents already have full bash access (R1/R9), so the incremental
    # risk is negligible.
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
    # ACCEPTED RISK (R14): RLIMIT_AS is temporarily set on the parent process
    # between setrlimit() and fork(). See daemon._run_process for details.
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
