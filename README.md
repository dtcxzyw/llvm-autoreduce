# llvm-autoreduce

Automated LLVM bug reproducer reduction tool. Watches the [llvm/llvm-project](https://github.com/llvm/llvm-project) issue tracker for crash and miscompilation reports, then automatically reduces them using `opt-bisect-limit` and `llvm-reduce`. Reduced results are submitted to [dtcxzyw/llvm-autoreduce](https://github.com/dtcxzyw/llvm-autoreduce).

## Pipeline

1. **Fetch** open issues from llvm/llvm-project
2. **Download** Godbolt sources, attachments, and issue body
3. **Security review** — AI agent screens reproducer content for malicious patterns
4. **Extract & classify** — bug type (crash / miscompilation), crash signature, and pipeline
5. **Reduce** — opt-bisect-limit binary search to find the crashing pass, then llvm-reduce to shrink IR
6. **Verify** the reduced IR still reproduces the bug
7. **Submit** result to the target repository

## Setup

Requires Python 3.12+, an LLVM source tree with `opt`/`llc`/`lli`/`llvm-reduce`/`clang` built, and optionally [Alive2](https://github.com/AliveToolkit/alive2) and [llubi](https://github.com/dtcxzyw/llvm-ub-aware-interpreter) for miscompilation verification.

```bash
uv sync
uv pip install -e .
```

### Required environment variables

| Variable | Description | Default |
|----------|-------------|---------|
| `AUTOREDUCE_TOKEN` | GitHub personal access token (required) | — |

### Toolchain

Run `scripts/update-tools.sh` to clone and build LLVM, Alive2, and LLUBI from source into `work/`.

## Usage

The daemon polls for new issues every 30 minutes, processes up to 20 issues per round, and writes logs to `work/daemon.log`.

### Supported bug types

| Bug type | Extract stage | Reduce stage |
|----------|--------------|-------------|
| Mid-end crash | oracle=`opt`, trigger crash with `opt <args> reproducer.ll`, extract literal substring from stderr as `crash_pattern` | tool=`opt`, bisect + llvm-reduce, verify crash pattern reproduces with `opt <args> reduced.ll` |
| Backend crash | oracle=`llc`, trigger crash with `llc <args> reproducer.ll`, extract literal substring from stderr as `crash_pattern` | tool=`llc`, llvm-reduce directly (no bisect), verify crash pattern reproduces with `llc <args> reduced.ll` |
| Mid-end miscompilation | oracle=`opt`, `llubi reproducer.ll` as reference (must exit 0), `opt <args> reproducer.ll \| llubi` as transformed — stdout differs **or transformed rc≠0/crash** confirms | 1. `alive2` — preferred, requires function pass + no TBAA/unsupported metadata<br>2. `llubi` — fallback, bisect to single pass → llvm-reduce → verify reference rc=0, transformed diff or rc≠0/crash |
| Backend miscompilation | oracle=`llc`, `llubi reproducer.ll` as reference (must exit 0), `lli reproducer.ll` as JIT output — stdout differs **or lli rc≠0/crash** confirms | oracle=`lli`, bisect to single pass → llvm-reduce → verify reference rc=0, lli diff or rc≠0/crash |

## Development



```bash
uv run ruff check .        # lint
uv run pytest tests/       # test
```

See [AGENTS.md](AGENTS.md) for contributor guidelines and design decisions.
