"""Configuration constants for llvm-autoreduce."""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
WORK_ROOT = PROJECT_ROOT / "work"

LLVM_TRUNK = WORK_ROOT / "llvm-trunk"
ALIVE2_TRUNK = WORK_ROOT / "alive2-trunk"
LLUBI_TRUNK = WORK_ROOT / "llubi-trunk"

LLVM_BIN = LLVM_TRUNK / "build" / "bin"
ALIVE2_BIN = ALIVE2_TRUNK / "build" / "alive-tv"
LLUBI_BIN = LLUBI_TRUNK / "build" / "llubi_legacy"

KNOWN_GOOD = WORK_ROOT / ".known-good"
PROCESSED = WORK_ROOT / "processed.txt"
DROPPED = WORK_ROOT / "dropped.txt"
DAEMON_LOG = WORK_ROOT / "daemon.log"

AUTOREDUCE_TOKEN = os.environ.get("AUTOREDUCE_TOKEN", "")
GITHUB_API = "https://api.github.com"
SOURCE_REPO = "llvm/llvm-project"
TARGET_REPO = "dtcxzyw/llvm-autoreduce"

REVIEW_TIMEOUT = 300
# Single opencode reduction run typically completes in under 2 minutes
# (opt-bisect-limit binary search + llvm-reduce on already-small inputs).
# The generous ceiling accounts for API latency and verbose LLM reasoning.
REDUCE_TIMEOUT = 900
VERIFY_TIMEOUT = 120
# ACCEPTED RISK (F24): DAEMON_INTERVAL and ISSUES_PER_ROUND are hardcoded.
# No environment variable override is provided for tuning poll frequency
# or batch size. For development or testing, modify these constants directly.
DAEMON_INTERVAL = 1800

ISSUES_PER_ROUND = 20

# Issues carrying any of these labels are skipped — they are known non-bug
# categories. The daemon only excludes based on label presence, never on
# label absence (unlabeled issues are still processed).
SKIP_LABELS = frozenset(
    {
        "question",
        "feature request",
        "feature-request",
        "documentation",
        "duplicate",
        "invalid",
        "wontfix",
    }
)
