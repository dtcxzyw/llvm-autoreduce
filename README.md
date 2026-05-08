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

```bash
autoreduce-daemon
```

The daemon polls for new issues every 30 minutes, processes up to 20 issues per round, and writes logs to `work/daemon.log`.

## Development

```bash
uv run ruff check .        # lint
uv run pytest tests/       # test
```

See [AGENTS.md](AGENTS.md) for contributor guidelines and design decisions.
