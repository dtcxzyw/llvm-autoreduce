#!/usr/bin/env python3
"""Main daemon loop for llvm-autoreduce."""

import atexit
import contextlib
import json
import logging
import os
import re
import resource
import shlex
import shutil
import signal
import subprocess
import sys
import time
from logging.handlers import TimedRotatingFileHandler

import requests
from tenacity import retry, stop_after_attempt, wait_exponential_jitter

from . import config, extract, github, opencode, tools, workdir

log = logging.getLogger("daemon")


def setup_logging():
    config.WORK_ROOT.mkdir(parents=True, exist_ok=True)
    handler = TimedRotatingFileHandler(
        config.DAEMON_LOG, when="midnight", interval=1, backupCount=10,
    )
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(handler)
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    root.addHandler(console)


_shutdown_requested = False


def _handle_shutdown(signum, _frame):
    global _shutdown_requested
    sig_name = signal.Signals(signum).name
    log.info("received %s, shutting down after current round", sig_name)
    _shutdown_requested = True


def _write_pidfile():
    pidfile = config.WORK_ROOT / "daemon.pid"
    pidfile.write_text(str(os.getpid()))


def _remove_pidfile():
    pidfile = config.WORK_ROOT / "daemon.pid"
    if pidfile.exists():
        pidfile.unlink()


def _check_binary(binary_path, name):
    """Check a single toolchain binary: exists, executable, and can run --version.

    Returns (ok: bool, detail: str). ok means the binary is fully functional.
    detail describes the state for logging: "ok", "missing", "not executable",
    "exit N", "timeout", or "failed to run".
    """
    if not binary_path.is_file():
        return False, "missing"
    if not os.access(binary_path, os.X_OK):
        return False, "not executable"
    try:
        result = subprocess.run(
            [str(binary_path), "--version"], capture_output=True, timeout=30,
        )
        if result.returncode == 0:
            return True, "ok"
        return False, f"exit {result.returncode}"
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except OSError:
        return False, "failed to run"


def _check_toolchain():
    """Build all toolchain components then verify binaries are functional.

    Always runs cmake --build before checking --version so that a
    silently broken binary (build succeeded but binary crashes) is
    caught by the rebuild.

    Called after a BuildError or build timeout in the main loop to detect
    whether the toolchain was left in a corrupt state that would cause all
    subsequent reduction rounds to fail silently.
    """
    log.info("toolchain health: building")
    proc = subprocess.run(
        ["bash", str(tools.UPDATE_SCRIPT), "--skip-git"],
        cwd=str(config.PROJECT_ROOT),
        capture_output=True, text=True, encoding="utf-8", timeout=1800,
    )
    if proc.returncode != 0:
        log.critical("toolchain health check FAILED: build exit %d\n%s\n%s",
                     proc.returncode, proc.stdout, proc.stderr)
        return False

    missing = []
    # LLVM core tools — checked against config.LLVM_BIN (built from source).
    for name in ("opt", "llc", "lli", "llvm-reduce", "clang"):
        ok, detail = _check_binary(config.LLVM_BIN / name, name)
        if not ok:
            missing.append(f"{name} ({detail})")
    # Miscompilation oracles — built alongside LLVM by update-tools.sh.
    # ACCEPTED RISK (F59): Oracle binary failures are treated identically to
    # core LLVM tool failures in the health check. If an oracle binary is
    # broken (e.g. after a rollback to a known-good hash that no longer
    # builds against the current LLVM trunk), the entire round is aborted,
    # including crash-only issues that do not require oracles. The toolchain
    # is trusted to produce functional binaries; a persistent oracle failure
    # indicates a systemic build-environment problem that requires operator
    # intervention regardless.
    for name, oracle_path in (
        ("alive-tv", config.ALIVE2_BIN),
        ("llubi_legacy", config.LLUBI_BIN),
    ):
        ok, detail = _check_binary(oracle_path, name)
        if not ok:
            missing.append(f"{name} ({detail})")
    if missing:
        log.critical("toolchain health check FAILED: %s", ", ".join(missing))
        return False
    log.info("toolchain health check passed")
    return True


def _cleanup_old_workdirs():
    """Remove per-issue work directories older than 10 days."""
    tasks_root = workdir.TASKS_DIR
    if not tasks_root.exists():
        return
    cutoff = time.time() - 10 * 86400
    removed = 0
    for path in tasks_root.iterdir():
        if not path.is_dir():
            continue
        try:
            if path.stat().st_mtime < cutoff:
                shutil.rmtree(path)
                removed += 1
        # ACCEPTED RISK (F16): Silently ignore OSError during cleanup (permission
        # denied, disk-full, EIO). Stale workdirs may accumulate if the error is
        # persistent, but surfacing every filesystem hiccup to the log would be
        # noisy and each individual failure is non-critical.
        except OSError:
            pass
    if removed:
        log.info("cleaned %d old workdirs", removed)


# ACCEPTED RISK (F5): No file locking — concurrent daemon instances may
# race on processed.txt, leading to duplicate processing or data loss.
# ACCEPTED RISK (F10): processed.txt grows without bound — every
# processed issue ID is stored permanently. For a long-running daemon
# this file accumulates entries linearly in time. Manual pruning is
# acceptable for now.
_processed_cache = None

# Maximum issue body size passed to AI agents (100 KB). Bodies exceeding this
# are rejected outright — truncation would silently drop inline reproducer
# code blocks located past the cutoff point.
_MAX_BODY_BYTES = 102400


def _load_processed_cache():
    global _processed_cache
    _processed_cache = set()
    if config.PROCESSED.exists():
        try:
            for line in config.PROCESSED.read_text().splitlines():
                stripped = line.strip()
                if stripped:
                    _processed_cache.add(stripped)
        except OSError:
            log.warning("failed to read %s, starting with empty cache", config.PROCESSED)


def mark_processed(issue_id):
    # One-ID-per-line format: each line is an issue number. Corrupt lines
    # are silently skipped. This avoids the all-or-nothing failure mode
    # of a JSON array — a single bad line never loses the rest of the file.
    with open(config.PROCESSED, "a") as f:
        f.write(f"{issue_id}\n")
        f.flush()
    if _processed_cache is not None:
        _processed_cache.add(str(issue_id))


def mark_dropped(issue_id, reason):
    """Record a dropped issue with timestamp and reason to config.DROPPED."""
    with open(config.DROPPED, "a") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')}\t{issue_id}\t{reason}\n")
        f.flush()


def is_processed(issue_id):
    if _processed_cache is None:
        _load_processed_cache()
    return str(issue_id) in _processed_cache


def _run_process(cmd, **kwargs):
    """Run subprocess with 8GB RLIMIT_AS propagated to child via pre-fork inheritance.

    Sets the address-space limit before forking so the child inherits it, then
    restores the parent limit immediately after fork(). This replaces preexec_fn
    (deprecated in Python 3.11+). The daemon is single-threaded so the brief
    parent-side limit change is harmless.

    ACCEPTED RISK (R14): RLIMIT_AS is temporarily set on the parent process
    between setrlimit() and fork(). If the daemon's RSS has grown above the 8 GB
    hard limit (e.g. after processing a large Godbolt JSON response), a
    subsequent allocation (logging, GC, signal handler) in this window may
    SIGSEGV the daemon. The window is a handful of Python bytecode instructions
    (setrlimit → Popen.__init__ → fork), and normal daemon memory usage stays
    well below 8 GB, so the risk is low. Using subprocess.Popen(preexec_fn=…)
    would avoid the parent-side side-effect but is deprecated in Python 3.11+.
    """
    timeout = kwargs.pop("timeout", None)
    if kwargs.get("text") and "encoding" not in kwargs:
        kwargs["encoding"] = "utf-8"
    old = resource.getrlimit(resource.RLIMIT_AS)
    limit = 8 * 1024 ** 3
    try:
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except (ValueError, OSError):
        old = None
    try:
        proc = subprocess.Popen(cmd, **kwargs)
    finally:
        if old is not None:
            with contextlib.suppress(ValueError, OSError):
                resource.setrlimit(resource.RLIMIT_AS, old)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


VALID_BUG_TYPES = frozenset({"crash", "miscompilation"})


def _validate_verdict(verdict):
    """Validate review.json schema. The security reviewer agent output is
    trusted for content, but required fields and types are checked."""
    if verdict.get("valid") is not True:
        raise ValueError(f"review.json valid is not True: {verdict.get('valid')!r}")
    # ACCEPTED RISK: malicious field is mandatory (FINAL DECISION).
    # If the security reviewer agent fails to include malicious, the
    # verdict is rejected outright — a missing field indicates the agent
    # did not complete its analysis and the safest default is to treat
    # the content as potentially malicious rather than silently passing.
    # The prompt explicitly requires malicious to be present; validation
    # enforces it so that a partial agent output cannot open a bypass.
    malicious = verdict.get("malicious")
    if not isinstance(malicious, bool):
        raise ValueError(f"review.json malicious missing or not bool: {malicious!r}")


def _validate_meta(meta):
    """Validate extract.json schema. Agent output is trusted for content
    but required fields, enumerations, and path-safety are checked."""
    bug_type = meta.get("type", "")
    if bug_type not in VALID_BUG_TYPES:
        raise ValueError(f"extract.json type not in {VALID_BUG_TYPES}: {bug_type!r}")
    oracle = meta.get("oracle", "")
    if oracle not in ("opt", "llc"):
        raise ValueError(f"extract.json oracle must be opt or llc: {oracle!r}")
    reproducer = meta.get("reproducer_file", "")
    if reproducer and ("/" in reproducer or "\\" in reproducer or "\0" in reproducer):
        raise ValueError(f"extract.json reproducer_file contains path separators: {reproducer!r}")
    _args = meta.get("args", "")
    # args string passes through to the reducer agent's prompt — the
    # extractor agent produces it and the reducer agent uses it. Both agents
    # are trusted oracles; the daemon does not validate args contents.
    # Path traversal on reproducer_file is validated because the daemon
    # writes those files itself.
    crash_pattern = meta.get("crash_pattern", "")
    # crash_pattern is a literal substring (not regex) matched against
    # crash output via plain string containment. The extractor agent
    # produces meaningful literal text fragments from actual crash output;
    # agent output is trusted.
    if bug_type == "crash" and not crash_pattern:
        raise ValueError("extract.json type=crash requires crash_pattern")
    if crash_pattern and len(crash_pattern) > 2000:
        raise ValueError(f"extract.json crash_pattern too long: {len(crash_pattern)} chars")


def _safe_relative(workdir_path, filename):
    """Resolve filename against workdir and reject path traversal."""
    # ACCEPTED RISK (F55): The workdir_prefix + os.sep suffix prevents
    # workdir_evil bypass but also rejects filenames that resolve to the
    # workdir itself (e.g. "."). In practice ir_file and reproducer_file
    # are always actual filenames validated by _validate_result / _validate_meta
    # (no empty strings, no path separators), so this edge case is never
    # triggered. If _safe_relative is reused in a new code path that passes
    # Path objects or ".", the check must be adapted.
    resolved = (workdir_path / filename).resolve()
    workdir_prefix = str(workdir_path.resolve()) + os.sep
    if not str(resolved).startswith(workdir_prefix):
        raise ValueError(f"Path traversal rejected: {filename!r}")
    return str(resolved)


def _validate_result(result):
    """Validate result.json schema. Agent output is trusted for content
    but required fields and enumerations are checked for structural validity.

    crash_pattern is intentionally NOT validated here — it originates
    exclusively from extract.json (the extractor agent). The reducer agent
    produces result.json without a crash_pattern field; the daemon pairs
    extract.json's crash_pattern with result.json's ir_file/tool/args at
    verification time.
    """
    if "ir_file" not in result:
        raise ValueError("result.json missing required field: ir_file")
    ir_file = result.get("ir_file", "")
    if not ir_file or "/" in ir_file or "\\" in ir_file:
        raise ValueError(f"result.json ir_file is empty or contains path separators: {ir_file!r}")
    result_type = result.get("type", "")
    if result_type == "crash":
        tool = result.get("tool", "opt")
        if tool not in ("opt", "llc"):
            raise ValueError(f"result.json crash type with invalid tool: {tool!r}")
    elif result_type == "miscompilation":
        oracle = result.get("oracle", "")
        if oracle not in ("llubi", "alive2", "lli"):
            raise ValueError(f"result.json miscompilation type with unknown oracle: {oracle!r}")
        reference = result.get("reference_file", "")
        if reference and ("/" in reference or "\\" in reference):
            raise ValueError(
                f"result.json reference_file contains path separators: {reference!r}"
            )
    else:
        raise ValueError(f"result.json has unknown type: {result_type!r}")


def verify_crash(result, workdir_path, crash_pattern):
    # Resolve the tool binary from the built LLVM toolchain (never PATH).
    # The reducer agent sets up PATH via opencode._env() to include
    # work/llvm-trunk/build/bin, but the daemon's own verify step must
    # explicitly use the same built binaries to avoid version mismatch.
    tool_name = result.get("tool", "opt")
    if tool_name not in ("opt", "llc"):
        log.error("verify crash: unknown tool %s", tool_name)
        return False
    tool_path = str(config.LLVM_BIN / tool_name)
    safe_ir = _safe_relative(workdir_path, result["ir_file"])
    # result.json args comes from the reducer agent, which is a trusted
    # oracle. The args string is split with shlex and passed to
    # subprocess.run as a list (no shell involved). Argument injection
    # into LLVM tools is not a concern — the agent already has
    # unrestricted bash access within the workdir.
    cmd = [tool_path] + shlex.split(result.get("args", "")) + [safe_ir]
    try:
        p = _run_process(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=str(workdir_path), timeout=config.VERIFY_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        log.error("verify crash timeout")
        return False
    except OSError:
        log.exception("verify crash os error")
        return False
    # ACCEPTED RISK (R15): p.stderr + p.stdout creates a combined string
    # whose size is bounded only by the subprocess's RLIMIT_AS (8 GB).
    # In practice LLVM tools on ≤ 8 KB inputs produce output well under 100 MB.
    # A malicious or pathological input that fills the pipe could OOM the
    # daemon, but such inputs are caught earlier by the 8 KB reproducer limit
    # and the security review.
    # ACCEPTED RISK (F43): p.returncode != 0 treats any non-zero exit as
    # a crash signal. LLVM tools may exit non-zero for non-crash reasons
    # (e.g. exec failure, resource exhaustion, I/O error). If such an
    # error message happens to contain the crash_pattern substring, the
    # verify step returns a false positive. In practice crash_pattern is
    # a specific fragment from an actual crash trace (e.g. "failed at
    # LICM.cpp"), making coincidental matches extremely unlikely.
    return p.returncode != 0 and crash_pattern in (p.stderr + p.stdout)


# ACCEPTED RISK (R6): verify_llubi compares exact stdout strings
# to detect miscompilation. Non-deterministic output (timestamps,
# metadata, randomization) may cause false positives.
#
# ACCEPTED RISK: This verify function is the safety net for the reducer
# agent's bisect and llvm-reduce interestingness scripts. The skill's
# shell scripts (llvm-miscompile-reduce/SKILL.md) use `!` + `pipefail` +
# `diff`, which causes oracle/tool crashes to be treated as miscompilation
# (non-zero pipeline exit inverted to 0 by `!`). The verify step here
# independently runs the oracle and checks returncode, catching
# crash-confused reductions produced by the skill's scripts.
def verify_llubi(result, workdir_path):
    safe_ir = _safe_relative(workdir_path, result["ir_file"])
    args = result.get("args", "")
    # llubi_args is produced by the reducer agent (trusted oracle).
    llubi_args = result.get("llubi_args", "--reduce-mode --max-steps 1000000")
    # Use the built LLVM toolchain opt binary, never PATH.
    opt_path = str(config.LLVM_BIN / "opt")
    try:
        ref = _run_process(
            [str(config.LLUBI_BIN)] + shlex.split(llubi_args) + [safe_ir],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=str(workdir_path), timeout=config.VERIFY_TIMEOUT,
        )
        if ref.returncode != 0:
            log.error("llubi ref failed: %s", ref.stderr[:200])
            return False

        # ACCEPTED RISK (R18): -S flag is placed after the input IR file
        # for both verify_llubi and verify_alive2. LLVM's cl::opt parser
        # handles flags position-independently, but this ordering is
        # non-idiomatic. If a future LLVM version changes to require options
        # before positional args, the flag would need to be moved.
        opt_out = _run_process(
            [opt_path] + shlex.split(args) + [safe_ir, "-S"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=str(workdir_path), timeout=config.VERIFY_TIMEOUT,
        )
        if opt_out.returncode != 0:
            log.error("llubi opt failed: %s", opt_out.stderr[:200])
            return False
        transformed = workdir_path / "__transformed.ll"
        # ACCEPTED RISK: __transformed.ll is written alongside the
        # per-issue workdir and may persist as a stale artifact if the
        # daemon is interrupted between verification and workdir cleanup.
        # This is intentional — on retry (exist_ok=True) the file is
        # overwritten with fresh content; a stale leftover has no effect
        # on correctness. Adding explicit cleanup after each verify call
        # adds churn without preventing any real problem.
        # ACCEPTED RISK (F58): opt_out.stdout is not checked for emptiness.
        # A non-zero returncode is already handled above, and opt -S producing
        # empty output on valid IR with a zero exit code is not observed in
        # practice. If this edge case ever occurs, the downstream oracle call
        # will produce an inconclusive result (not a false positive).
        transformed.write_text(opt_out.stdout)

        # ACCEPTED RISK: "__transformed.ll" is passed as a relative
        # path string (not via _safe_relative). The cwd is set to
        # workdir_path so relative resolution is correct. The filename
        # is hardcoded and matches the write path above, so path
        # traversal cannot occur.
        test = _run_process(
            [str(config.LLUBI_BIN)] + shlex.split(llubi_args) + ["__transformed.ll"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=str(workdir_path), timeout=config.VERIFY_TIMEOUT,
        )
        if test.returncode != 0:
            log.error("llubi test crashed: signal=%d stderr=%s", -test.returncode if test.returncode < 0 else test.returncode, test.stderr[:200])
            return False
        return ref.stdout != test.stdout
    except subprocess.TimeoutExpired:
        log.error("verify llubi timeout")
        return False
    except OSError:
        log.exception("verify llubi os error")
        return False


# ACCEPTED RISK (R8): verify_alive2 relies on Alive2's stable output
# format to detect miscompilation. If upstream Alive2 changes the phrasing
# of "0 incorrect transformations", "Transformation seems to be correct",
# or the error patterns, the logic below must be updated. These strings
# have been stable across multiple Alive2 releases and the coupling is
# limited to this single function.
_ALIVE2_INCORRECT_RE = re.compile(
    r"[1-9]\d* incorrect transformation|ERROR: Value mismatch"
)

_ALIVE2_APPROXIMATION_MARKER = "Alive2 approximated the semantics of the programs"


def verify_alive2(result, workdir_path):
    safe_ir = _safe_relative(workdir_path, result["ir_file"])
    args = result.get("args", "")
    # alive2_args is produced by the reducer agent (trusted oracle).
    alive2_args = result.get("alive2_args", "--smt-to=10000")
    # Use the built LLVM toolchain opt binary, never PATH.
    opt_path = str(config.LLVM_BIN / "opt")
    try:
        opt_out = _run_process(
            [opt_path] + shlex.split(args) + [safe_ir, "-S"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=str(workdir_path), timeout=config.VERIFY_TIMEOUT,
        )
        if opt_out.returncode != 0:
            log.error("alive2 opt failed: %s", opt_out.stderr[:200])
            return False
        transformed = workdir_path / "__transformed.ll"
        # ACCEPTED RISK (F58): opt_out.stdout is not checked for emptiness —
        # same rationale as verify_llubi above.
        transformed.write_text(opt_out.stdout)

        # ACCEPTED RISK: "__transformed.ll" is passed as a relative
        # path string (not via _safe_relative). The cwd is set to
        # workdir_path so relative resolution is correct. The filename
        # is hardcoded and matches the write path above, so path
        # traversal cannot occur.
        p = _run_process(
            [str(config.ALIVE2_BIN), "--disable-undef-input"]
            + shlex.split(alive2_args) + [safe_ir, "__transformed.ll"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=str(workdir_path), timeout=config.VERIFY_TIMEOUT,
        )
        # If alive-tv crashes (negative returncode), treat as inconclusive
        # — not a confirmed miscompilation.
        if p.returncode < 0:
            log.error("alive2 crashed: signal=%d stderr=%s", -p.returncode, p.stderr[:200])
            return False
        output = p.stderr + p.stdout
        # Check for disk-full before pattern matching — disk exhaustion
        # may produce truncated output that looks like a miscompilation.
        if "No space left on device" in output or "Disk quota exceeded" in output:
            return False
        # Both phrases must be present for Alive2 to declare correctness.
        correct = (
            "0 incorrect transformations" in output
            and "Transformation seems to be correct" in output
        )
        if correct:
            return False
        # Must match a specific incorrect-transformation or value-mismatch
        # pattern to be a confirmed miscompilation.
        if _ALIVE2_INCORRECT_RE.search(output):
            # Reject Alive2 approximations — they are not confirmed bugs.
            if _ALIVE2_APPROXIMATION_MARKER in output:
                log.info("alive2 approximation detected, not a confirmed miscompilation")
                return False
            return True
        # Inconclusive — Alive2 may have been killed by resource limits
        # or produced unexpected output. Treat as not confirmed.
        log.warning("alive2 inconclusive: no correctness message and no error pattern")
        return False
    except subprocess.TimeoutExpired:
        log.error("verify alive2 timeout")
        return False
    except OSError:
        log.exception("verify alive2 os error")
        return False


# verify_lli compares stdout from llubi_legacy (reference interpreter)
# and lli (JIT/backend-native execution) to detect backend miscompilation.
# The reducer agent preprocesses the IR to remove main() argument
# dependencies before using the lli oracle, so the two tools produce
# consistent output for correct backends.
def verify_lli(result, workdir_path):
    safe_ir = _safe_relative(workdir_path, result["ir_file"])
    args = result.get("args", "")
    # lli_args and llubi_args are produced by the reducer agent (trusted oracle).
    lli_args = result.get("lli_args", "")
    llubi_args = result.get("llubi_args", "--reduce-mode --max-steps 1000000")
    opt_path = str(config.LLVM_BIN / "opt")
    lli_path = str(config.LLVM_BIN / "lli")
    try:
        ref = _run_process(
            [str(config.LLUBI_BIN)] + shlex.split(llubi_args) + [safe_ir],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=str(workdir_path), timeout=config.VERIFY_TIMEOUT,
        )
        if ref.returncode != 0:
            log.error("lli verify: llubi ref failed: %s", ref.stderr[:200])
            return False

        opt_out = _run_process(
            [opt_path] + shlex.split(args) + [safe_ir, "-S"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=str(workdir_path), timeout=config.VERIFY_TIMEOUT,
        )
        if opt_out.returncode != 0:
            log.error("lli verify: opt failed: %s", opt_out.stderr[:200])
            return False
        transformed = workdir_path / "__transformed.ll"
        # ACCEPTED RISK (F58): opt_out.stdout is not checked for emptiness —
        # same rationale as verify_llubi above.
        transformed.write_text(opt_out.stdout)

        test = _run_process(
            [lli_path] + shlex.split(lli_args) + ["__transformed.ll"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=str(workdir_path), timeout=config.VERIFY_TIMEOUT,
        )
        if test.returncode != 0:
            log.error("lli verify: lli test failed: signal=%d stderr=%s",
                      -test.returncode if test.returncode < 0 else test.returncode,
                      test.stderr[:200])
            return False
        return ref.stdout != test.stdout
    except subprocess.TimeoutExpired:
        log.error("verify lli timeout")
        return False
    except OSError:
        log.exception("verify lli os error")
        return False


# The daemon trusts the oracle choice made by the reducer agent inside
# result.json. It does not independently select or fallback between
# llubi_legacy and alive-tv — the reducer agent has full context about
# which oracle succeeded during its opt-bisect-limit binary search.
# ACCEPTED RISK (F57): verify() and _validate_result() use separate
# if-else chains for oracle dispatch. If a new oracle is added to
# _validate_result() without a corresponding branch in verify(), the
# new oracle silently returns False (all issues dropped as verify_failed).
# The two sites must be kept in sync manually. There is no programmatic
# enforcement because both already enumerate the same closed set and new
# oracle types are expected to be extremely rare.
def verify(result, workdir_path, meta):
    if result.get("type") == "crash":
        # crash_pattern originates exclusively from extract.json.
        crash_pattern = meta.get("crash_pattern", "")
        if not crash_pattern:
            log.error("crash verification requires crash_pattern from extract.json")
            return False
        return verify_crash(result, workdir_path, crash_pattern)
    if result.get("oracle") == "llubi":
        return verify_llubi(result, workdir_path)
    if result.get("oracle") == "alive2":
        return verify_alive2(result, workdir_path)
    if result.get("oracle") == "lli":
        return verify_lli(result, workdir_path)
    log.error("verify: cannot verify result with type=%r oracle=%r",
              result.get("type"), result.get("oracle"))
    return False


def verify_extract_consistency(meta, result, workdir_path):
    """Cross-check extract.json metadata against reduce result.json.

    crash_pattern is validated against meta only — result.json no longer
    carries crash_pattern (it originates exclusively from extract.json).
    """
    bug_type = meta.get("type", "")
    result_type = result.get("type", "")
    if bug_type and result_type and bug_type != result_type:
        log.warning("type mismatch: extract=%s result=%s", bug_type, result_type)
        return False

    crash_pattern = meta.get("crash_pattern", "")
    if bug_type == "crash" and not crash_pattern:
        log.warning("extract type=crash but crash_pattern is empty")
        return False
    if bug_type == "miscompilation" and crash_pattern:
        log.warning("extract type=miscompilation but crash_pattern is non-empty, ignoring crash_pattern")

    ir_file = result.get("ir_file", "")
    if ir_file and not (workdir_path / ir_file).exists():
        log.warning("result ir_file %r not found in workdir", ir_file)
        return False

    reproducer_file = meta.get("reproducer_file", "")
    if reproducer_file and not (workdir_path / reproducer_file).exists():
        log.warning("extract reproducer_file %r not found in workdir", reproducer_file)
        return False

    reference_file = result.get("reference_file", "")
    if reference_file and not (workdir_path / reference_file).exists():
        log.warning("result reference_file %r not found in workdir", reference_file)
        return False

    return True


# Godbolt API request with tenacity retry — same retry policy as the GitHub
# API client to handle transient upstream failures and rate limits.
# ACCEPTED RISK (F15): Godbolt shortlink JSON responses are limited to 100 KB
# (envelope). Individual session sources > 8 KB are filtered downstream by
# reprocess_issue / F12. A legitimate Godbolt link with multiple large
# sessions may exceed this cap and be silently skipped.
_GODBOLT_MAX_JSON = 102400
@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential_jitter(initial=1, max=10),
    reraise=True,
)
def _fetch_godbolt_single(short_id):
    resp = requests.get(
        f"https://godbolt.org/api/shortlinkinfo/{short_id}",
        timeout=30,
        stream=True,
    )
    resp.raise_for_status()
    chunks = []
    total = 0
    for chunk in resp.iter_content(chunk_size=8192):
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > _GODBOLT_MAX_JSON:
            raise requests.HTTPError(
                f"Godbolt response too large: exceeds {_GODBOLT_MAX_JSON} bytes",
                response=resp,
            )
    return json.loads(b"".join(chunks))


def _fetch_godbolt(body):
    links = set(extract.find_godbolt_links(body))
    if not links:
        return []
    # ACCEPTED RISK: Godbolt shortlink count is capped at 3 per issue to
    # prevent abuse / resource exhaustion — same rationale as MAX_ATTACHMENTS.
    MAX_GODBOLT_LINKS = 3
    if len(links) > MAX_GODBOLT_LINKS:
        log.info("godbolt link limit (%d) reached, ignoring %d links",
                 MAX_GODBOLT_LINKS, len(links) - MAX_GODBOLT_LINKS)
        # ACCEPTED RISK: set→list→set slicing has non-deterministic ordering
        # (Python set iteration depends on hash seed). Most issues have ≤2 Godbolt
        # links, and each issue is processed exactly once, so ordering variation
        # across runs has zero practical impact.
        links = set(list(links)[:MAX_GODBOLT_LINKS])
    sources = []
    failed = 0
    # ACCEPTED RISK (F52): No upper bound on the number of sessions
    # per Godbolt shortlink. The total JSON response is capped at
    # 100 KB (F15), but this envelope can contain thousands of
    # minimal sessions (e.g. empty or single-line sources), each
    # written to a file in the per-issue workdir. The practical
    # risk is negligible — Godbolt shortlinks require a Compiler
    # Explorer account to create, and the workdir is cleaned up
    # after processing. A dedicated attacker could fill the workdir
    # with many small files, but the 3-link limit (F17) and the
    # operator's ability to monitor disk usage provide sufficient
    # mitigation.
    for short_id in links:
        try:
            data = _fetch_godbolt_single(short_id)
            for session in data.get("sessions", []):
                lang = session.get("language", "ir")
                src = session.get("source")
                if src is None:
                    log.debug("godbolt session missing source key, skipping")
                    continue
                if not isinstance(src, str):
                    log.info("godbolt session source is not a string (type=%s), skipping", type(src).__name__)
                    continue
                if not src.strip():
                    log.debug("godbolt session source is empty, skipping")
                    continue
                sources.append((src, lang))
        except (requests.RequestException, json.JSONDecodeError):
            # ACCEPTED RISK (F23): json.JSONDecodeError from Godbolt API is
            # caught here and the shortlink is silently skipped. Note that
            # the @retry decorator on _fetch_godbolt_single retries on ALL
            # exceptions (including JSONDecodeError) up to 5 times, wasting
            # ~50 seconds on doomed retries. This is accepted — malformed
            # Godbolt JSON responses are extremely rare, and the wasted time
            # is negligible in a 30-minute poll cycle.
            log.exception("godbolt fetch failed id=%s", short_id)
            failed += 1
    if failed:
        log.warning("godbolt fetch: %d/%d links failed", failed, len(links))
    return sources


def _download_attachments(body, wd):
    # ACCEPTED RISK (F17): Attachment count is capped at 3 per issue to
    # prevent abuse / resource exhaustion from issues with many attachments.
    # Most LLVM bug reports contain at most 1-2 reproducer attachments.
    # ACCEPTED RISK (F18): All attachment files are downloaded without
    # extension filtering and stored under numbered names (attachment1,
    # attachment2, ...). The extractor agent identifies the actual file
    # type by reading content, so filename/extension-based filtering is
    # unnecessary and would silently drop valid reproducers (e.g. .s
    # assembly attachments that were previously excluded).
    MAX_ATTACHMENTS = 3
    for idx, (url, filename) in enumerate(extract.find_attachment_urls(body), 1):
        if idx > MAX_ATTACHMENTS:
            log.info("attachment limit (%d) reached, ignoring remaining attachments", MAX_ATTACHMENTS)
            break
        safe_name = f"attachment{idx}"
        try:
            github.download_attachment(url, str(wd / safe_name))
            log.info("attachment downloaded: %s", safe_name)
        except (requests.RequestException, OSError):
            log.exception("attachment download failed: %s", filename)


def _generate_report(meta, result, workdir_path, issue_id):
    """Generate reduction report mechanically from verified data.

    Replaces the AI agent-generated report.md with deterministic output
    built from extract.json, result.json, and the reduced IR file.
    """
    bug_type = meta.get("type", "unknown")
    crash_pattern = meta.get("crash_pattern", "")

    ir_file = result["ir_file"]
    ir_path = _safe_relative(workdir_path, ir_file)
    try:
        ir_content = workdir.read(ir_path)
    except (ValueError, OSError) as e:
        raise ValueError(f"failed to read reduced IR {ir_file}: {e}") from e

    lines = []
    lines.append(f"# Reduced reproducer for llvm/llvm-project#{issue_id}")
    lines.append("")
    lines.append(f"**Bug type:** {bug_type}")
    # pipeline and crash_pattern are inserted into inline markdown code
    # spans; the reducer agent produces well-formed strings — agent
    # output is trusted.
    reduced_args = result.get("args", meta.get("args", ""))
    lines.append(f"**Pipeline:** `{reduced_args}`")

    oracle = result.get("oracle", "")
    if oracle:
        lines.append(f"**Oracle:** {oracle}")
        if oracle in ("llubi", "alive2"):
            lines.append("**Scope:** middle-end")
        elif oracle == "lli":
            lines.append("**Scope:** backend")
    if crash_pattern:
        lines.append(f"**Crash pattern:** `{crash_pattern}`")

    lines.append("")
    lines.append("## Toolchain")
    lines.append("")
    for name, repo in (("llvm", config.LLVM_TRUNK), ("alive2", config.ALIVE2_TRUNK), ("llubi", config.LLUBI_TRUNK)):
        sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if sha:
            lines.append(f"- **{name}:** `{sha[:12]}`")
        else:
            lines.append(f"- **{name}:** (unknown)")
    lines.append("")

    lines.append("")
    lines.append("## Reduced IR")
    lines.append("")
    # ACCEPTED RISK: Reduced IR content is inserted verbatim into a markdown
    # code fence. If the IR contains triple backticks the code block may break.
    # The IR is agent-generated from llvm-reduce output — the agent is trusted
    # to produce well-formed, benign content, and in practice reduced IR rarely
    # contains backtick sequences that would corrupt the markdown structure.
    lines.append("```llvm")
    lines.append(ir_content)
    lines.append("```")

    lines.append("")
    lines.append("## Steps to reproduce")
    lines.append("")

    tool = result.get("tool", "opt")
    args = result.get("args", "")

    # ACCEPTED RISK (F44): The bash command in the reproduction steps
    # is built by naive string concatenation (tool + args + ir_file)
    # rather than by reconstructing from shlex.split(args) as the verify
    # step uses. This means the reported command may not be structurally
    # identical to what was actually executed (e.g. quoted arguments
    # appear differently). The command is for human reference only and
    # the reported result has passed mechanical verification. No shell
    # safety issue arises because the report is submitted as a GitHub
    # issue body (GitHub-flavored markdown), not executed by the daemon.
    if bug_type == "crash":
        cmd = f"{tool} {args} {ir_file}"
        cmd = " ".join(cmd.split())
        lines.append("```bash")
        lines.append(cmd)
        lines.append("```")
    elif bug_type == "miscompilation":
        if oracle == "alive2":
            alive2_args = result.get("alive2_args", "--smt-to=10000")
            lines.append("```bash")
            lines.append(f"opt {args} {ir_file} -S > __reduced_opt.ll && alive-tv --disable-undef-input {alive2_args} {ir_file} __reduced_opt.ll")
            lines.append("```")
        elif oracle == "llubi":
            llubi_args = result.get("llubi_args", "--reduce-mode --max-steps 1000000")
            lines.append("```bash")
            lines.append("# Reference:")
            lines.append(f"llubi_legacy {llubi_args} {ir_file}")
            lines.append("# Transformed (incorrect):")
            lines.append(f"opt {args} {ir_file} -S > __reduced_opt.ll && llubi_legacy {llubi_args} __reduced_opt.ll")
            lines.append("```")
        elif oracle == "lli":
            lli_args = result.get("lli_args", "")
            llubi_args = result.get("llubi_args", "--reduce-mode --max-steps 1000000")
            lines.append("```bash")
            lines.append("# Reference:")
            lines.append(f"llubi_legacy {llubi_args} {ir_file}")
            lines.append("# Transformed (incorrect, via backend):")
            lines.append(f"opt {args} {ir_file} -S > __reduced_opt.ll && lli {lli_args} __reduced_opt.ll")
            lines.append("```")

    return "\n".join(lines)


def reprocess_issue(issue):
    issue_id = issue["number"]
    if is_processed(issue_id):
        return

    # Label-based exclusion: skip issues tagged with known non-bug labels.
    # Uses prefix matching — a label is excluded if it starts with any
    # entry in config.SKIP_LABEL_PREFIXES. This covers exact-match labels
    # (e.g. "invalid") and namespace labels (e.g. "clang-tidy").
    # Unlabeled issues are still processed.
    issue_labels = {lbl["name"].lower() for lbl in issue.get("labels", [])}
    matched = {lbl for lbl in issue_labels
               for pfx in config.SKIP_LABEL_PREFIXES if lbl.startswith(pfx)}
    if matched:
        log.info("issue=%d skipped: label match %s", issue_id, matched)
        mark_dropped(issue_id, "label_skip")
        mark_processed(issue_id)
        return

    log.info("issue=%d processing", issue_id)
    title, body = github.get_issue_info(issue_id)
    body = body or ""

    if not body.strip():
        log.info("issue=%d empty body, skip", issue_id)
        mark_dropped(issue_id, "empty_body")
        mark_processed(issue_id)
        return

    # ACCEPTED RISK (F11): Label-based pre-filtering is limited to
    # known non-bug labels (question, feature request, documentation,
    # duplicate, invalid, wontfix). Issues without any of these labels
    # — including unlabeled issues — proceed to security review and
    # extraction. The daemon does not use positive label signals
    # (miscompilation, crash) to prioritize or include issues. Label-
    # based filtering is a coarse exclusion mechanism only; GitHub
    # label updates are asynchronous and a newly mislabeled bug may
    # be incorrectly skipped until the label is corrected upstream.

    # ACCEPTED RISK (F27): When an issue is retried after a failed submission,
    # workdir.create reuses the existing directory (exist_ok=True). Stale files
    # from the previous run may persist. This is accepted because issue-body
    # edits between rounds are extremely rare — the typical retry case is a
    # transient GitHub API error with an unchanged body.
    wd = workdir.create(issue_id)

    # Remove stale agent output files from a prior failed run to prevent
    # them from being mistaken for fresh agent output if an agent crashes
    # before writing its own result. See F27 for exist_ok reuse rationale.
    for stale in ("review.json", "extract.json", "result.json"):
        stale_path = wd / stale
        if stale_path.exists():
            stale_path.unlink()

    # Step 1: validate body size before any network operations.
    # Large bodies waste Godbolt API calls and attachment downloads on
    # issues that will be immediately rejected. Check early to avoid
    # unnecessary network traffic.
    if len(body) > _MAX_BODY_BYTES:
        log.warning("issue=%d body too large (%d bytes > %d), skip",
                    issue_id, len(body), _MAX_BODY_BYTES)
        mark_dropped(issue_id, "body_too_large")
        mark_processed(issue_id)
        return

    # Step 2: download raw materials (Godbolt sources, attachments, issue body).
    # No mechanical extraction — the AI agents parse everything themselves.
    godbolt_sources = _fetch_godbolt(body)
    _download_attachments(body, wd)
    source_index = []
    for idx, (src, lang) in enumerate(godbolt_sources, 1):
        workdir.write(wd / f"godbolt_{idx}", src)
        source_index.append({"file": f"godbolt_{idx}", "language": lang})
    if source_index:
        workdir.write_json(wd / "sources.json", source_index)
    issue_text = f"# Issue #{issue_id}: {title}\n\n{body}"
    workdir.write(wd / "issue.md", issue_text)

    # Step 2: security review (bash denied) — audit + classify.
    # ACCEPTED RISK: The security reviewer rejects only code-level malware patterns
    # (system/exec/fork/popen etc.) in input reproducers. It does not defend against:
    #   (R2) Indirect prompt injection — adversarial markdown in issue bodies may
    #        instruct the agent to produce colluding review.json output.
    #   (R3) AI-generated shell scripts — reducer agent later generates and runs
    #        interestingness.sh (via llvm-reduce --test), which the security reviewer
    #        never inspects. The reviewer's coverage is limited to the initial
    #        reproducer content, not downstream AI artifact generation.
    #   (R12) Compile-time code execution — C/C++ reproducers can execute arbitrary
    #        code at compile time via __attribute__((constructor)), consteval
    #        functions, template metaprogramming, #pragma directives, or #include
    #        of sensitive files. The security reviewer is static-only (bash: deny)
    #        and cannot detect these patterns. The extractor agent compiles these
    #        sources with full bash access in the next step. Defense against
    #        compile-time attacks is left to OS-level isolation (Docker).
    #   (R17) Security reviewer bash:deny enforcement depends entirely on the
    #        opencode CLI tool honoring the agent config file. The Python daemon
    #        has no mechanism to pass or verify bash:deny, and does not inspect
    #        the agent's execution log. If opencode's config handling changes,
    #        the reviewer silently gains full bash access on untrusted content.
    review_prompt = "Review all reproducer files in this directory for malicious content and patterns. Write your verdict to review.json."
    ok = opencode.run(
        agent="security-reviewer",
        workdir=wd,
        prompt=review_prompt,
        timeout=config.REVIEW_TIMEOUT,
        shutdown_check=lambda: _shutdown_requested,
    )
    if not ok:
        log.warning("issue=%d review agent failed", issue_id)
        mark_dropped(issue_id, "review_agent_failed")
        mark_processed(issue_id)
        return

    review_path = wd / "review.json"
    if not review_path.exists():
        log.warning("issue=%d review.json missing", issue_id)
        mark_dropped(issue_id, "review_json_missing")
        mark_processed(issue_id)
        return

    try:
        verdict = workdir.read_json(review_path)
    except json.JSONDecodeError:
        log.warning("issue=%d review.json invalid", issue_id)
        mark_dropped(issue_id, "review_json_invalid")
        mark_processed(issue_id)
        return

    # ACCEPTED RISK (R16): Logging the full review.json verdict includes the
    # `reason` field, whose length is not validated by _validate_verdict. An
    # excessively long reason (e.g. from an anomalous agent) may produce a
    # multi-MB log line. The TimedRotatingFileHandler keeps 10 daily files,
    # capping total disk usage.
    log.info("issue=%d review=%s", issue_id, json.dumps(verdict))

    try:
        _validate_verdict(verdict)
    except ValueError:
        log.warning("issue=%d review.json validation failed", issue_id)
        mark_dropped(issue_id, "review_validation_failed")
        mark_processed(issue_id)
        return

    if verdict.get("malicious"):
        log.info("issue=%d skipped: malicious", issue_id)
        mark_dropped(issue_id, "malicious")
        mark_processed(issue_id)
        return

    # Step 3: extract reproducer metadata (bash allowed).
    # ACCEPTED RISK (R9): The extractor agent has bash access even though
    # its primary task is classification and metadata extraction. This is
    # intentional — the extractor is instructed to attempt reproducing the
    # bug first to validate that the reproducer is functional and to
    # capture an accurate crash pattern. Skipping this pre-validation
    # would produce lower-quality extract.json and waste reducer agent
    # time on non-reproducible inputs.
    extract_prompt = (
        "Your ONLY task: produce a validated reproducer and classify the bug. "
        "Read issue.md for context, then find the reproducer.\n\n"
        "Procedure (do NOT deviate):\n"
        "1. If the reproducer is C source, compile to IR AT THE REPORTED OPT LEVEL. "
        "NEVER use -O0 — O0 IR will NOT trigger middle-end bugs that depend on "
        "inlining, loop structures, or alias analysis from higher opt levels. "
        "Always compile with -O1/-O2/-O3 to match the issue, plus "
        "-Xclang -disable-llvm-passes so clang emits IRGen-level IR without "
        "running any LLVM optimization passes:\n"
        "   clang -x c -O2 -Xclang -disable-llvm-passes -S -emit-llvm source.c -o reproducer.ll\n"
        "If the reproducer is already .ll, use it directly.\n"
        "2. Determine the oracle: 'opt' for opt/llvm-reduce bugs (middle-end), "
        "'llc' for llc/backend bugs. clang is ONLY used for IR generation — "
        "NEVER compile C to a native binary.\n"
        "3. Reproduce the bug ONCE:\n"
        "   - Crash (oracle=opt): opt <args> reproducer.ll -o /dev/null 2>&1 | grep -qF '<pattern>'\n"
        "   - Crash (oracle=llc): llc <args> reproducer.ll -o /dev/null 2>&1 | grep -qF '<pattern>'\n"
        "   - Miscompilation (oracle=opt): llubi_legacy --reduce-mode reproducer.ll > ref; "
        "opt <args> reproducer.ll -S | llubi_legacy --reduce-mode -; outputs must differ\n"
        "   - Miscompilation (oracle=llc): llubi_legacy --reduce-mode reproducer.ll > ref; "
        "lli reproducer.ll > test; ref and test must differ (backend miscompilation)\n"
        "4. CRITICAL: The moment you have reproduced the bug, write extract.json and "
        "STOP. Do NOT run more commands. Do NOT read IR files. "
        "If NOT reproduced after ONE attempt, try ONE variation.\n\n"
        "extract.json schema:\n"
        '{"type": "crash|miscompilation", "reproducer_file": "<filename>", '
        '"args": "<opt/llc arguments>", "oracle": "opt|llc", '
        '"crash_pattern": "<literal substring from crash output, empty for miscompilation>"}'
    )
    ok = opencode.run(
        agent="extractor",
        workdir=wd,
        prompt=extract_prompt,
        timeout=config.EXTRACT_TIMEOUT,
        shutdown_check=lambda: _shutdown_requested,
    )
    if not ok:
        log.warning("issue=%d extractor agent failed", issue_id)
        mark_dropped(issue_id, "extractor_agent_failed")
        mark_processed(issue_id)
        return

    extract_path = wd / "extract.json"
    if extract_path.exists():
        try:
            meta = workdir.read_json(extract_path)
        except json.JSONDecodeError:
            log.warning("issue=%d extract.json invalid, skip", issue_id)
            mark_dropped(issue_id, "extract_json_invalid")
            mark_processed(issue_id)
            return
    else:
        log.warning("issue=%d extract.json missing, skip", issue_id)
        mark_dropped(issue_id, "extract_json_missing")
        mark_processed(issue_id)
        return
    log.info("issue=%d extract=%s", issue_id, json.dumps(meta))

    # Handle agent-classified "unrelated" — the extractor may correctly
    # identify non-LLVM-bug issues (e.g., build errors, documentation
    # requests miscategorized as bugs). These are explicitly tracked as a
    # distinct reason rather than lumped into extract_validation_failed.
    if meta.get("type") == "unrelated":
        log.warning("issue=%d extract reported type=unrelated, skip", issue_id)
        mark_dropped(issue_id, "extract_bug_unrelated")
        mark_processed(issue_id)
        return

    try:
        _validate_meta(meta)
    except ValueError:
        log.warning("issue=%d extract.json validation failed", issue_id)
        mark_dropped(issue_id, "extract_validation_failed")
        mark_processed(issue_id)
        return

    # Step 4: reduction.
    # ACCEPTED RISKS:
    #   (R1) No execution sandbox — the reducer agent has full bash access on the
    #        host user account. It generates and executes shell scripts
    #        (interestingness.sh) via llvm-reduce --test. No chroot, namespace,
    #        seccomp, or container isolation is applied. Confinement relies
    #        solely on natural-language instructions in the agent definition.
    #   (F1) No retry on transient failures — if opencode.run returns non-zero
    #        (timeout, API error, etc.), the issue is immediately marked as
    #        processed and never retried. Temporary infrastructure errors
    #        permanently skip valid issues.
    #   (F3) Single reproducer file — only meta.reproducer_file is passed to
    #        the reducer. Multi-file reproducer scenarios (e.g. inter-module
    #        bugs requiring multiple .ll files) are not supported and will
    #        silently fail reduction.
    reduce_prompt = "Read extract.json to determine the bug type. Load the appropriate skill (llvm-crash-reduce or llvm-miscompile-reduce) and reduce the reproducer. Write result.json."
    ok = opencode.run(
        agent="reducer",
        workdir=wd,
        prompt=reduce_prompt,
        timeout=config.REDUCE_TIMEOUT,
        shutdown_check=lambda: _shutdown_requested,
    )
    if not ok:
        # If the reducer timed out but wrote a result.json (checkpoint
        # from the skill's step 7), treat the reduction as successful.
        if (wd / "result.json").exists():
            log.info("issue=%d reducer timed out but result.json exists, continuing", issue_id)
        else:
            log.warning("issue=%d reduce agent failed", issue_id)
            mark_dropped(issue_id, "reducer_agent_failed")
            mark_processed(issue_id)
            return

    result_path = wd / "result.json"
    if not result_path.exists():
        log.warning("issue=%d result.json missing", issue_id)
        mark_dropped(issue_id, "result_json_missing")
        mark_processed(issue_id)
        return

    try:
        result = workdir.read_json(result_path)
    except json.JSONDecodeError:
        log.warning("issue=%d result.json invalid", issue_id)
        mark_dropped(issue_id, "result_json_invalid")
        mark_processed(issue_id)
        return

    try:
        _validate_result(result)
    except ValueError:
        log.warning("issue=%d result.json validation failed", issue_id)
        mark_dropped(issue_id, "result_validation_failed")
        mark_processed(issue_id)
        return

    # Step 5: verify before submitting
    if result.get("error"):
        log.warning("issue=%d reducer reported error: %s", issue_id, result["error"])
        mark_dropped(issue_id, "reducer_error")
        mark_processed(issue_id)
        return
    if not verify_extract_consistency(meta, result, wd):
        log.warning("issue=%d extract-result consistency check failed", issue_id)
        mark_dropped(issue_id, "consistency_check_failed")
        mark_processed(issue_id)
        return
    # ACCEPTED RISK (F13): No retry on verify failures — if the verification
    # subprocess times out or the toolchain signals a non-reproducible result,
    # the issue is immediately marked processed and never retried. Transient
    # infra issues (system load, temporary toolchain glitch) permanently lose
    # the issue. Accepted because verify failures are biased toward genuine
    # non-reproducibility rather than transient errors, and the risk of
    # spurious passes (accepting a bad reduction) outweighs the cost of
    # occasionally discarding a valid one.
    # The daemon verifies correctness (bug still reproduces) but never
    # compares reduced IR size against the original reproducer — the reducer
    # agent is trusted to follow instructions and run llvm-reduce. Agent
    # output is authoritative.
    if not verify(result, wd, meta):
        log.warning("issue=%d verify failed", issue_id)
        mark_dropped(issue_id, "verify_failed")
        mark_processed(issue_id)
        return
    log.info("issue=%d verify pass", issue_id)

    # Step 6: generate report and submit
    # Report is generated mechanically from verified data (meta, result, reduced IR)
    # rather than relying on AI-generated report.md.
    # ACCEPTED RISK (F54): _generate_report failures are terminal — the issue is
    # permanently marked processed and never retried. Report generation operates on
    # already-validated data (meta passed _validate_meta, result passed _validate_result,
    # verification passed) and the only expected failure modes are OSError reading the
    # IR file or an anomalous meta/result structure. Both indicate a permanently
    # unprocessable issue. This path is consistent with every other failure path
    # in reprocess_issue.
    try:
        report = _generate_report(meta, result, wd, issue_id)
    except Exception:
        log.exception("issue=%d report generation failed", issue_id)
        mark_dropped(issue_id, "report_generation_failed")
        mark_processed(issue_id)
        return
    # ACCEPTED RISK: meta['type'] uses direct key access (not .get)
    # because _validate_meta already guarantees the key is present with a
    # value in VALID_BUG_TYPES at this point in the pipeline. A future
    # refactor that calls _generate_report at a different stage must
    # preserve this invariant. The agent output is trusted to include
    # type in every valid extract.json.
    report_title = f"[Reduced] {meta['type']} — #{issue_id}"
    try:
        url = github.create_issue(report_title, report)
        log.info("issue=%d submitted %s", issue_id, url)
    except Exception:
        # ACCEPTED RISK (F50): GitHub submission failures are terminal —
        # the issue is marked processed and never retried. Previously this
        # path deferred retry to the next round (no mark_processed), which
        # caused an infinite retry loop when the report body exceeded
        # GitHub's ~64 KB issue body limit (e.g. reduced IR between 64 KB
        # and 200 KB). The daemon has no mechanism to shrink the report
        # between rounds, so retrying is fruitless. Per-issue submission
        # failures are rare and almost always indicate a permanent problem
        # (body too large, target repo deleted, token scope changed).
        log.exception("issue=%d submission failed", issue_id)
        mark_dropped(issue_id, "submission_failed")
        mark_processed(issue_id)
        return
    mark_processed(issue_id)


def main():
    setup_logging()
    # ACCEPTED RISK (F40): Startup validity check is limited to non-empty
    # string — no API call verifies the token actually authenticates. An
    # expired, revoked, or wrong-scope token will silently cause all rounds
    # to fail with "round failed" logs until an operator notices. A
    # lightweight GET /user probe would detect this at startup cost of
    # one extra API call per daemon restart.
    if not config.AUTOREDUCE_TOKEN:
        log.critical("AUTOREDUCE_TOKEN environment variable is required")
        sys.exit(1)

    # Verify required binaries exist before entering the poll loop.
    # LLVM tools are checked against the self-maintained trunk build at
    # config.LLVM_BIN (never PATH). Oracle binaries (alive-tv, llubi_legacy)
    # are built by tools.update_all() in the first loop iteration and
    # verified by _check_toolchain() on build errors / timeouts.
    missing = []
    opencode_path = shutil.which("opencode")
    if opencode_path is None:
        missing.append("opencode")
    else:
        log.info("found opencode at %s", opencode_path)
    for binary in ["opt", "clang", "llc", "lli", "llvm-reduce"]:
        ok, detail = _check_binary(config.LLVM_BIN / binary, binary)
        if ok:
            log.info("found %s at %s", binary, config.LLVM_BIN / binary)
        elif detail in ("missing", "not executable"):
            missing.append(binary)
        else:
            log.warning("%s at %s: %s on --version", binary, config.LLVM_BIN / binary, detail)
    for oracle_name, oracle_path in (
        ("alive-tv", config.ALIVE2_BIN),
        ("llubi_legacy", config.LLUBI_BIN),
    ):
        ok, detail = _check_binary(oracle_path, oracle_name)
        if ok:
            log.info("found %s at %s", oracle_name, oracle_path)
        else:
            log.warning("%s at %s: %s — miscompilation verification will be unavailable",
                        oracle_name, oracle_path, detail)
    if missing:
        log.critical("required binaries not found: %s", ", ".join(missing))
        sys.exit(1)

    _write_pidfile()
    atexit.register(_remove_pidfile)
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGQUIT, _handle_shutdown)
    # ACCEPTED RISK (F26): SIGHUP triggers shutdown rather than config reload.
    # Standard daemon practice reserves SIGHUP for config reload, but this
    # daemon treats SIGHUP identically to SIGTERM/SIGINT as a graceful
    # shutdown request. Log rotation tools that send SIGHUP will cause the
    # daemon to exit instead of reloading configuration.
    signal.signal(signal.SIGHUP, _handle_shutdown)
    # Shutdown is checked at round boundaries, after every issue (1-issue
    # granularity), and during every opencode agent invocation (1-second
    # polling via shutdown_check in opencode.run). A SIGTERM/SIGINT received
    # mid-issue terminates the current agent subprocess within 1 second,
    # and the for-loop breaks immediately after the current issue completes.

    log.info("llvm-autoreduce daemon starting")

    while not _shutdown_requested:
        try:
            log.info("round start")
            tools.update_all()
            # ACCEPTED RISK (F47): Toolchain health check runs once per round
            # before issue processing. If the build succeeded (exit 0) but
            # produced a broken toolchain (stale shared libs, corrupt binaries),
            # this check catches it before all 20 issues are silently lost.
            # A failed health check aborts the round — the daemon stays alive
            # per F34/F45 so the operator can intervene. This is the FINAL
            # decision: the health check is the last line of defense against
            # systemic data loss from a silently-broken toolchain.
            # ACCEPTED RISK (F56): No agent health check — unlike the toolchain
            # (checked per-round via _check_toolchain), the opencode binary and
            # AI provider are only validated at daemon startup. A persistent
            # opencode or AI provider outage causes every issue in every round
            # to permanently fail as "agent_failed" → mark_processed, silently
            # burning through all open issues over multiple rounds. The daemon
            # has no consecutive-failure counter or circuit breaker for agent
            # errors. Operator log monitoring is the sole mitigation.
            if not _check_toolchain():
                log.critical("toolchain health check failed after update, aborting round")
                if _shutdown_requested:
                    break
                deadline = time.time() + config.DAEMON_INTERVAL
                while time.time() < deadline and not _shutdown_requested:
                    time.sleep(1)
                continue
            _cleanup_old_workdirs()
            issues = github.fetch_issues()
            log.info("round fetched %d issues", len(issues))
            for issue in issues:
                if _shutdown_requested:
                    log.info("shutdown requested mid-round, stopping")
                    break
                try:
                    reprocess_issue(issue)
                except Exception:
                    # ACCEPTED RISK (F35): Outer per-issue exceptions are terminal —
                    # mark_processed and never retry. This is the FINAL design decision.
                    # Every exception path that escapes reprocess_issue (e.g. bug in the
                    # daemon itself triggered by a specific issue's data) permanently
                    # skips that issue. The alternative of retrying indefinitely (as was
                    # done before F35) wastes resources on fundamentally unprocessable
                    # issues with no hope of eventual success. Breaking buggy issues are
                    # extremely rare and the cost of occasionally losing one is negligible
                    # compared to infinite retry loops.
                    issue_id = issue.get("number", "?")
                    log.exception("issue=%s unhandled error, permanently skipping", issue_id)
                    mark_dropped(issue_id, "unhandled_exception")
                    mark_processed(issue_id)
            log.info("round done")
        # ACCEPTED RISK (F34): Non-rollback BuildError (exit code != 0, 2) means
        # the known-good toolchain also fails to build. The daemon continues running
        # but all issues in this and subsequent rounds will fail silently because
        # the toolchain is unusable. A full daemon exit would be more appropriate
        # but would require external supervision (systemd restart) to recover if
        # the build failure is transient (e.g. OOM). The current behavior of
        # logging and continuing is accepted — the operator must monitor logs.
        # ACCEPTED RISK (F45): _check_toolchain return value is not used
        # here — the daemon continues the poll loop regardless of whether
        # the health check passes or fails. A permanently corrupted
        # toolchain (e.g. stale broken shared library, accidentally
        # deleted binary) will be detected and logged critically by
        # _check_toolchain, but the daemon stays alive so the operator
        # can notice the log and intervene. Exiting the process would
        # require external supervision (systemd) to restart, which is a
        # deployment concern outside this daemon's scope.
        except tools.BuildError:
            log.exception("round failed: toolchain build error")
            _check_toolchain()
        except subprocess.TimeoutExpired:
            log.exception("round failed: toolchain build timeout")
            _check_toolchain()
        except Exception:
            log.exception("round failed")
        if _shutdown_requested:
            break
        # Sleep in 1-second increments so that SIGTERM/SIGINT are checked
        # promptly, rather than blocking for the full DAEMON_INTERVAL.
        deadline = time.time() + config.DAEMON_INTERVAL
        while time.time() < deadline and not _shutdown_requested:
            time.sleep(1)

    log.info("daemon shutting down")


if __name__ == "__main__":
    main()
