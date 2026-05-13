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
import threading
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



def _handle_shutdown(signum, _frame):
    sig_name = signal.Signals(signum).name
    log.info("received %s, shutting down", sig_name)
    config.request_shutdown()
    # Restore default handler so a second signal kills us immediately.
    signal.signal(signum, signal.SIG_DFL)


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
    try:
        proc = _run_process(
            ["bash", str(tools.UPDATE_SCRIPT), "--skip-git"],
            cwd=str(config.PROJECT_ROOT),
            text=True, encoding="utf-8", timeout=1800,
        )
    except subprocess.TimeoutExpired:
        log.critical("toolchain health check FAILED: build timed out")
        return False
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
    """Run subprocess with 8GB RLIMIT_AS and shutdown-aware polling.

    Sets the address-space limit before forking so the child inherits it, then
    restores the parent limit immediately after fork(). This replaces preexec_fn
    (deprecated in Python 3.11+). The daemon is single-threaded so the brief
    parent-side limit change is harmless.

    Output is captured via daemon threads to avoid pipe-buffer deadlock while
    the main thread polls for shutdown every second. config.check_shutdown()
    raises SystemExit if a signal was received.

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
    text = kwargs.pop("text", False)
    encoding = kwargs.pop("encoding", "utf-8")
    kwargs.pop("stdout", None)
    kwargs.pop("stderr", None)
    stdout = kwargs.pop("stdout", subprocess.PIPE)
    stderr = kwargs.pop("stderr", subprocess.PIPE)

    old = resource.getrlimit(resource.RLIMIT_AS)
    limit = 8 * 1024 ** 3
    try:
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except (ValueError, OSError):
        old = None
    try:
        proc = subprocess.Popen(
            cmd, stdout=stdout, stderr=stderr, **kwargs,
        )
    finally:
        if old is not None:
            with contextlib.suppress(ValueError, OSError):
                resource.setrlimit(resource.RLIMIT_AS, old)

    out_chunks = []
    err_chunks = []

    def _read(pipe, buf):
        try:
            for chunk in iter(lambda: pipe.read(65536), b""):
                buf.append(chunk)
        finally:
            pipe.close()

    tout = threading.Thread(target=_read, args=(proc.stdout, out_chunks), daemon=True)
    terr = threading.Thread(target=_read, args=(proc.stderr, err_chunks), daemon=True)
    tout.start()
    terr.start()

    deadline = time.time() + timeout if timeout else float("inf")
    try:
        while tout.is_alive() or terr.is_alive():
            config.check_shutdown()
            remaining = deadline - time.time()
            if remaining <= 0:
                proc.kill()
                tout.join(timeout=1)
                terr.join(timeout=1)
                raise subprocess.TimeoutExpired(cmd, timeout)
            tout.join(timeout=min(remaining, 1))
            terr.join(timeout=min(remaining, 1))
    except SystemExit:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        tout.join(timeout=1)
        terr.join(timeout=1)
        raise

    proc.wait()
    out = b"".join(out_chunks)
    err = b"".join(err_chunks)
    if text:
        out = out.decode(encoding)
        err = err.decode(encoding)
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
    # Compatibility: lli is a JIT tool; backend bugs use llc as the
    # trigger with lli as the comparison oracle. Map lli → llc so
    # older extractor output is accepted.
    if oracle == "lli":
        oracle = "llc"
        meta["oracle"] = "llc"
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
    pattern = meta.get("pattern", "")
    # pattern is a literal substring (not regex) matched against crash
    # output via plain string containment, or one of wrong_output /
    # nonzero_exit / infinite_loop for miscompilation.
    if bug_type == "crash":
        if not pattern:
            raise ValueError("extract.json type=crash requires pattern")
        if len(pattern) > 2000:
            raise ValueError(f"extract.json pattern too long: {len(pattern)} chars")
    elif bug_type == "miscompilation":
        if pattern not in ("wrong_output", "nonzero_exit", "infinite_loop"):
            raise ValueError(
                f"extract.json miscompilation pattern must be "
                f"wrong_output/nonzero_exit/infinite_loop: {pattern!r}"
            )


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

    # pattern is intentionally NOT validated here — it originates
    # exclusively from extract.json (as the "pattern" field). The reducer agent
    # produces result.json without a pattern field; the daemon pairs
    # extract.json's pattern with result.json's ir_file/tool/args at
    # verification time.
    """
    if "ir_file" not in result:
        raise ValueError("result.json missing required field: ir_file")
    ir_file = result.get("ir_file", "")
    if not ir_file or "/" in ir_file or "\\" in ir_file:
        raise ValueError(f"result.json ir_file is empty or contains path separators: {ir_file!r}")
    result_type = result.get("type", "")
    if result_type == "crash":
        oracle = result.get("oracle", "opt")  # default to opt for backward compat
        if oracle not in ("opt", "llc"):
            raise ValueError(f"result.json crash type with invalid oracle: {oracle!r}")
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

    # Reject bisect artifacts and default pipelines in args.
    # After bisect, args must be reduced to a single pass or a few specific
    # passes — never -opt-bisect-limit or a default<O?> pipeline.
    _args = result.get("args", "")
    if "-opt-bisect-limit" in _args:
        raise ValueError(f"result.json args must not contain -opt-bisect-limit: {_args!r}")
    if "default<" in _args:
        raise ValueError(f"result.json args must not contain default pipeline: {_args!r}")


def verify_crash(result, workdir_path, pattern):
    # Resolve the tool binary from the built LLVM toolchain (never PATH).
    # The reducer agent sets up PATH via opencode._env() to include
    # work/llvm-trunk/build/bin, but the daemon's own verify step must
    # explicitly use the same built binaries to avoid version mismatch.
    tool_name = result.get("oracle", "opt")
    if tool_name not in ("opt", "llc"):
        log.error("verify crash: unknown tool %s", tool_name)
        return False
    tool_path = str(config.LLVM_BIN / tool_name)
    safe_ir = _safe_relative(workdir_path, result["ir_file"])
    if not _verify_ir_valid(result["ir_file"], workdir_path):
        return False
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
    # error message happens to contain the pattern substring, the
    # verify step returns a false positive. In practice pattern is
    # a specific fragment from an actual crash trace (e.g. "failed at
    # LICM.cpp"), making coincidental matches extremely unlikely.
    return p.returncode != 0 and pattern in (p.stderr + p.stdout)


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
def verify_llubi(result, workdir_path, pattern=""):
    safe_ir = _safe_relative(workdir_path, result["ir_file"])
    if not _verify_ir_valid(result["ir_file"], workdir_path):
        return False
    if not _check_no_undef(result["ir_file"], workdir_path):
        log.error("llubi verify: IR contains undef")
        return False
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
        test_crashed = test.returncode != 0
        stdout_diff = ref.stdout != test.stdout

        if pattern == "wrong_output":
            if test_crashed:
                log.error("llubi test crashed — expected wrong_output, pattern changed")
                return False
            return stdout_diff
        if pattern == "nonzero_exit":
            if test_crashed:
                log.info("llubi test crashed (signal=%d) — confirmed nonzero_exit",
                         -test.returncode if test.returncode < 0 else test.returncode)
                return True
            log.error("llubi test exited 0 — expected nonzero_exit, pattern changed")
            return False
        if pattern == "infinite_loop":
            # infinite_loop is handled by TimeoutExpired above — if we reach
            # here the process exited normally, which is NOT infinite_loop.
            log.error("llubi test exited — expected infinite_loop")
            return False
        log.error("verify llubi: unknown pattern %r", pattern)
        return False
    except subprocess.TimeoutExpired:
        if pattern == "infinite_loop":
            log.info("verify llubi timeout — confirmed infinite loop")
            return True
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
    if not _verify_ir_valid(result["ir_file"], workdir_path):
        return False
    if not _check_no_undef(result["ir_file"], workdir_path):
        log.error("alive2 verify: IR contains undef")
        return False
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
def verify_lli(result, workdir_path, pattern=""):
    safe_ir = _safe_relative(workdir_path, result["ir_file"])
    if not _verify_ir_valid(result["ir_file"], workdir_path):
        return False
    if not _check_no_undef(result["ir_file"], workdir_path):
        log.error("lli verify: IR contains undef")
        return False
    if not _check_target_triple_x86(result["ir_file"], workdir_path):
        log.error("lli verify: reproducer must have target triple starting with x86_64")
        return False
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
        test_crashed = test.returncode != 0
        stdout_diff = ref.stdout != test.stdout

        if pattern == "wrong_output":
            if test_crashed:
                log.error("lli test crashed — expected wrong_output, pattern changed")
                return False
            return stdout_diff
        if pattern == "nonzero_exit":
            if test_crashed:
                log.info("lli test crashed (signal=%d) — confirmed nonzero_exit",
                         -test.returncode if test.returncode < 0 else test.returncode)
                return True
            log.error("lli test exited 0 — expected nonzero_exit, pattern changed")
            return False
        if pattern == "infinite_loop":
            log.error("lli test exited — expected infinite_loop")
            return False
        log.error("verify lli: unknown pattern %r", pattern)
        return False
    except subprocess.TimeoutExpired:
        if pattern == "infinite_loop":
            log.info("verify lli timeout — confirmed infinite loop")
            return True
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
    pattern = meta.get("pattern", "")
    if result.get("type") == "crash":
        # pattern originates exclusively from extract.json.
        if not pattern:
            log.error("crash verification requires pattern from extract.json")
            return False
        return verify_crash(result, workdir_path, pattern)
    if result.get("oracle") == "llubi":
        return verify_llubi(result, workdir_path, pattern)
    if result.get("oracle") == "alive2":
        return verify_alive2(result, workdir_path)
    if result.get("oracle") == "lli":
        if not _check_main_no_params(result["ir_file"], workdir_path):
            log.warning("verify: lli reduced IR main() has params")
            return False
        return verify_lli(result, workdir_path, pattern)
    log.error("verify: cannot verify result with type=%r oracle=%r",
              result.get("type"), result.get("oracle"))
    return False


def verify_extract_consistency(meta, result, workdir_path):
    """Cross-check extract.json metadata against reduce result.json.

    pattern is validated against meta only — result.json no longer
    carries pattern (it originates exclusively from extract.json).
    """
    bug_type = meta.get("type", "")
    result_type = result.get("type", "")
    if bug_type and result_type and bug_type != result_type:
        log.warning("type mismatch: extract=%s result=%s", bug_type, result_type)
        return False

    pattern = meta.get("pattern", "")
    if bug_type == "crash" and not pattern:
        log.warning("extract type=crash but pattern is empty")
        return False
    if bug_type == "miscompilation" and pattern not in ("wrong_output", "nonzero_exit", "infinite_loop"):
        log.warning("extract type=miscompilation but pattern is not a recognized value: %r", pattern)

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


_MAIN_NO_PARAMS_RE = re.compile(r"define\s+\S+\s+@main\s*\(\s*\)")
_TARGET_TRIPLE_X86_RE = re.compile(r'target\s+triple\s*=\s*"x86_64')

# Matches 'undef' as a standalone value in LLVM IR, preceded by whitespace
# and followed by a word boundary (comma, newline, space, etc.).
_UNDEF_RE = re.compile(r"\sundef\b")


def _check_main_no_params(reproducer_file, workdir_path):
    """Verify that main() has no parameters (required for backend miscompilation).

    llubi_legacy and lli produce different output when main() uses argc/argv
    because llubi_legacy does not pass command-line arguments. Backend
    miscompilation reproducers MUST have `i32 @main()` with empty params.
    """
    safe_ir = _safe_relative(workdir_path, reproducer_file)
    try:
        content = workdir.read(safe_ir)
    except (ValueError, OSError):
        return False
    return bool(_MAIN_NO_PARAMS_RE.search(content))


def _check_target_triple_x86(reproducer_file, workdir_path):
    """Verify that the IR file has an x86_64 target triple.

    lli runs on the local x86_64 host and cannot JIT IR compiled for other
    architectures. Backend miscompilation reproducers MUST declare
    target triple = "x86_64...".

    ACCEPTED RISK (F74): The regex checks for a literal "x86_64 substring
    in the target triple string. It will also match e.g.
    target triple = "x86_64-unknown-linux-gnu" (valid) and
    target triple = "x86_64h-apple-darwin" (valid for x86_64 on Darwin).
    It will NOT match "x86_64" inside comments or unrelated fields
    because the regex requires the `target triple = "` prefix.
    """
    safe_ir = _safe_relative(workdir_path, reproducer_file)
    try:
        content = workdir.read(safe_ir)
    except (ValueError, OSError):
        return False
    return bool(_TARGET_TRIPLE_X86_RE.search(content))


def _check_no_undef(ir_file, workdir_path):
    """Verify the IR file does not contain undef values.

    undef can non-deterministically mask miscompilations — alive2
    handles it differently, and llubi/lli may produce inconsistent
    results. The reducer agent is instructed to replace undef with
    zero/null/poison.
    """
    safe_ir = _safe_relative(workdir_path, ir_file)
    try:
        content = workdir.read(safe_ir)
    except (ValueError, OSError):
        return False
    return not bool(_UNDEF_RE.search(content))


def _verify_ir_valid(ir_file, workdir_path):
    """Run opt -passes=verify to confirm the IR is valid before verification."""
    opt_path = str(config.LLVM_BIN / "opt")
    safe_ir = _safe_relative(workdir_path, ir_file)
    try:
        p = _run_process(
            [opt_path, "-passes=verify", "-disable-output", safe_ir],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=str(workdir_path), timeout=config.VERIFY_TIMEOUT,
        )
        if p.returncode != 0:
            log.error("ir verify failed: %s", p.stderr[:200])
            return False
        return True
    except subprocess.TimeoutExpired:
        log.error("ir verify timed out")
        return False
    except OSError:
        log.exception("ir verify os error")
        return False


def verify_extract(meta, workdir_path):
    """Independently reproduce the bug from extract.json metadata.

    Reuses the same verify functions that validate the reducer's result.json
    to confirm the extractor's classification before the reducer runs.
    Returns True if the bug reproduces.

    ACCEPTED RISK (R19): verify_extract constructs a synthetic result dict
    from extract.json fields and passes it to verify_crash / verify_llubi /
    verify_lli. If those functions add new required fields that the synthetic
    dict does not supply (e.g. KeyError from direct dict access), the
    verify step fails and the issue is dropped. The set of required fields
    across the three verify functions is small and stable.
    """
    bug_type = meta.get("type", "")
    oracle = meta.get("oracle", "")
    args = meta.get("args", "")
    pattern = meta.get("pattern", "")

    if bug_type == "crash":
        if not pattern:
            log.error("verify_extract: crash type missing pattern")
            return False
        result = {
            "oracle": oracle,
            "args": args,
            "ir_file": meta["reproducer_file"],
        }
        return verify_crash(result, workdir_path, pattern)

    if bug_type == "miscompilation":
        reproducer = meta["reproducer_file"]
        if not _check_no_undef(reproducer, workdir_path):
            log.warning("verify_extract: miscomp reproducer contains undef")
            return False
        # Backend miscompilation requires main() with no parameters —
        # llubi_legacy and lli disagree on argc/argv.
        if oracle == "llc":
            if not _check_main_no_params(reproducer, workdir_path):
                log.warning("verify_extract: backend miscomp reproducer main() has params")
                return False
            if not _check_target_triple_x86(reproducer, workdir_path):
                log.warning("verify_extract: backend miscomp reproducer must have target triple starting with x86_64")
                return False
        result = {
            "ir_file": meta["reproducer_file"],
            "args": args,
        }
        if oracle == "opt":
            result["llubi_args"] = "--reduce-mode --max-steps 1000000"
            return verify_llubi(result, workdir_path, pattern)
        if oracle == "llc":
            result["llubi_args"] = "--reduce-mode --max-steps 1000000"
            result["lli_args"] = ""
            return verify_lli(result, workdir_path, pattern)
        log.error("verify_extract: miscompilation with unknown oracle=%r", oracle)
        return False

    log.error("verify_extract: unknown bug type=%r", bug_type)
    return False


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


def _generate_report(meta, result, workdir_path, issue_id, timing=None):
    """Generate reduction report mechanically from verified data.

    Replaces the AI agent-generated report.md with deterministic output
    built from extract.json, result.json, and the reduced IR file.
    """
    bug_type = meta.get("type", "unknown")
    pattern = meta.get("pattern", "")

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
    # pattern is inserted into inline markdown code spans; the
    # reducer agent produces well-formed strings — agent output
    # is trusted.
    oracle = result.get("oracle", "")
    if oracle:
        lines.append(f"**Oracle:** {oracle}")
        if oracle in ("llubi", "alive2"):
            lines.append("**Scope:** middle-end")
        elif oracle == "lli":
            lines.append("**Scope:** backend")
    if bug_type == "crash" and pattern:
        lines.append(f"**Crash pattern:** `{pattern}`")
    elif bug_type == "miscompilation" and pattern:
        lines.append(f"**Pattern:** `{pattern}`")

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
    if timing:
        lines.append("## Timing")
        lines.append("")
        lines.append(f"**Total:** {int(timing['total'])}s")
        for label in ("security-reviewer", "extractor", "reducer"):
            key = label.replace("-", "_")
            if key in timing:
                lines.append(f"- **{label}:** {int(timing[key])}s")
        lines.append("")

    lines.append("")
    lines.append("## Reduced IR")
    lines.append("")

    # Prepend an invocation comment showing the tool + args that trigger the bug.
    args = result.get("args", "")
    invocation = None
    if bug_type == "crash":
        invocation = f"; {oracle} {args} {ir_file}".rstrip()
    elif oracle in ("llubi", "alive2"):
        invocation = f"; opt {args} {ir_file}".rstrip()
    elif oracle == "lli":
        invocation = f"; lli {args} {ir_file}".rstrip()
    if invocation:
        invocation = " ".join(invocation.split())

    # ACCEPTED RISK: Reduced IR content is inserted verbatim into a markdown
    # code fence. If the IR contains triple backticks the code block may break.
    # The IR is agent-generated from llvm-reduce output — the agent is trusted
    # to produce well-formed, benign content, and in practice reduced IR rarely
    # contains backtick sequences that would corrupt the markdown structure.
    lines.append("```llvm")
    if invocation:
        lines.append(invocation)
    lines.append(ir_content)
    lines.append("```")

    lines.append("")
    lines.append("## Steps to reproduce")
    lines.append("")

    args = result.get("args", "")

    # ACCEPTED RISK (F44): The bash command in the reproduction steps
    # is built by naive string concatenation (oracle + args + ir_file)
    # rather than by reconstructing from shlex.split(args) as the verify
    # step uses. This means the reported command may not be structurally
    # identical to what was actually executed (e.g. quoted arguments
    # appear differently). The command is for human reference only and
    # the reported result has passed mechanical verification. No shell
    # safety issue arises because the report is submitted as a GitHub
    # issue body (GitHub-flavored markdown), not executed by the daemon.
    if bug_type == "crash":
        cmd = f"{oracle} {args} {ir_file}"
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
            lli_cmd = f"lli {lli_args} {ir_file}".strip()
            lli_cmd = " ".join(lli_cmd.split())
            lines.append("```bash")
            lines.append("# Reference:")
            lines.append(f"llubi_legacy {llubi_args} {ir_file}")
            lines.append("# Transformed (incorrect, via backend):")
            lines.append(lli_cmd)
            lines.append("```")

    return "\n".join(lines)


def reprocess_issue(issue):
    t_start = time.time()
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
               for pfx in config.SKIP_LABEL_PREFIXES
               if lbl.startswith(pfx) and lbl not in config.SKIP_LABEL_ALLOW}
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
    t0 = time.time()
    ok = opencode.run(
        agent="security-reviewer",
        workdir=wd,
        prompt=review_prompt,
        timeout=config.REVIEW_TIMEOUT,
        shutdown_check=lambda: config._shutdown_requested,
    )
    t_review = time.time() - t0
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
        "3. Determine the pattern:\n"
        "   - Crash: a literal substring from the actual crash output (e.g. 'Assertion `X` failed').\n"
        "   - Miscompilation: 'wrong_output' (stdout differs), 'nonzero_exit' (oracle crashes or exits non-zero), "
        "or 'infinite_loop' (oracle times out / hangs).\n"
        "4. Determine args: args is ALWAYS passed to the tool indicated by oracle:\n"
        "   - oracle=opt → opt pipeline (e.g. -passes='default<O2>')\n"
        "   - oracle=llc crash → llc flags (default \"\")\n"
        "   - oracle=llc miscompilation → lli flags (default \"\" — lli runs the already-optimized full_opt.ll)\n"
        "5. CRITICAL: The moment you have reproduced the bug, write extract.json and "
        "STOP. Do NOT run more commands. Do NOT read IR files. "
        "If NOT reproduced after ONE attempt, try ONE variation.\n"
        "After writing extract.json, self-validate: verify-extract\n\n"
        "extract.json schema:\n"
        '{"type": "crash|miscompilation", "reproducer_file": "<filename>", '
        '"args": "<opt/llc arguments>", "oracle": "opt|llc", '
        '"pattern": "<crash substring, or wrong_output|nonzero_exit|infinite_loop>"}'
    )
    t0 = time.time()
    ok = opencode.run(
        agent="extractor",
        workdir=wd,
        prompt=extract_prompt,
        timeout=config.EXTRACT_TIMEOUT,
        shutdown_check=lambda: config._shutdown_requested,
    )
    t_extract = time.time() - t0
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

    # Independently reproduce the bug to confirm the extractor's classification
    # before spending reducer time on it.
    if not verify_extract(meta, wd):
        log.warning("issue=%d extract reproduction failed", issue_id)
        mark_dropped(issue_id, "extract_verify_failed")
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
    reduce_prompt = "Read extract.json to determine the bug type. Load the appropriate skill (llvm-crash-reduce or llvm-miscompile-reduce) and reduce the reproducer. Write result.json. After writing result.json, self-validate: verify-result"
    t0 = time.time()
    ok = opencode.run(
        agent="reducer",
        workdir=wd,
        prompt=reduce_prompt,
        timeout=config.REDUCE_TIMEOUT,
        shutdown_check=lambda: config._shutdown_requested,
    )
    t_reduce = time.time() - t0
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
        timing = {
            "total": time.time() - t_start,
            "security_reviewer": t_review,
            "extractor": t_extract,
            "reducer": t_reduce,
        }
        report = _generate_report(meta, result, wd, issue_id, timing=timing)
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
    # Shutdown is checked via config.check_shutdown() in every blocking
    # subprocess call and polling loop. A SIGTERM/SIGINT raises SystemExit
    # which propagates through except Exception handlers and stops the daemon
    # cleanly with atexit cleanup (pidfile removal).

    log.info("llvm-autoreduce daemon starting")

    last_toolchain_update = 0.0
    next_issue_poll = 0.0
    TOOLCHAIN_INTERVAL = config.TOOLCHAIN_INTERVAL
    ISSUE_POLL_INTERVAL = config.ISSUE_POLL_INTERVAL

    while True:
        config.check_shutdown()
        now = time.time()

        # Toolchain update — independent of issue polling.
        if now - last_toolchain_update >= TOOLCHAIN_INTERVAL:
            log.info("round start")
            try:
                tools.update_all()
                last_toolchain_update = time.time()
            except tools.BuildError:
                log.exception("toolchain build error, will retry next cycle")
                _check_toolchain()
            except subprocess.TimeoutExpired:
                log.exception("toolchain build timeout, will retry next cycle")
                _check_toolchain()
            config.check_shutdown()
            if _check_toolchain():
                _cleanup_old_workdirs()
                log.info("toolchain update ok")
            else:
                log.critical("toolchain health check failed after update")
                # Back off briefly before the next cycle.
                deadline = time.time() + 60
                while time.time() < deadline:
                    config.check_shutdown()
                    time.sleep(1)
                continue

        # Issue polling — independent of toolchain updates.
        if time.time() >= next_issue_poll:
            try:
                issues = github.fetch_issues()
                log.info("fetched %d issues", len(issues))
                for issue in issues:
                    config.check_shutdown()
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
            except Exception:
                log.exception("round failed")
            next_issue_poll = time.time() + ISSUE_POLL_INTERVAL

        config.check_shutdown()
        time.sleep(1)

    # Not reachable — SystemExit propagates out of main().
    log.info("daemon shutting down")


if __name__ == "__main__":
    main()
