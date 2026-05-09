---
name: llvm-crash-reduce
description: Reduce LLVM crash reproducers — opt-bisect-limit to find pass, then llvm-reduce IR
---

## Tools
All LLVM tools are on PATH: `opt`, `llc`, `lli`, `llvm-reduce`, `clang`, `alive-tv`, `llubi_legacy`.

## Crash Reduction Pipeline

**CRITICAL: Reduction operates exclusively on LLVM IR. Never compile IR to native binaries for verification.**

### 1. Reproduce the crash
Run the crash command from the issue. Extract a literal crash signature substring (e.g., "Assertion `X && Y") — plain text, not regex.

### 2. Build opt pipeline
If the issue gives a specific pass, use it directly. Otherwise try `-passes='default<O2>'` or `-passes='default<O3>'`.

### 3. opt-bisect-limit binary search to find single pass

**Goal: identify the single pass that triggers the crash.**

First, get the total pass count:
```
opt -opt-bisect-limit=-1 -passes='<pipeline>' repro.ll -S -o /dev/null 2>&1   → total=N
```
Binary search lo=1, hi=N:
```
opt -opt-bisect-limit=M -passes='<pipeline>' repro.ll 2>&1
  crash → hi=M
  ok    → lo=M+1
```
Converge to M (the first pass that triggers the crash).

### 4. Extract single pass name and capture IR before it

Extract the pass name from the crash output (e.g., "Assertion failed at LICM.cpp" → pass is "licm") or from the bisect log (opt prints the last pass run before the crash).

Capture the IR just before the crashing pass:
```
opt -opt-bisect-limit=M-1 -passes='<pipeline>' repro.ll -S > before.ll
```

### 5. Handle llc crashes
If crash is in llc (not opt), skip bisect and go directly to llvm-reduce with an interestingness script that runs llc and checks for the crash signature.
**Crash in lli is not supported** — the daemon will reject result.json with `tool: "lli"` for crash type.

### 6. llvm-reduce with ONLY the single pass

**CRITICAL: The interestingness script must use only the single pass (`-passes=<pass_name>`), NOT the full pipeline.**

```bash
cat > interestingness.sh <<'EOF'
#!/bin/bash
set -e
timeout 30 opt -passes='<pass_name>' "$1" 2>&1 | grep -qF "<signature>"
EOF
chmod +x interestingness.sh
llvm-reduce --test=interestingness.sh before.ll
```
Output: `reduced.ll`

### 7. Verify
Run the single pass on `reduced.ll`, confirm crash signature still matches.

### 8. Write results

**result.json:**
```json
{
  "type": "crash",
  "tool": "opt",
  "args": "-passes=licm",
  "ir_file": "reduced.ll"
}
```
The `args` for opt MUST be the single pass (e.g. `-passes=licm`), not a full pipeline like `-passes=default<O2>`.

**result.json for llc:**
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
