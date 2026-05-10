---
name: llvm-miscompile-reduce
description: Reduce LLVM miscompilation reproducers — LLUBI/Alive2 oracle + opt-bisect-limit + llvm-reduce
---

## Tools
All LLVM tools are on PATH: `opt`, `llc`, `lli`, `llvm-reduce`, `clang`, `alive-tv`, `llubi_legacy`, `llvm-extract`.

**Timeout rule: wrap every standalone `opt`, `llc`, `lli`, or `clang` command with `timeout 60`.** llubi_legacy `--reduce-mode --max-steps 1000000` is sufficient. interestingness.sh commands already carry timeouts — no extra wrapping needed there.

## Miscompilation Reduction Pipeline

**CRITICAL: Reduction operates exclusively on LLVM IR. Never compile IR to native binaries for verification — use the oracle tools (llubi_legacy, alive-tv, lli) directly on IR.**

### 0. Read metadata from extract.json
Read `extract.json` and note:
- `pipeline` — the pipeline string from the issue (e.g. `-passes='default<O2>'`).
- `reproducer_file` — the `.ll` file to reduce.

Create a symlink for convenience so all scripts can use `repro.ll`:
```
ln -sf <reproducer_file> repro.ll
```

### 1. Reproduce with oracle

First, confirm the miscompilation is reproducible. **Always try alive-tv first for middle-end bugs. Fall back to llubi_legacy if alive-tv is inconclusive.**

**alive-tv (preferred for middle-end, function pass bugs):**
```
opt -passes='<pipeline>' repro.ll -S > step.ll
alive-tv --disable-undef-input --smt-to=10000 repro.ll step.ll
```
Check the output:
- "incorrect transformation" count > 0 or "ERROR: Value mismatch" → transformation is incorrect (confirmed miscompilation)
- Both "0 incorrect transformations" **and** "Transformation seems to be correct!" present → correct (not a bug)
- "Alive2 approximated the semantics of the programs" → **NOT a miscompilation** (approximation, skip alive2 for this issue)
- Error about unsupported intrinsic/function → **NOT a valid alive2 match** (skip alive2)

**llubi_legacy (fallback):**
```
set -o pipefail
timeout 60 llubi_legacy --reduce-mode --max-steps 1000000 repro.ll > ref_ubi
! opt -passes='<pipeline>' repro.ll -S | llubi_legacy --reduce-mode --max-steps 1000000 - | diff -q ref_ubi -
```
**ACCEPTED RISK:** Crashes in the pipeline (opt or llubi_legacy segfault) are treated as miscompilation: `pipefail` makes the pipeline exit non-zero on crash, `!` inverts that to exit 0 ("miscompilation found"). This affects both the reproduction check and the bisect step — a buggy pass that crashes will be incorrectly selected as the miscompilation trigger. The daemon's final `verify()` step independently checks the reduced IR and will reject cases where the miscompilation does not actually reproduce, so a crash-confused reduction is caught at verification time.
If no diff: not reproducible. If diff: proceed to bisect.

**lli (backend miscompilation):**
Use `lli` only when the bug is in backend codegen/instruction selection. llubi_legacy provides the reference semantics, lli runs through the JIT backend — differing outputs indicate a backend miscompilation.

**CRITICAL — lli preprocessing:** Before using the `lli` oracle, preprocess the IR to remove `main()` argument dependencies. If `main()` uses `argc`/`argv`, strip those references from the IR (e.g., replace `argc` with a constant, or remove the argument-using code path). Without this preprocessing, `llubi_legacy` and `lli` may produce different output even on a correct backend because `llubi_legacy` does not pass command-line arguments.

```
set -o pipefail
timeout 60 llubi_legacy --reduce-mode --max-steps 1000000 repro.ll > ref_ubi
! opt -passes='<pipeline>' repro.ll -S | lli - | diff -q ref_ubi -
```
**ACCEPTED RISK:** Crash → miscompilation. Same `!` + `pipefail` inversion as llubi reproduction.
If no diff: not reproducible. If diff: proceed to bisect.

### 2. Choose oracle

**Preference order for middle-end:** alive-tv > llubi_legacy.
Use the **first** oracle that confirmed the miscompilation in step 1. Default for middle-end: alive-tv. The reducer agent chooses the oracle — the daemon does not second-guess this choice.

### 3. opt-bisect-limit binary search to find single pass

**Goal: identify the single pass that introduces the miscompilation, not just the point in a pipeline.**

First, get the total pass count:
```
timeout 60 opt -opt-bisect-limit=-1 -passes='<pipeline>' repro.ll -S -o /dev/null 2>&1   → total=N
```

**IMPORTANT: Use `set -o pipefail` for all bisect comparisons.** Without pipefail, if the oracle crashes (e.g., llubi_legacy segfaults), the empty output will be mistaken for a miscompilation. Note: even with pipefail, a crash is still treated as miscompilation because `!` inverts the non-zero pipeline exit to 0 (see ACCEPTED RISK annotations on the crash rows below).

Pre-compute the reference output once (same for all bisect iterations):
```
timeout 60 llubi_legacy --reduce-mode --max-steps 1000000 repro.ll > ref_ubi
```

**llubi_legacy oracle — bisect:**
Binary search lo=1, hi=N:
```
set -o pipefail
! opt -opt-bisect-limit=M -passes='<pipeline>' repro.ll -S | llubi_legacy --reduce-mode --max-steps 1000000 - | diff -q ref_ubi -
  same    → lo=M+1  (exit 1: not miscompiled)
  diff    → hi=M    (exit 0: miscompilation found)
  crash   → hi=M    (ACCEPTED RISK: pipefail non-zero exit is inverted by ! to exit 0 — crash is treated as miscompilation)
```
Converge to M (the first pass that introduces the miscompilation).

**alive-tv oracle — bisect:**
Binary search lo=1, hi=N:
```
timeout 60 opt -opt-bisect-limit=M -passes='<pipeline>' repro.ll -S > step.ll
alive-tv --disable-undef-input --smt-to=10000 repro.ll step.ll
```
Check the output:
- Both "0 incorrect transformations" **and** "Transformation seems to be correct!" present → lo=M+1 (correct)
- "incorrect transformation" count > 0 or "ERROR: Value mismatch" → hi=M (miscompilation found)
- "Alive2 approximated" or unsupported intrinsic/function → inconclusive; treat as lo=M+1

**lli oracle — bisect:**
Pre-compute the reference output once:
```
timeout 60 llubi_legacy --reduce-mode --max-steps 1000000 repro.ll > ref_ubi
```
Binary search lo=1, hi=N:
```
set -o pipefail
! opt -opt-bisect-limit=M -passes='<pipeline>' repro.ll -S | lli - | diff -q ref_ubi -
  same    → lo=M+1
  diff    → hi=M
  crash   → hi=M    (ACCEPTED RISK: crash treated as miscompilation — same ! inversion as llubi bisect)
```
Converge to M.

### 4. Extract the single pass name and capture IR before it

The bisect log prints the last pass run before the miscompilation (e.g. `BISECT: running pass (N) GVN on ...`). Convert this to the `-passes=` form (e.g. `-passes=gvn`). Do NOT guess from filenames.

Capture the IR just before the bad pass:
```
opt -opt-bisect-limit=M-1 -passes='<pipeline>' repro.ll -S > before.ll
```

### 5. Check pass type and extract single function (alive2 oracle)

Determine if the buggy pass is a function pass. If YES, extract a single function for alive2 verification:

```
llvm-extract -func=<function_name> before.ll -S -o single_func.ll
```

Test that the bug still reproduces on the single function with alive-tv. If it does, use `single_func.ll` as the reduction input for alive2 oracle. If not, use `before.ll` and llubi oracle.

**Only use alive2 oracle for function pass bugs.** For module pass bugs (e.g. inliner, IPSCCP, globalopt), use llubi.

### 6. llvm-reduce with ONLY the single pass

**CRITICAL: The interestingness script must use only the single pass (`-passes=<pass_name>`), NOT the full pipeline.**

**alive-tv oracle (function pass bugs):**
```bash
cat > interestingness.sh <<'SCRIPT'
#!/bin/bash
set -eo pipefail
timeout 30 opt -passes='<pass_name>' "$1" -S > __opt.ll
timeout 120 alive-tv --disable-undef-input --smt-to=10000 "$1" __opt.ll 2>&1 | grep -qE '[1-9][0-9]* incorrect transformation|ERROR: Value mismatch'
SCRIPT
```
Note: `grep -qE` returns 0 (interesting=true) only when there is at least one incorrect transformation or a value mismatch. "0 incorrect transformations", "Transformation seems to be correct!", "Alive2 approximated the semantics", and unsupported intrinsic errors all return 1 (not interesting).

**llubi_legacy oracle:**
```bash
cat > interestingness.sh <<'SCRIPT'
#!/bin/bash
set -eo pipefail
timeout 120 llubi_legacy --reduce-mode --max-steps 1000000 "$1" > _ref.txt
timeout 30 opt -passes='<pass_name>' "$1" -S | timeout 120 llubi_legacy --reduce-mode --max-steps 1000000 - | ! diff -q _ref.txt -
SCRIPT
```
**ACCEPTED RISK:** Crash → interesting. `!` inverts the pipeline exit: if opt or llubi_legacy crashes, the `pipefail` pipeline exits non-zero, `!` flips it to 0 (interesting). The daemon's final verify step independently confirms the miscompilation and rejects crash-confused reductions.

**lli oracle:**
```bash
cat > interestingness.sh <<'SCRIPT'
#!/bin/bash
set -eo pipefail
timeout 120 llubi_legacy --reduce-mode --max-steps 1000000 "$1" > _ref.txt
timeout 30 opt -passes='<pass_name>' "$1" -S | timeout 30 lli - | ! diff -q _ref.txt -
SCRIPT
```
**ACCEPTED RISK:** Crash → interesting. Same `!` + `pipefail` inversion as the llubi interestingness script.

Then:
```
chmod +x interestingness.sh
llvm-reduce --test=interestingness.sh <input>.ll
```
Output: `reduced.ll`

If llvm-reduce gets stuck on a specific delta pass (check its progress output for a pass that keeps running without making progress), kill it and retry with `--skip-delta-passes=<pass_name>` (e.g. `--skip-delta-passes=instructions`). Repeat if it gets stuck on another pass.

### 7. Additional manual reduction (alive2 oracle)

After llvm-reduce, try these techniques on `reduced.ll` to shrink it further. Test after each change that the miscompilation still reproduces with alive-tv.

**Reduce bitwidth:** Replace `i64` with smaller integer types (`i32`, `i16`, `i8`) where possible, and `i32` with `i16` or `i8`. Adjust constants accordingly.

**Reduce pointer width:** In the target datalayout, change `p:64:64` to `p:32:32` (or lower). Update `target triple` to match (e.g. use a 32-bit target like `armv7-unknown-linux-gnueabihf`).

**Reduce loop trip count:** If the IR has a loop with a fixed trip count (e.g. `br i1 %cmp, label %loop, label %exit` where %cmp compares induction variable against a constant like 128), reduce the constant (e.g. 128 → 4). This shrinks the loop body that needs to be preserved.

**Loop transformations (alive2):** For bugs involving loop passes, use alive-tv's loop unrolling flags to help it reason about loops:
```
alive-tv --disable-undef-input --smt-to=10000 -src-unroll=4 -tgt-unroll=4 <src> <tgt>
```
This unrolls loops in both source and target up to N iterations, allowing alive2 to analyze loop transformations.

**NEVER use undef.** Do not introduce `undef` or `poison` values — alive2 handles them differently and they can mask real bugs. Use concrete values instead.

### 8. Verify and write results

Verify the reduced IR still reproduces the miscompilation with the single pass.

**result.json (alive2):**
```json
{
  "type": "miscompilation",
  "tool": "opt",
  "args": "-passes=gvn",
  "ir_file": "reduced.ll",
  "reference_file": "repro.ll",
  "oracle": "alive2",
  "llubi_args": "",
  "alive2_args": "--disable-undef-input --smt-to=10000"
}
```
The `args` field MUST be the single pass (e.g. `-passes=gvn`), not a full pipeline.

**result.json (llubi):**
```json
{
  "type": "miscompilation",
  "tool": "opt",
  "args": "-passes=gvn",
  "ir_file": "reduced.ll",
  "reference_file": "repro.ll",
  "oracle": "llubi",
  "llubi_args": "--reduce-mode --max-steps 1000000",
  "alive2_args": ""
}
```

**result.json (lli — backend miscompilation):**
```json
{
  "type": "miscompilation",
  "tool": "opt",
  "args": "-passes=gvn",
  "ir_file": "reduced.ll",
  "reference_file": "repro.ll",
  "oracle": "lli",
  "llubi_args": "--reduce-mode --max-steps 1000000",
  "lli_args": ""
}
```

## Error handling
- Oracle crash on original IR: try the other oracle
- If bisect cannot isolate a single pass: report the smallest pipeline possible in `args`
- If alive2 reports approximation or unsupported intrinsics: fall back to llubi
- If all reduction attempts fail, write `result.json` with the FULL schema plus an `error` field describing the reason. The daemon requires all schema fields to be present — a bare `{"error": "..."}` will fail validation. Use:
```json
{
  "type": "miscompilation",
  "tool": "opt",
  "args": "",
  "ir_file": "error.ll",
  "reference_file": "repro.ll",
  "oracle": "llubi",
  "llubi_args": "--reduce-mode --max-steps 1000000",
  "alive2_args": "",
  "error": "brief description of what failed"
}
```
- Do NOT generate a report.md file — the daemon handles report generation
- CRITICAL: All files stay in current working directory, never /tmp, /home, /etc, /var, or any other system path
