#!/usr/bin/env python3
"""Reprocess a single LLVM issue end-to-end — toolchain update, filter, review, reduce.

Usage:
    python scripts/reprocess-single.py <issue-number>

Logs to work/issue-<N>.log.
"""

import argparse
import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

# Ensure the package is importable when run from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from llvm_autoreduce import config, daemon, github, tools


def setup_logging(issue_id):
    config.WORK_ROOT.mkdir(parents=True, exist_ok=True)
    log_path = config.WORK_ROOT / f"issue-{issue_id}.log"
    handler = TimedRotatingFileHandler(log_path, when="midnight", interval=1, backupCount=10)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(handler)
    root.addHandler(logging.StreamHandler(sys.stderr))


def main():
    parser = argparse.ArgumentParser(description="Reprocess a single LLVM issue")
    parser.add_argument("issue_id", type=int, help="GitHub issue number")
    args = parser.parse_args()

    setup_logging(args.issue_id)
    log = logging.getLogger("single")
    log.info("reprocess-single starting for issue=%d", args.issue_id)

    # Step 1: update toolchain
    log.info("toolchain update start")
    tools.update_all()
    log.info("toolchain update ok")
    if not daemon._check_toolchain():
        log.critical("toolchain health check failed, aborting")
        sys.exit(1)

    # Step 2: clear the issue from processed.txt so it is always reprocessed.
    # The pipeline's own mark_processed/mark_dropped calls will re-add it
    # after completion, maintaining the file for the daemon.
    issue_id = args.issue_id
    if config.PROCESSED.exists():
        lines = config.PROCESSED.read_text().splitlines()
        kept = [line for line in lines if line.strip() != str(issue_id)]
        config.PROCESSED.write_text("\n".join(kept) + ("\n" if kept else ""))
    # Reset the in-memory cache so is_processed() reloads from disk.
    daemon._processed_cache = None

    # Step 3: fetch issue metadata
    log.info("fetching issue metadata")
    issue_url = f"{config.GITHUB_API}/repos/{config.SOURCE_REPO}/issues/{issue_id}"
    resp = github._request("GET", issue_url)
    raw = resp.json()
    issue = {
        "number": raw["number"],
        "labels": raw.get("labels", []),
    }

    # Step 4: run the full pipeline (label filter → review → extract → reduce → verify → report)
    daemon.reprocess_issue(issue)

    log.info("reprocess-single done for issue=%d", args.issue_id)


if __name__ == "__main__":
    main()
