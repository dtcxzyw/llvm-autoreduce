# AGENTS

## Project Overview

`llvm-autoreduce` is an automated LLVM bug reproducer reduction tool. It focuses on reducing crash and miscompilation reproducers in LLVM's middle-end and back-end. See `README.md` for the user-facing overview.

## Development Environment

- **Python dependency management** — use `uv` (see `uv.lock`). Run `uv sync` to install all dependencies (including dev). Run `uv pip install -e .` to install the package itself in editable mode. The virtualenv lives at `.venv/`.
- **Testing** — `uv run pytest tests/` or `.venv/bin/python -m pytest tests/`. Test config is in `pyproject.toml` under `[tool.pytest.ini_options]`.
- **Linting** — `uv run ruff check .`
- **Toolchain build** — The LLVM, Alive2, and LLUBI toolchains are built by `scripts/update-tools.sh` (invoked automatically by the daemon on each round). Requires an LLVM installation with development headers on the host for bootstrapping.

## Repository Language Rules

- Write all repository content in English and reply to the user in the user's language.

## User Interaction Rules

- If requirements or plans are ambiguous, eliminate disagreement instead of guessing: first explore the codebase when that can answer the question, otherwise ask clarifying questions aggressively, preferably one at a time.
- Clarifying questions must drive toward shared understanding by walking the design tree and resolving decision dependencies; each question must cover purpose, constraints, success criteria, or scope boundaries as appropriate, include all viable options, avoid requiring free-form input, and state the recommended answer.
- After the user approves execution, keep going until the full planned task is complete. Do not stop for intermediate progress reports, return control while the approved goal is only partially implemented, or ask whether to continue with obvious next steps, natural follow-ups, or clear previews, summaries, or refinements unless a genuine blocker, real ambiguity, or material risk requires user input.
- If something should obviously be done now, do it instead of deferring it to "later", "next", or a "follow-up". When reviewing or refining in-progress work, immediately implement any concrete fix you understand and that is not blocked.
- Resolve encountered difficulties autonomously whenever possible.
- When handing control back to the user after task completion, end the response with `Done.`

## Design and Planning Rules

- Before proposing a design or implementation, inspect the current project context, including relevant files, documentation, and recent changes.
- State assumptions explicitly. If materially different interpretations remain after exploration, surface and resolve them instead of silently choosing one.
- For feature work, behavior changes, and other creative or architectural tasks, complete a design step before implementation. If the request is too broad for one coherent spec or plan, decompose it into smaller subprojects and handle them one at a time.
- After gathering enough context, present 2-3 viable approaches with trade-offs and a recommended option, size the design sections to the topic, and validate the design with the user before implementation.
- Before substantial implementation, define concrete success criteria and explicit checks that prove completion; prefer tests, builds, or other verifiable validation over vague goals.
- Unless the user explicitly asks for a temporary workaround, limited experiment, reduced-scope language design, or backwards compatibility, design and implement the final intended product: complete behavior, durable interfaces, maintainable structure, the full language design rather than a staged or minimal subset, and clean breaking changes instead of compatibility shims during rapid iteration.
- If a requested feature depends on a lower-level prerequisite, implement that prerequisite as part of the work instead of lowering the acceptance bar, trimming scope, or presenting a partial workaround as complete.
- Do not limit the solution to the smallest easy increment or patch size when the correct solution requires broader structural changes. Make decisive, coherent refactors, including renaming APIs, reshaping module boundaries, or replacing flawed internal structures when needed.
- Design systems as small, well-bounded units with clear responsibilities, explicit interfaces, and dependencies that are easy to understand and test independently.
- In existing codebases, follow established patterns unless changing them is necessary to support the current goal.
- Apply YAGNI strictly. Make focused changes, avoid unrelated reformatting or cleanup, and remove only unrequested features, speculative abstractions, unnecessary complexity, or artifacts made unused by your own change unless broader cleanup was requested.

## Git Commit Rules

- Preserve a linear history. Do not force push except when rebasing a PR branch onto the latest `origin/main` and force-pushing that same PR branch update.
- Do not amend commits that have already been pushed, and do not rewrite, replace, or reorder existing commits unless the user explicitly requests it.
- Before starting work, ensure the worktree is clean; if prior changes are present, review them and commit the relevant ones before beginning new task work.
- Commit changes automatically after each completed milestone without waiting for the user to ask. After finishing a logical unit of work that passes tests and pre-commit checks, immediately stage and commit with a proper Conventional Commit message, then ensure the worktree is clean.
- Never commit temporary files, scratch artifacts, ad hoc notes, or other non-durable byproducts.
- Use specific Conventional Commits for every commit. When a commit is non-trivial, include a body describing what changed, why it changed, and what validation was performed; make the subject identify the precise logical unit and the body record the concrete scope of the change.
- Split commits by logical unit so they stay focused and reviewable.
- Ensure relevant tests pass before creating a commit, assume pre-commit hooks will run, and do not bypass them with `--no-verify`.
- **CRITICAL — ACCEPTED RISK:** When auditing code or reporting findings, you MUST NOT report issues that are already explicitly annotated with `ACCEPTED RISK` in source code comments. These are known, deliberate trade-offs. Re-reporting them is noise and wastes review time. Before including any finding in an audit, verify it is NOT tagged `ACCEPTED RISK` in the relevant source file.
- **AUDIT SCOPE — TOOLCHAIN STATE:** Do not audit for crash-consistency of toolchain state files (e.g., `.known-good`), partial-build recovery, or atomicity of multi-component state updates in `scripts/update-tools.sh`. The build script handles rollbacks and the daemon's health check detects unusable toolchains.
- **AUDIT SCOPE — CONTAINER CONFIG:** Do not audit container/deployment configuration files (`Dockerfile`, `devcontainer.json`, `.devcontainer/`) for security. These are deployment artifacts managed separately from the application logic.
- **AUDIT SCOPE — FILESYSTEM INFRASTRUCTURE:** Do not audit for filesystem-level failures (stale NFS mounts, kernel hangs during `shutil.rmtree`, disk-full race conditions during writes). These are operating environment concerns, not application logic defects.
- **AUDIT SCOPE — HOST CRASH / UNCLEAN SHUTDOWN:** Do not audit for data loss or duplicate processing caused by host machine crash, SIGKILL, kernel panic, power loss, or any other form of unclean shutdown. The daemon's durability guarantees assume a clean shutdown path. Audit findings about missing `fsync`, write-ahead logging, or crash-consistency of non-build state files (e.g. `processed.txt`) are excluded — these are operating environment concerns, not application logic defects.
- **AUDIT SCOPE — TEST COVERAGE:** Do not treat missing test coverage for functions that require substantial mocking (subprocess invocation, external HTTP APIs, LLVM toolchain binaries) as audit findings. Integration/e2e tests for the full pipeline are valued but not mandatory — the daemon's verification step and ACCEPTED RISK annotations cover the critical failure modes. Unit-testable pure logic (validation, extraction, workdir) should remain tested.
- Do not consider multi-daemon scenarios, race conditions, or parallel execution in design or audit work. The daemon is designed to run as a single instance. Do not raise findings that only apply to concurrent or multi-threaded execution — the daemon processes issues sequentially in a single thread.
- **AUDIT SCOPE — AGENT OUTPUT TRUST:** Do not audit for missing defensive validation of AI agent output fields (size checks, format sanitization, content bounds). The daemon trusts agent-produced JSON as authoritative — the prompts and agent configs are the contract. Adding code-level guards against malformed-but-valid agent output is unnecessary defense-in-depth and falls outside the threat model.
- **AUDIT SCOPE — TRANSIENT PER-ISSUE FAILURES:** Do not audit for transient exceptions that affect a single issue. The per-issue exception handler logs and continues; the issue may be retried in a future round. Only raise findings for bugs that crash the entire daemon process or cause systemic failure (e.g., every issue or round fails deterministically).
- **AUDIT SCOPE — PER-ISSUE mark_processed PATTERN:** `reprocess_issue` marks issues permanently processed (`mark_processed()`) on every internal failure path by design. Only GitHub API submission errors and the outer per-issue exception handler skip `mark_processed` to allow a future-round retry. This is the FINAL design decision — do NOT re-audit this pattern. The distinction between "infrastructure error" and "content error" is intentionally not made inside `reprocess_issue`; the daemon treats every pipeline-stage failure as a terminal outcome for that issue. Do not raise findings about transient infrastructure outages causing mass issue loss — they sort out on the next poll cycle. This is explicitly annotated as ACCEPTED RISK F32 in `daemon.py`.
- **AUDIT SCOPE — TRIVIAL / FALSE-POSITIVE FINDINGS:** Do not report findings that are cosmetic, stylistic, or semantically equivalent to existing behavior. Do not report issues that are already handled by test coverage, existing ACCEPTED RISK annotations, or any other AUDIT SCOPE exclusion above. Before reporting any finding, exhaustively verify it is NOT covered by any exclusion and NOT a trivial/negligible concern. If a finding can be described as "this is technically X but doesn't actually cause problems," do NOT report it.
- **AUDIT SCOPE — HTTP RETRY GRANULARITY:** Do not audit `_request()` for retrying specific 5xx status codes that are permanent errors (501, 505). GitHub's API does not return these in practice; the current blind-retry-on-all-5xx behavior is accepted. The retry delays total ~14 seconds across 3 attempts, which is negligible in a 30-minute poll cycle.
- **AUDIT SCOPE — NON-DETERMINISTIC GODBOLT LINK SELECTION:** Do not audit the non-deterministic ordering of Godbolt links when >3 are present (`set(list(links)[:3])` in `_fetch_godbolt`). Most issues contain ≤2 Godbolt links, and an issue is processed exactly once, so link-ordering variation across runs has zero practical impact.
