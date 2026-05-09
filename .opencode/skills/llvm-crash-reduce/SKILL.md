---
name: llvm-crash-reduce
description: Reduce LLVM crash reproducers — opt-bisect-limit to find pass, then llvm-reduce IR
---

## Tools
All LLVM tools are on PATH: `opt`, `llc`, `lli`, `llvm-reduce`, `clang`, `alive-tv`, `llubi_legacy`.

## Crash Reduction Pipeline

**CRITICAL: Reduction operates exclusively on LLVM IR. Never compile IR to native binaries for verification.**

### 1. Read metadata from extract.json
Read `extract.json` and note:
- `crash_pattern` — the literal crash signature substring (plain text, not regex). Use this directly; do not re-extract.
- `pipeline` — the pipeline string from the issue or `-passes='default<O2>'` as fallback.
- `reproducer_file` — the `.ll` file to reduce.

### 2. Reproduce the crash
Run the crash with the given pipeline to verify the reproducer still crashes and the crash_pattern matches:
```
opt -passes='<pipeline>' <reproducer_file> 2>&1 | grep -qF "<crash_pattern>"
```
For llc crashes: `llc <reproducer_file> 2>&1 | grep -qF "<crash_pattern>"`.

**Crash in lli is not supported.** If extract.json indicates a crash from lli (the crash output mentions `lli` or JIT), write result.json with an error field immediately. The daemon rejects `tool: "lli"` for crash type.

### 3. opt-bisect-limit binary search to find single pass (opt crashes only)

For llc crashes, skip to step 5.

**Goal: identify the single pass that triggers the crash.**

First, get the total pass count:
```
opt -opt-bisect-limit=-1 -passes='<pipeline>' <reproducer_file> -S -o /dev/null 2>&1   → total=N
```
Binary search lo=1, hi=N:
```
opt -opt-bisect-limit=M -passes='<pipeline>' <reproducer_file> 2>&1
  crash → hi=M
  ok    → lo=M+1
```
Converge to M (the first pass that triggers the crash).

### 4. Extract single pass name and capture IR before it

The bisect log prints the last pass run before the crash. Use that output to determine the exact pass name (e.g., `-passes=licm`). Do NOT guess the pass name from filenames in crash backtraces.

To see pass names explicitly:
```
opt -opt-bisect-limit=M -passes='<pipeline>' <reproducer_file> -S -o /dev/null 2>&1
```
The last line before the crash lists the failing pass by its registered name — use that name directly.

Capture the IR just before the crashing pass:
```
opt -opt-bisect-limit=M-1 -passes='<pipeline>' <reproducer_file> -S > before.ll
```

### 5. Handle llc crashes

If crash is in llc (not opt), skip bisect and go directly to llvm-reduce. The interestingness script runs llc and checks for the crash signature — no pass or pipeline is involved.

**Crash in lli is not supported** — the daemon will reject `result.json` with `tool: "lli"` for crash type. If the issue reports an lli crash, write result.json with an error field.

### 6. llvm-reduce with ONLY the single pass

**CRITICAL: The interestingness script must use only the single pass (`-passes=<pass_name>`), NOT the full pipeline.**

**For opt crashes:**
```bash
cat > interestingness.sh <<'EOF'
#!/bin/bash
set -e
timeout 30 opt -passes='<pass_name>' "$1" 2>&1 | grep -qF "<crash_pattern>"
EOF
chmod +x interestingness.sh
llvm-reduce --test=interestingness.sh before.ll
```

**For llc crashes (no bisect, no single pass):**
```bash
cat > interestingness.sh <<'EOF'
#!/bin/bash
set -e
timeout 30 llc "$1" 2>&1 | grep -qF "<crash_pattern>"
EOF
chmod +x interestingness.sh
llvm-reduce --test=interestingness.sh <reproducer_file>
```

Output: `reduced.ll`

### 7. Verify
Run the single pass (opt) or llc on `reduced.ll`, confirm crash signature still matches.

### 8. Write results

**result.json for opt crash:**
```json
{
  "type": "crash",
  "tool": "opt",
  "args": "-passes=licm",
  "ir_file": "reduced.ll"
}
```
The `args` for opt MUST be the single pass (e.g. `-passes=licm`), not a full pipeline like `-passes='default<O2>'`.

**result.json for llc crash:**
```json
{
  "type": "crash",
  "tool": "llc",
  "args": "",
  "ir_file": "reduced.ll"
}
```

## Error handling
- If any step fails and the crash cannot be reduced, write `result.json` with the FULL schema plus an `error` field describing the reason. The daemon requires all schema fields to be present — a bare `{"error": "..."}` will fail validation. Use:
```json
{
  "type": "crash",
  "tool": "opt",
  "args": "",
  "ir_file": "error.ll",
  "error": "brief description of what failed"
}
```
- Do NOT generate a report.md file — the daemon handles report generation
- CRITICAL: All files stay in current working directory, never /tmp, /home, /etc, /var, or any other system path
