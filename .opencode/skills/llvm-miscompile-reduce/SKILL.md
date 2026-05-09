---
name: llvm-miscompile-reduce
description: Reduce LLVM miscompilation reproducers — LLUBI/Alive2 oracle + opt-bisect-limit + llvm-reduce
---

## Tools
All LLVM tools are on PATH: `opt`, `llc`, `lli`, `llvm-reduce`, `clang`, `alive-tv`, `llubi_legacy`.

## Miscompilation Reduction Pipeline

**CRITICAL: Reduction operates exclusively on LLVM IR. Never compile IR to native binaries for verification — use the oracle tools (llubi_legacy, alive-tv, lli) directly on IR.**

### 1. Reproduce with oracle

First, confirm the miscompilation is reproducible with the oracle on the pipeline.

**llubi_legacy:**
```
llubi_legacy --max-steps 1000000 repro.ll > ref_ubi
opt -passes='<pipeline>' repro.ll -S | llubi_legacy --max-steps 1000000 - > test_ubi
diff ref_ubi test_ubi
```
If no diff: not reproducible with llubi, try alive-tv. If diff: proceed to bisect.

**alive-tv:**
```
opt -passes='<pipeline>' repro.ll -S > step.ll
alive-tv --disable-undef-input --smt-to=10000 repro.ll step.ll
```
Check the output:
- Both "0 incorrect transformations" **and** "Transformation seems to be correct!" present → transformation is correct (not a bug)
- "incorrect transformation" count > 0 or "ERROR: Value mismatch" → transformation is incorrect (confirmed miscompilation)
- "Alive2 approximated the semantics of the programs" → inconclusive (approximation, not a confirmed bug)

If no miscompilation detected: not reproducible with alive-tv, try llubi_legacy.

**lli (backend miscompilation):**
Use `lli` only when the bug is in backend codegen/instruction selection. llubi_legacy provides the reference semantics, lli runs through the JIT backend — differing outputs indicate a backend miscompilation.

**CRITICAL — lli preprocessing:** Before using the `lli` oracle, preprocess the IR to remove `main()` argument dependencies. If `main()` uses `argc`/`argv`, strip those references from the IR (e.g., replace `argc` with a constant, or remove the argument-using code path). Without this preprocessing, `llubi_legacy` and `lli` may produce different output even on a correct backend because `llubi_legacy` does not pass command-line arguments.

```
llubi_legacy --max-steps 1000000 repro.ll > ref_ubi
opt -passes='<pipeline>' repro.ll -S | lli - > test_out
diff ref_ubi test_out
```
If no diff: not reproducible. If diff: proceed to bisect.

### 2. Choose oracle

Use the **first** oracle that confirmed the miscompilation in step 1. Default: `llubi_legacy`. The reducer agent chooses the oracle — the daemon does not second-guess this choice.

### 3. opt-bisect-limit binary search to find single pass

**Goal: identify the single pass that introduces the miscompilation, not just the point in a pipeline.**

First, get the total pass count:
```
opt -opt-bisect-limit=-1 -passes='<pipeline>' repro.ll -S -o /dev/null 2>&1   → total=N
```

**llubi_legacy oracle — bisect:**
```
llubi_legacy --max-steps 1000000 repro.ll > ref_ubi
```
Binary search lo=1, hi=N:
```
opt -opt-bisect-limit=M -passes='<pipeline>' repro.ll -S | llubi_legacy --max-steps 1000000 - > test_ubi
diff ref_ubi test_ubi
  same    → lo=M+1  (the miscompilation has not happened yet)
  diff    → hi=M    (the miscompilation has occurred)
```
Converge to M (the first pass that introduces the miscompilation).

**alive-tv oracle — bisect:**
Binary search lo=1, hi=N:
```
opt -opt-bisect-limit=M -passes='<pipeline>' repro.ll -S > step.ll
alive-tv --disable-undef-input --smt-to=10000 repro.ll step.ll
```
Check the output:
- Both "0 incorrect transformations" **and** "Transformation seems to be correct!" present → lo=M+1 (correct)
- "incorrect transformation" count > 0 or "ERROR: Value mismatch" → hi=M (miscompilation found)
- "Alive2 approximated" or any other output → inconclusive; treat as lo=M+1 but note the approximation

**lli oracle — bisect:**
```
llubi_legacy --max-steps 1000000 repro.ll > ref_ubi
```
Binary search lo=1, hi=N:
```
opt -opt-bisect-limit=M -passes='<pipeline>' repro.ll -S | lli - > test_out
diff ref_ubi test_out
  same    → lo=M+1
  diff    → hi=M
```
Converge to M.

### 4. Extract the single pass name and capture IR before it

Extract the pass name from the bisect convergence. The pass that triggers the bug is pass M in the pipeline. Determine its name (e.g., "gvn", "licm", "instcombine") from the opt output or the pipeline description.

Capture the IR just before the bad pass:
```
opt -opt-bisect-limit=M-1 -passes='<pipeline>' repro.ll -S > before.ll
```

### 5. llvm-reduce with ONLY the single pass

**CRITICAL: The interestingness script must use only the single pass (`-passes=<pass_name>`), NOT the full pipeline.**

**llubi_legacy oracle:**
```bash
cat > interestingness.sh <<'SCRIPT'
#!/bin/bash
set -e
timeout 120 llubi_legacy --max-steps 1000000 repro.ll > ref_ubi.txt
timeout 30 opt -passes='<pass_name>' "$1" -S > __tmp.ll
timeout 120 llubi_legacy --max-steps 1000000 __tmp.ll > test_ubi.txt
! diff -q ref_ubi.txt test_ubi.txt
SCRIPT
```

**alive-tv oracle:**
```bash
cat > interestingness.sh <<'SCRIPT'
#!/bin/bash
set -e
timeout 30 opt -passes='<pass_name>' "$1" -S > __opt.ll
alive-tv --disable-undef-input --smt-to=10000 "$1" __opt.ll 2>&1 | grep -qE '[1-9][0-9]* incorrect transformation|ERROR: Value mismatch'
SCRIPT
```
Note: `grep -qE` returns 0 (interesting=true) only when there is at least one incorrect transformation or a value mismatch. "0 incorrect transformations", "Transformation seems to be correct!", and "Alive2 approximated" all return 1 (not interesting).

**lli oracle:**
```bash
cat > interestingness.sh <<'SCRIPT'
#!/bin/bash
set -e
timeout 120 llubi_legacy --max-steps 1000000 repro.ll > ref_ubi.txt
timeout 30 opt -passes='<pass_name>' "$1" -S > __tmp.ll
timeout 30 lli __tmp.ll > test_out.txt
! diff -q ref_ubi.txt test_out.txt
SCRIPT
```

Then:
```
chmod +x interestingness.sh
llvm-reduce --test=interestingness.sh before.ll
```
Output: `reduced.ll`

### 6. Verify and write results

Verify the reduced IR still reproduces the miscompilation with the single pass.

**result.json (llubi/alive2):**
```json
{
  "type": "miscompilation",
  "tool": "opt",
  "args": "-passes=gvn",
  "pass_name": "gvn",
  "ir_file": "reduced.ll",
  "reference_file": "repro.ll",
  "oracle": "llubi",
  "llubi_args": "--max-steps 1000000",
  "alive2_args": "--smt-to=10000"
}
```
The `args` field MUST be the single pass (e.g. `-passes=gvn`), not a full pipeline.

**result.json (lli — backend miscompilation):**
```json
{
  "type": "miscompilation",
  "tool": "opt",
  "args": "-passes=gvn",
  "pass_name": "gvn",
  "ir_file": "reduced.ll",
  "reference_file": "repro.ll",
  "oracle": "lli",
  "llubi_args": "--max-steps 1000000",
  "lli_args": ""
}
```

## Error handling
- Oracle crash on original IR: try the other oracle
- If bisect cannot isolate a single pass: report the smallest pipeline possible in `args`
- If all reduction attempts fail, write `result.json` with the FULL schema plus an `error` field describing the reason. The daemon requires all schema fields to be present — a bare `{"error": "..."}` will fail validation. Use:
```json
{
  "type": "miscompilation",
  "tool": "opt",
  "args": "",
  "ir_file": "error.ll",
  "reference_file": "repro.ll",
  "oracle": "llubi",
  "llubi_args": "--max-steps 1000000",
  "alive2_args": "",
  "error": "brief description of what failed"
}
```
- Do NOT generate a report.md file — the daemon handles report generation
- CRITICAL: All files stay in current working directory, never /tmp, /home, /etc, /var, or any other system path
