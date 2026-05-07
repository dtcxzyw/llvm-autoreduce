"""Configuration constants for llvm-autoreduce."""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
WORK_ROOT = PROJECT_ROOT / "work"

LLVM_TRUNK = WORK_ROOT / "llvm-trunk"
ALIVE2_TRUNK = WORK_ROOT / "alive2-trunk"
LLUBI_TRUNK = WORK_ROOT / "llubi-trunk"

LLVM_BIN = Path(os.environ.get("LLVM_BIN_PATH", LLVM_TRUNK / "build" / "bin"))
ALIVE2_BIN = Path(os.environ.get("ALIVE2_PATH", ALIVE2_TRUNK / "build" / "bin" / "alive-tv"))
LLUBI_BIN = Path(os.environ.get("LLUBI_PATH", LLUBI_TRUNK / "build" / "bin" / "llubi_legacy"))

KNOWN_GOOD = WORK_ROOT / ".known-good"
PROCESSED = WORK_ROOT / "processed.txt"
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
