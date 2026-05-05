#!/usr/bin/env python3
"""Main daemon loop for llvm-autoreduce."""

import json
import logging
import re
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


def mark_processed(issue_id):
    processed = set()
    if config.PROCESSED.exists():
        processed = set(json.loads(config.PROCESSED.read_text()))
    processed.add(str(issue_id))
    config.PROCESSED.write_text(json.dumps(sorted(processed)))


def is_processed(issue_id):
    if not config.PROCESSED.exists():
        return False
    return str(issue_id) in json.loads(config.PROCESSED.read_text())


def read_prompt(name):
    path = config.PROJECT_ROOT / "prompts" / name
    return path.read_text()


def verify_crash(result, workdir_path):
    cmd = f"opt -passes='{result['pass_name']}'"
    if result.get("opt_args"):
        cmd += f" {result['opt_args']}"
    cmd += f" {result['ir_file']}"
    try:
        p = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            cwd=str(workdir_path), timeout=config.VERIFY_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        log.error("verify crash timeout")
        return False
    pattern = result.get("crash_pattern", "")
    return bool(re.search(pattern, p.stderr + p.stdout, re.DOTALL))


def verify_llubi(result, workdir_path):
    ir_file = result["ir_file"]
    pass_name = result["pass_name"]
    llubi_args = result.get("llubi_args", "--max-steps 1000000")
    try:
        ref = subprocess.run(
            f"{config.LLUBI_BIN} {llubi_args} {ir_file}",
            shell=True, capture_output=True, text=True,
            cwd=str(workdir_path), timeout=config.VERIFY_TIMEOUT,
        )
        if ref.returncode != 0:
            log.error("llubi ref failed: %s", ref.stderr[:200])
            return False

        opt_out = subprocess.run(
            f"opt -passes='{pass_name}' {ir_file} -S",
            shell=True, capture_output=True, text=True,
            cwd=str(workdir_path), timeout=config.VERIFY_TIMEOUT,
        )
        transformed = workdir_path / "__transformed.ll"
        transformed.write_text(opt_out.stdout)

        test = subprocess.run(
            f"{config.LLUBI_BIN} {llubi_args} __transformed.ll",
            shell=True, capture_output=True, text=True,
            cwd=str(workdir_path), timeout=config.VERIFY_TIMEOUT,
        )
        return ref.stdout != test.stdout
    except subprocess.TimeoutExpired:
        log.error("verify llubi timeout")
        return False


def verify_alive2(result, workdir_path):
    ir_file = result["ir_file"]
    pass_name = result["pass_name"]
    alive2_args = result.get("alive2_args", "--smt-to=10000")
    try:
        opt_out = subprocess.run(
            f"opt -passes='{pass_name}' {ir_file} -S",
            shell=True, capture_output=True, text=True,
            cwd=str(workdir_path), timeout=config.VERIFY_TIMEOUT,
        )
        transformed = workdir_path / "__transformed.ll"
        transformed.write_text(opt_out.stdout)

        p = subprocess.run(
            f"{config.ALIVE2_BIN} --disable-undef-input {alive2_args} {ir_file} __transformed.ll",
            shell=True, capture_output=True, text=True,
            cwd=str(workdir_path), timeout=config.VERIFY_TIMEOUT,
        )
        output = p.stderr + p.stdout
        return p.returncode != 0 and "Transformation seems to be correct!" not in output
    except subprocess.TimeoutExpired:
        log.error("verify alive2 timeout")
        return False


def verify(result, workdir_path):
    if result["type"] == "crash":
        return verify_crash(result, workdir_path)
    if result.get("oracle") == "llubi":
        return verify_llubi(result, workdir_path)
    if result.get("oracle") == "alive2":
        return verify_alive2(result, workdir_path)
    return False


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
        except Exception:
            log.exception("godbolt fetch failed id=%s", short_id)
    return sources


def _download_attachments(body, wd):
    for url, filename in extract.find_attachment_urls(body):
        if not filename.lower().endswith((".ll", ".c", ".cpp", ".cxx")):
            continue
        try:
            github.download_attachment(url, str(wd / filename))
            log.info("attachment downloaded: %s", filename)
        except Exception:
            log.exception("attachment download failed: %s", filename)


def reprocess_issue(issue):
    issue_id = issue["number"]
    if is_processed(issue_id):
        return

    log.info("issue=%d processing", issue_id)
    body = github.get_issue_body(issue_id) or ""
    title = github.get_issue_title(issue_id)

    if not body.strip():
        log.info("issue=%d empty body, skip", issue_id)
        mark_processed(issue_id)
        return

    wd = workdir.create(issue_id)

    issue_text = f"# Issue #{issue_id}: {title}\n\n{body}"
    workdir.write(wd / "issue.md", issue_text)

    # Step 1: extract all reproducers (code blocks, Godbolt, attachments)
    godbolt_sources = _fetch_godbolt(body)
    _download_attachments(body, wd)
    sources = extract.assemble_reproducers(body, godbolt_sources, wd)
    reproducer_texts = []
    for name, content, _lang in sources:
        target = wd / name
        workdir.write(target, content)
        reproducer_texts.append(f"### File: {name}\n```\n{content[:8192]}\n```")
    workdir.write(wd / "reproducers.md", "\n\n".join(reproducer_texts))

    # Step 2: security review (sees issue.md + all extracted reproducers)
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

    if verdict.get("type") == "unrelated" or verdict.get("malicious"):
        log.info("issue=%d skipped: type=%s malicious=%s",
                 issue_id, verdict.get("type"), verdict.get("malicious"))
        mark_processed(issue_id)
        workdir.cleanup(issue_id)
        return

    # Step 3: reduction
    reduce_prompt = read_prompt("reducer.txt").format(
        issue_file="issue.md",
        verdict_type=verdict["type"],
        reproducer_file=verdict.get("reproducer_file", "repro.ll"),
        crash_pattern=verdict.get("crash_pattern", ""),
        pipeline=verdict.get("pipeline", "-passes='default<O2>'"),
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

    # Step 4: verify before submitting
    if not verify(result, wd):
        log.warning("issue=%d verify failed", issue_id)
        mark_processed(issue_id)
        workdir.cleanup(issue_id)
        return
    log.info("issue=%d verify pass", issue_id)

    # Step 5: submit
    report = workdir.read(wd / "report.md")
    report_title = f"[Reduced] {verdict['type']} — #{issue_id}"
    url = github.create_issue(report_title, report)
    log.info("issue=%d submitted %s", issue_id, url)

    mark_processed(issue_id)
    workdir.cleanup(issue_id)


def main():
    setup_logging()
    if not config.GITHUB_TOKEN:
        log.critical("GITHUB_TOKEN environment variable is required")
        sys.exit(1)
    log.info("llvm-autoreduce daemon starting")

    while True:
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
        time.sleep(config.DAEMON_INTERVAL)


if __name__ == "__main__":
    main()
