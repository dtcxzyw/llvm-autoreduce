---
name: llvm-crash-reduce
description: Reduce LLVM crash reproducers — opt-bisect-limit to find pass, then llvm-reduce IR
---

## Tools
All LLVM tools are on PATH: `opt`, `llc`, `lli`, `llvm-reduce`, `clang`, `alive-tv`, `llubi_legacy`.

**Timeout rule: wrap every standalone `opt`, `llc`, or `clang` command with `timeout 60`.** llubi_legacy `--reduce-mode --max-steps 1000000` is sufficient. interestingness.sh commands already carry timeouts — no extra wrapping needed there.

## Crash Reduction Pipeline

**CRITICAL: Reduction operates exclusively on LLVM IR. Never compile IR to native binaries for verification.**

### 1. Read metadata from extract.json
Read `extract.json` and note:
- `crash_pattern` — the literal crash signature substring (plain text, not regex). Use this directly; do not re-extract.
- `args` — the opt/llc arguments from the issue or `-passes='default<O2>'` as fallback.
- `reproducer_file` — the `.ll` file to reduce.

### 2. Reproduce the crash
Run the crash with the given pipeline to verify the reproducer still crashes and the crash_pattern matches:
```
opt -passes='<pipeline>' <reproducer_file> 2>&1 | grep -qF "<crash_pattern>"
```
For llc crashes: `llc <reproducer_file> 2>&1 | grep -qF "<crash_pattern>"`.

### 3. opt-bisect-limit to identify the crashing pass (opt crashes only)

For llc crashes, skip to step 5.

**Goal: identify the single pass that triggers the crash — one command, no binary search needed.**

`opt-bisect-limit=-1` runs all passes without stopping, but logs each pass number and name. The crash happens at the last pass printed in the log. The pass number M from the log is the crashing pass.

```
timeout 60 opt -opt-bisect-limit=-1 -passes='<pipeline>' reproducer.ll -S -o /dev/null 2>&1
```
Look for the last line matching `BISECT: running pass (M) <PassName> on ...` before the crash output — that M is the crashing pass number.

### 4. Extract the single pass name and capture IR before it

The bisect log prints the pass name (e.g. `BISECT: running pass (N) InstCombine on ...`). Convert to the `-passes=` form (e.g. `-passes=instcombine`). Do NOT guess from filenames in crash backtraces.

Capture the IR just before the crashing pass:
```
opt -opt-bisect-limit=M-1 -passes='<pipeline>' reproducer.ll -S > before.ll
```

### 5. Handle llc crashes

If crash is in llc (not opt), skip bisect and go directly to llvm-reduce. The interestingness script runs llc and checks for the crash signature — no pass or pipeline is involved.

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

If llvm-reduce gets stuck on a specific delta pass (check its progress output for a pass that keeps running without making progress), kill it and retry with `--skip-delta-passes=<pass_name>` (e.g. `--skip-delta-passes=instructions`). Repeat if it gets stuck on another pass.

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
