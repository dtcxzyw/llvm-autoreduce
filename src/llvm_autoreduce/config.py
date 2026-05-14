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
# Separate token for labeling llvm/llvm-project issues.
# Requires write access to the SOURCE_REPO (llvm/llvm-project) to add labels.
AUTOREDUCE_LLVM_TOKEN = os.environ.get("AUTOREDUCE_LLVM_TOKEN", "")
GITHUB_API = "https://api.github.com"
SOURCE_REPO = "llvm/llvm-project"
TARGET_REPO = "dtcxzyw/llvm-autoreduce"

REVIEW_TIMEOUT = 300
# Extractor needs more time — it compiles C sources and runs toolchain
# commands to reproduce the bug before classification.
EXTRACT_TIMEOUT = 1200
# 30-minute timeout for reduction: bisect via opt-bisect-limit + llvm-reduce.
# Margin for API latency, LLM reasoning, and large O2 pass pipelines.
REDUCE_TIMEOUT = 1800
VERIFY_TIMEOUT = 120
# ACCEPTED RISK (F24): DAEMON_INTERVAL and ISSUES_PER_ROUND are hardcoded.
# No environment variable override is provided for tuning poll frequency
# or batch size. For development or testing, modify these constants directly.
DAEMON_INTERVAL = 1800

# Issue fetching runs independently of toolchain updates.
ISSUE_POLL_INTERVAL = 120   # 2 minutes between issue-list fetches
TOOLCHAIN_INTERVAL = 7200   # 2 hours between LLVM rebuilds

# Shutdown flag — set by signal handlers, checked by all blocking loops.
_shutdown_requested = False


def request_shutdown():
    global _shutdown_requested
    _shutdown_requested = True


def check_shutdown():
    if _shutdown_requested:
        raise SystemExit(0)

ISSUES_PER_ROUND = 20

# Labels whose prefix matches any entry in this set are skipped.
# Each entry is a lowercase string; a label is excluded if it starts with
# (i.e. has the prefix of) any entry. This covers both exact-match labels
# (e.g. "invalid") and namespace labels (e.g. "clang:" matches
# "clang:frontend", "clang:codegen", etc.). Backend-related labels
# (backend:*, llvm:selectiondag, llvm:globalisel, llvm:regalloc,
# llvm:codegen) are intentionally NOT excluded — they are legitimate
# bug categories for this daemon.
SKIP_LABEL_PREFIXES = frozenset(
    {
        # Non-bug categories (carried over from old SKIP_LABELS)
        "question",
        "feature request",
        "feature-request",
        "documentation",
        "duplicate",
        "invalid",
        "wontfix",
        # Build/infra
        "build-problem",
        "infra:",
        # Clang diagnostics / tooling (not LLVM core bugs)
        "clang:diagnostics",
        "clang-tidy",
        "clang-format",
        "clang:as-a-library",
        "clangd",
        "clang-tools-extra",
        "check-request",
        # Non-LLVM subprojects
        "hlsl",
        "mlir",
        "flang",

        "lldb",
        "libc++",
        "polly",
        "tablegen",
        "bolt",
        "mc",
        "pgo",
        "tools:",
        # Non-bug / feature request labels
        "concepts",
        "bugzilla",
        "libc",
        "packaging",
        # Other excluded categories
        "undefined behavior",
        "llvm-reduce",
        "coroutines",
    }
)

# Exact labels that are NEVER skipped, even if their prefix matches
# SKIP_LABEL_PREFIXES. Currently protects tools:llc and tools:opt from
# the blanket tools: exclusion — these are legitimate LLVM backend bugs.
SKIP_LABEL_ALLOW = frozenset(
    {
        "tools:llc",
        "tools:opt",
    }
)
