#!/usr/bin/env python3
"""Main daemon loop for llvm-autoreduce."""

import atexit
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
from logging.handlers import RotatingFileHandler

import requests

from . import config, extract, github, opencode, tools, workdir

log = logging.getLogger("daemon")


def setup_logging():
    config.WORK_ROOT.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(config.DAEMON_LOG, maxBytes=10_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(handler)
    root.addHandler(logging.StreamHandler(sys.stderr))


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


def mark_processed(issue_id):
    # ACCEPTED RISK (F5): No file locking — concurrent daemon instances may
    # race on processed.json, leading to duplicate processing or data loss.
    # ACCEPTED RISK (F10): processed.json grows without bound — every
    # processed issue ID is stored permanently. For a long-running daemon
    # this file accumulates entries linearly in time, and parsing the full
    # JSON on every is_processed() call becomes increasingly expensive.
    # The cost remains tolerable for months of operation on llvm/llvm-project
    # since the daemon processes at most 20 issues per round with a 30-minute
    # interval. Manual pruning is acceptable for now.
    processed = set()
    if config.PROCESSED.exists():
        try:
            processed = set(json.loads(config.PROCESSED.read_text()))
        except (json.JSONDecodeError, OSError):
            log.warning("processed.json corrupt, resetting")
    processed.add(str(issue_id))
    tmp = config.PROCESSED.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(processed)))
    tmp.replace(config.PROCESSED)


def is_processed(issue_id):
    if not config.PROCESSED.exists():
        return False
    try:
        return str(issue_id) in json.loads(config.PROCESSED.read_text())
    except (json.JSONDecodeError, OSError):
        log.warning("processed.json corrupt during read, treating as empty")
        return False


def read_prompt(name):
    path = config.PROJECT_ROOT / "prompts" / name
    return path.read_text()


def _set_limits():
    """Set resource limits on child processes to prevent OOM."""
    try:
        # 8 GB address space — enough for opt/llc/llvm-reduce on reduced IR.
        limit = 8 * 1024 ** 3
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except (ValueError, OSError):
        pass


VALID_BUG_TYPES = frozenset({"crash", "miscompilation"})


def _validate_verdict(verdict):
    """Reject review.json with unexpected values to prevent prompt injection."""
    if verdict.get("valid") is not True:
        raise ValueError(f"review.json valid is not True: {verdict.get('valid')!r}")
    malicious = verdict.get("malicious")
    if malicious is not None and malicious not in (True, False):
        raise ValueError(f"review.json malicious is not bool: {malicious!r}")


def _validate_meta(meta):
    """Reject extract.json with unexpected values."""
    bug_type = meta.get("bug_type", "")
    if bug_type not in VALID_BUG_TYPES:
        raise ValueError(f"extract.json bug_type not in {VALID_BUG_TYPES}: {bug_type!r}")
    reproducer = meta.get("reproducer_file", "")
    if reproducer and ("/" in reproducer or "\\" in reproducer or "\0" in reproducer):
        raise ValueError(f"extract.json reproducer_file contains path separators: {reproducer!r}")
    pipeline = meta.get("pipeline", "")
    if pipeline:
        # Shell metacharacters that could cause command injection. Note:
        # < > are intentionally excluded — they are part of LLVM pass syntax
        # (e.g. -passes='default<O2>') and are safe with list-based subprocess.
        # ACCEPTED RISK (R4): The pipeline string passes through to the reducer
        # agent's prompt, which has bash access. Agents may reinterpret < > as
        # shell redirection when generating scripts. This validator only guards
        # against direct process.Popen injection; downstream AI behavior is
        # constrained only by natural-language prompts, not technical controls.
        dangerous = {"$", "`", ";", "|", "&", "(", ")", "{", "}"}
        if any(c in pipeline for c in dangerous):
            raise ValueError(f"extract.json pipeline contains shell metacharacters: {pipeline!r}")
    crash_pattern = meta.get("crash_pattern", "")
    # crash_pattern is a literal substring (not regex) matched against
    # crash output via plain string containment.
    if crash_pattern and len(crash_pattern) > 2000:
        raise ValueError(f"extract.json crash_pattern too long: {len(crash_pattern)} chars")


def _safe_relative(workdir_path, filename):
    """Resolve filename against workdir and reject path traversal."""
    resolved = (workdir_path / filename).resolve()
    workdir_prefix = str(workdir_path.resolve()) + os.sep
    if not str(resolved).startswith(workdir_prefix):
        raise ValueError(f"Path traversal rejected: {filename!r}")
    return str(resolved)


def _validate_result(result):
    """Reject result.json with missing required fields."""
    if "ir_file" not in result:
        raise ValueError("result.json missing required field: ir_file")
    if result.get("type") == "crash":
        crash_pattern = result.get("crash_pattern", "")
        if not crash_pattern:
            raise ValueError("result.json type=crash but crash_pattern is empty")
        if len(crash_pattern) > 2000:
            raise ValueError(f"result.json crash_pattern too long: {len(crash_pattern)} chars")


def verify_crash(result, workdir_path):
    tool = result.get("tool", "opt")
    safe_ir = _safe_relative(workdir_path, result["ir_file"])
    # ACCEPTED RISK (R5): result.json args comes from the reducer agent
    # (which has bash access). List-based subprocess.run prevents shell
    # injection but does not prevent argument injection into LLVM tools
    # (e.g. -o /dev/null). This is acceptable because the reducer agent
    # already has unrestricted bash access within the workdir.
    cmd = [tool] + shlex.split(result.get("args", "")) + [safe_ir]
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True,
            cwd=str(workdir_path), timeout=config.VERIFY_TIMEOUT,
            preexec_fn=_set_limits,
        )
    except subprocess.TimeoutExpired:
        log.error("verify crash timeout")
        return False
    except OSError:
        log.exception("verify crash os error")
        return False
    needle = result.get("crash_pattern", "")
    return needle in (p.stderr + p.stdout)


# ACCEPTED RISK (R6): verify_llubi compares exact stdout strings
# to detect miscompilation. Non-deterministic output (timestamps,
# metadata, randomization) may cause false positives.
def verify_llubi(result, workdir_path):
    safe_ir = _safe_relative(workdir_path, result["ir_file"])
    args = result.get("args", "")
    # ACCEPTED RISK (R7): llubi_args from result.json may inject arguments
    # into llubi_legacy. The reducer agent already has unrestricted bash
    # access within the workdir.
    llubi_args = result.get("llubi_args", "--max-steps 1000000")
    try:
        ref = subprocess.run(
            [str(config.LLUBI_BIN)] + shlex.split(llubi_args) + [safe_ir],
            capture_output=True, text=True,
            cwd=str(workdir_path), timeout=config.VERIFY_TIMEOUT,
            preexec_fn=_set_limits,
        )
        if ref.returncode != 0:
            log.error("llubi ref failed: %s", ref.stderr[:200])
            return False

        opt_out = subprocess.run(
            ["opt"] + shlex.split(args) + [safe_ir, "-S"],
            capture_output=True, text=True,
            cwd=str(workdir_path), timeout=config.VERIFY_TIMEOUT,
            preexec_fn=_set_limits,
        )
        if opt_out.returncode != 0:
            log.error("llubi opt failed: %s", opt_out.stderr[:200])
            return False
        transformed = workdir_path / "__transformed.ll"
        transformed.write_text(opt_out.stdout)

        test = subprocess.run(
            [str(config.LLUBI_BIN)] + shlex.split(llubi_args) + ["__transformed.ll"],
            capture_output=True, text=True,
            cwd=str(workdir_path), timeout=config.VERIFY_TIMEOUT,
            preexec_fn=_set_limits,
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
    r"[1-9]\d* incorrect transformations?|ERROR: Value mismatch"
)

_ALIVE2_APPROXIMATION_MARKER = "Alive2 approximated the semantics of the programs"


def verify_alive2(result, workdir_path):
    safe_ir = _safe_relative(workdir_path, result["ir_file"])
    args = result.get("args", "")
    # ACCEPTED RISK (R7): alive2_args from result.json may inject arguments
    # into alive-tv. The reducer agent already has unrestricted bash
    # access within the workdir.
    alive2_args = result.get("alive2_args", "--smt-to=10000")
    try:
        opt_out = subprocess.run(
            ["opt"] + shlex.split(args) + [safe_ir, "-S"],
            capture_output=True, text=True,
            cwd=str(workdir_path), timeout=config.VERIFY_TIMEOUT,
            preexec_fn=_set_limits,
        )
        if opt_out.returncode != 0:
            log.error("alive2 opt failed: %s", opt_out.stderr[:200])
            return False
        transformed = workdir_path / "__transformed.ll"
        transformed.write_text(opt_out.stdout)

        p = subprocess.run(
            [str(config.ALIVE2_BIN), "--disable-undef-input"]
            + shlex.split(alive2_args) + [safe_ir, "__transformed.ll"],
            capture_output=True, text=True,
            cwd=str(workdir_path), timeout=config.VERIFY_TIMEOUT,
            preexec_fn=_set_limits,
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


def verify(result, workdir_path):
    if result.get("type") == "crash":
        return verify_crash(result, workdir_path)
    if result.get("oracle") == "llubi":
        return verify_llubi(result, workdir_path)
    if result.get("oracle") == "alive2":
        return verify_alive2(result, workdir_path)
    return False


def verify_extract_consistency(meta, result):
    """Cross-check extract.json metadata against reduce result.json."""
    bug_type = meta.get("bug_type", "")
    result_type = result.get("type", "")
    if bug_type and result_type and bug_type != result_type:
        log.warning("bug_type mismatch: extract=%s result=%s", bug_type, result_type)
        return False

    crash_pattern = meta.get("crash_pattern", "")
    if bug_type == "crash" and not crash_pattern:
        log.warning("extract bug_type=crash but crash_pattern is empty")
        return False
    if bug_type == "miscompilation" and crash_pattern:
        log.warning("extract bug_type=miscompilation but crash_pattern is non-empty, ignoring crash_pattern")

    if result_type == "crash":
        result_pattern = result.get("crash_pattern", "")
        if crash_pattern and result_pattern and crash_pattern != result_pattern:
            log.warning(
                "crash_pattern mismatch: extract=%s result=%s",
                crash_pattern, result_pattern,
            )
            # non-fatal: reducer may have refined the pattern, just warn
    return True


def _fetch_godbolt(body):
    links = extract.find_godbolt_links(body)
    if not links:
        return []
    sources = []
    for short_id in links:
        try:
            resp = requests.get(
                f"https://godbolt.org/api/shortlink/{short_id}",
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            for session in data.get("sessions", []):
                lang = session.get("language", "ir")
                src = session.get("source", "")
                if src.strip():
                    sources.append((src, lang))
        except (requests.RequestException, json.JSONDecodeError):
            log.exception("godbolt fetch failed id=%s", short_id)
    return sources


def _download_attachments(body, wd):
    for url, filename in extract.find_attachment_urls(body):
        if not filename.lower().endswith((".ll", ".c", ".cpp", ".cxx")):
            continue
        # Prefix to avoid collisions with pipeline output files (result.json, etc.).
        safe_name = f"attach_{filename}"
        try:
            github.download_attachment(url, str(wd / safe_name))
            log.info("attachment downloaded: %s", safe_name)
        except (requests.RequestException, OSError):
            log.exception("attachment download failed: %s", filename)


def reprocess_issue(issue):
    issue_id = issue["number"]
    if is_processed(issue_id):
        return

    log.info("issue=%d processing", issue_id)
    title, body = github.get_issue_info(issue_id)
    body = body or ""

    if not body.strip():
        log.info("issue=%d empty body, skip", issue_id)
        mark_processed(issue_id)
        return

    # ACCEPTED RISK (F11): No label-based pre-filtering — the daemon
    # processes every open issue regardless of labels (question,
    # feature-request, documentation, etc.). Non-bug issues go through
    # security review and extraction before being rejected by
    # _validate_meta (bug_type not in VALID_BUG_TYPES). Label-based
    # filtering is unreliable because GitHub label updates are
    # asynchronous and may not reflect the latest classification at
    # the time of issue fetch.

    wd = workdir.create(issue_id)

    issue_text = f"# Issue #{issue_id}: {title}\n\n{body}"
    workdir.write(wd / "issue.md", issue_text)

    # Step 1: extract all reproducers (code blocks, Godbolt, attachments)
    godbolt_sources = _fetch_godbolt(body)
    _download_attachments(body, wd)
    sources = extract.assemble_reproducers(body, godbolt_sources, wd)
    # ACCEPTED RISK (F12): Reproducers exceeding 8 KB are skipped entirely.
    # Large LLVM IR inputs are almost certainly not yet reduced and would
    # timeout reduction or exhaust LLM context. Skipping them avoids
    # wasting agent inference time on inputs that are too large to be
    # usefully reduced in the current single-pass pipeline. Manual
    # pre-reduction or a future two-phase pipeline can handle these later.
    for _name, content, _lang in sources:
        if len(content) > 8192:
            log.info("issue=%d reproducer %s too large (%d bytes), skip", issue_id, _name, len(content))
            mark_processed(issue_id)
            workdir.cleanup(issue_id)
            return
    reproducer_texts = []
    for name, content, _lang in sources:
        target = wd / name
        workdir.write(target, content)
        reproducer_texts.append(f"### File: {name}\n```\n{content[:8192]}\n```")
    workdir.write(wd / "reproducers.md", "\n\n".join(reproducer_texts))

    # Step 2: security review (bash denied) — audit + classify.
    # ACCEPTED RISK: The security reviewer rejects only code-level malware patterns
    # (system/exec/fork/popen etc.) in input reproducers. It does not defend against:
    #   (R2) Indirect prompt injection — adversarial markdown in issue bodies may
    #        instruct the agent to produce colluding review.json output.
    #   (R3) AI-generated shell scripts — reducer agent later generates and runs
    #        interestingness.sh (via llvm-reduce --test), which the security reviewer
    #        never inspects. The reviewer's coverage is limited to the initial
    #        reproducer content, not downstream AI artifact generation.
    review_prompt = read_prompt("security-reviewer.txt").format(
        issue_file="issue.md",
        reproducer_file="reproducers.md",
    )
    ok = opencode.run(
        agent="security-reviewer",
        workdir=wd,
        prompt=review_prompt,
        timeout=config.REVIEW_TIMEOUT,
    )
    if not ok:
        log.warning("issue=%d review agent failed", issue_id)
        mark_processed(issue_id)
        workdir.cleanup(issue_id)
        return

    review_path = wd / "review.json"
    if not review_path.exists():
        log.warning("issue=%d review.json missing", issue_id)
        mark_processed(issue_id)
        workdir.cleanup(issue_id)
        return

    try:
        verdict = workdir.read_json(review_path)
    except json.JSONDecodeError:
        log.warning("issue=%d review.json invalid", issue_id)
        mark_processed(issue_id)
        workdir.cleanup(issue_id)
        return

    log.info("issue=%d review=%s", issue_id, json.dumps(verdict))

    try:
        _validate_verdict(verdict)
    except ValueError:
        log.warning("issue=%d review.json validation failed", issue_id)
        mark_processed(issue_id)
        workdir.cleanup(issue_id)
        return

    if verdict.get("malicious"):
        log.info("issue=%d skipped: malicious", issue_id)
        mark_processed(issue_id)
        workdir.cleanup(issue_id)
        return

    # Step 3: extract reproducer metadata (bash allowed).
    # ACCEPTED RISK (R9): The extractor agent has bash access even though
    # its primary task is classification and metadata extraction. This is
    # intentional — the extractor is instructed to attempt reproducing the
    # bug first to validate that the reproducer is functional and to
    # capture an accurate crash pattern. Skipping this pre-validation
    # would produce lower-quality extract.json and waste reducer agent
    # time on non-reproducible inputs.
    extract_prompt = read_prompt("extractor.txt")
    ok = opencode.run(
        agent="extractor",
        workdir=wd,
        prompt=extract_prompt,
        timeout=config.REVIEW_TIMEOUT,
    )
    if not ok:
        log.warning("issue=%d extractor agent failed", issue_id)
        mark_processed(issue_id)
        workdir.cleanup(issue_id)
        return

    extract_path = wd / "extract.json"
    if extract_path.exists():
        try:
            meta = workdir.read_json(extract_path)
        except json.JSONDecodeError:
            log.warning("issue=%d extract.json invalid, skip", issue_id)
            mark_processed(issue_id)
            workdir.cleanup(issue_id)
            return
    else:
        log.warning("issue=%d extract.json missing, skip", issue_id)
        mark_processed(issue_id)
        workdir.cleanup(issue_id)
        return
    log.info("issue=%d extract=%s", issue_id, json.dumps(meta))

    try:
        _validate_meta(meta)
    except ValueError:
        log.warning("issue=%d extract.json validation failed", issue_id)
        mark_processed(issue_id)
        workdir.cleanup(issue_id)
        return

    # Step 4: reduction.
    # ACCEPTED RISKS:
    #   (R1) No execution sandbox — the reducer agent has full bash access on the
    #        host user account. It generates and executes shell scripts
    #        (interestingness.sh) via llvm-reduce --test. No chroot, namespace,
    #        seccomp, or container isolation is applied. Confinement relies
    #        solely on natural-language instructions in prompts.
    #   (F1) No retry on transient failures — if opencode.run returns non-zero
    #        (timeout, API error, etc.), the issue is immediately marked as
    #        processed and never retried. Temporary infrastructure errors
    #        permanently skip valid issues.
    #   (F3) Single reproducer file — only meta.reproducer_file is passed to
    #        the reducer. Multi-file reproducer scenarios (e.g. inter-module
    #        bugs requiring multiple .ll files) are not supported and will
    #        silently fail reduction.
    reduce_prompt = read_prompt("reducer.txt").format(
        issue_file="issue.md",
        verdict_type=meta["bug_type"],
        reproducer_file=meta.get("reproducer_file", "repro.ll"),
        crash_pattern=meta.get("crash_pattern", ""),
        pipeline=meta.get("pipeline", "-passes='default<O2>'"),
    )
    ok = opencode.run(
        agent="reducer",
        workdir=wd,
        prompt=reduce_prompt,
        timeout=config.REDUCE_TIMEOUT,
    )
    if not ok:
        log.warning("issue=%d reduce agent failed", issue_id)
        mark_processed(issue_id)
        workdir.cleanup(issue_id)
        return

    result_path = wd / "result.json"
    if not result_path.exists():
        log.warning("issue=%d result.json missing", issue_id)
        mark_processed(issue_id)
        workdir.cleanup(issue_id)
        return

    try:
        result = workdir.read_json(result_path)
    except json.JSONDecodeError:
        log.warning("issue=%d result.json invalid", issue_id)
        mark_processed(issue_id)
        workdir.cleanup(issue_id)
        return

    try:
        _validate_result(result)
    except ValueError:
        log.warning("issue=%d result.json validation failed", issue_id)
        mark_processed(issue_id)
        workdir.cleanup(issue_id)
        return

    # Step 5: verify before submitting
    if not verify_extract_consistency(meta, result):
        log.warning("issue=%d extract-result consistency check failed", issue_id)
        mark_processed(issue_id)
        workdir.cleanup(issue_id)
        return
    if not verify(result, wd):
        log.warning("issue=%d verify failed", issue_id)
        mark_processed(issue_id)
        workdir.cleanup(issue_id)
        return
    log.info("issue=%d verify pass", issue_id)

    # Step 6: submit
    report = workdir.read(wd / "report.md")
    report_title = f"[Reduced] {meta['bug_type']} — #{issue_id}"
    url = github.create_issue(report_title, report)
    log.info("issue=%d submitted %s", issue_id, url)

    mark_processed(issue_id)
    workdir.cleanup(issue_id)


def main():
    setup_logging()
    if not config.AUTOREDUCE_TOKEN:
        log.critical("AUTOREDUCE_TOKEN environment variable is required")
        sys.exit(1)

    # Verify required binaries exist before entering the poll loop.
    missing = []
    for binary in ["opencode", "opt", "clang", "llc", "lli", "llvm-reduce"]:
        path = shutil.which(binary)
        if path is None:
            missing.append(binary)
        else:
            log.info("found %s at %s", binary, path)
    for binary in ["llubi_legacy", "alive-tv"]:
        path = shutil.which(binary) or (
            config.LLUBI_BIN if binary == "llubi_legacy" and config.LLUBI_BIN.exists() else
            config.ALIVE2_BIN if binary == "alive-tv" and config.ALIVE2_BIN.exists() else None
        )
        if path is not None:
            log.info("found %s at %s", binary, path)
        else:
            log.warning("%s not found on PATH — miscompilation oracles will fail", binary)
    if missing:
        log.critical("required binaries not found on PATH: %s", ", ".join(missing))
        sys.exit(1)

    _write_pidfile()
    atexit.register(_remove_pidfile)
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    log.info("llvm-autoreduce daemon starting")

    while not _shutdown_requested:
        try:
            log.info("round start")
            tools.update_all()
            issues = github.fetch_issues()
            log.info("round fetched %d issues", len(issues))
            for issue in issues:
                try:
                    reprocess_issue(issue)
                except Exception:
                    log.exception("issue=%d unhandled error", issue.get("number", "?"))
            log.info("round done")
        except Exception:
            log.exception("round failed")
        if _shutdown_requested:
            break
        time.sleep(config.DAEMON_INTERVAL)

    log.info("daemon shutting down")


if __name__ == "__main__":
    main()
