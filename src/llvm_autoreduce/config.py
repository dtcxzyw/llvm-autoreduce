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
# Extractor needs more time — it compiles C sources and runs toolchain
# commands to reproduce the bug before classification.
EXTRACT_TIMEOUT = 600
# Single opencode reduction run typically completes in under 2 minutes
# (opt-bisect-limit binary search + llvm-reduce on already-small inputs).
# The generous ceiling accounts for API latency and verbose LLM reasoning.
REDUCE_TIMEOUT = 1500
VERIFY_TIMEOUT = 120
# ACCEPTED RISK (F24): DAEMON_INTERVAL and ISSUES_PER_ROUND are hardcoded.
# No environment variable override is provided for tuning poll frequency
# or batch size. For development or testing, modify these constants directly.
DAEMON_INTERVAL = 1800

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
        # Clang tooling (not LLVM core bugs)
        # NOTE: "clang:" prefix is intentionally excluded — Clang IRGen
        # bugs (clang:codegen) produce miscompiled LLVM IR and are
        # legitimate targets for the daemon.
        "clang-tidy",
        "clang-format",
        "clangd",
        "check-request",
        # Non-LLVM subprojects
        "mlir",
        "flang",
        "lld:",
        "lldb",
        "libc++",
        "polly",
        "tablegen",
        "bolt",
        "mc",
        "pgo",
        "tools:",
        # Other excluded categories
        "undefined behavior",
        "llvm-reduce",
        "coroutines",
    }
)
