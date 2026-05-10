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
- `oracle` — `opt` for middle-end, `llc` for backend.
- `args` — the opt/llc arguments (e.g. `-passes='default<O2>'`).
- `reproducer_file` — the `.ll` file to reduce.

Create a symlink for convenience:
```
ln -sf <reproducer_file> repro.ll
```

### 1. Choose bisect/reduce oracle

Based on `extract.json` oracle:
- `oracle=opt` (middle-end) → use **llubi_legacy** for bisect and reduce
- `oracle=llc` (backend) → use **lli** for bisect and reduce (with llubi_legacy as reference)

**CRITICAL — lli preprocessing:** Before using the `lli` oracle, preprocess the IR to remove `main()` argument dependencies. If `main()` uses `argc`/`argv`, strip those references from the IR (e.g., replace `argc` with a constant). Without this, `llubi_legacy` and `lli` may produce different output even on a correct backend because `llubi_legacy` does not pass command-line arguments.

### 2. Reproduce the miscompilation

**Middle-end (llubi):**
```
set -o pipefail
timeout 60 llubi_legacy --reduce-mode --max-steps 1000000 repro.ll > ref_ubi
! opt -passes='<args>' repro.ll -S | llubi_legacy --reduce-mode --max-steps 1000000 - | diff -q ref_ubi -
```
**Backend (lli):**
```
set -o pipefail
timeout 60 llubi_legacy --reduce-mode --max-steps 1000000 repro.ll > ref_ubi
! opt -passes='<args>' repro.ll -S | lli - | diff -q ref_ubi -
```
**ACCEPTED RISK:** Crashes in the pipeline (opt, llubi_legacy, or lli segfault) are treated as miscompilation: `pipefail` makes the pipeline exit non-zero on crash, `!` inverts that to exit 0 ("miscompilation found"). The daemon's final `verify()` step independently checks the reduced IR and will reject cases where the miscompilation does not actually reproduce, so a crash-confused reduction is caught at verification time.

### 3. opt-bisect-limit binary search to find single pass

**Goal: identify the single pass that introduces the miscompilation.**

First, pre-compute the reference output and get total pass count:
```
timeout 60 llubi_legacy --reduce-mode --max-steps 1000000 repro.ll > ref_ubi
timeout 60 opt -opt-bisect-limit=-1 -passes='<args>' repro.ll -S -o /dev/null 2>&1   → total=N
```

**Middle-end (llubi oracle) — binary search lo=1, hi=N:**
```
while lo < hi:
    M = (lo + hi) / 2
    set -o pipefail
    ! opt -opt-bisect-limit=M -passes='<args>' repro.ll -S | llubi_legacy --reduce-mode --max-steps 1000000 - | diff -q ref_ubi -
      exit 0 (diff or crash) → miscompilation at or before M → hi=M
      exit 1 (same output)   → correct up to M               → lo=M+1
```

**Backend (lli oracle) — binary search lo=1, hi=N:**
```
while lo < hi:
    M = (lo + hi) / 2
    set -o pipefail
    ! opt -opt-bisect-limit=M -passes='<args>' repro.ll -S | lli - | diff -q ref_ubi -
      exit 0 (diff or crash) → miscompilation at or before M → hi=M
      exit 1 (same output)   → correct up to M               → lo=M+1
```
**ACCEPTED RISK:** Crash → miscompilation. `!` + `pipefail` inverts oracle/tool crashes to exit 0. The daemon's `verify()` step independently confirms.

**IMPORTANT:** `diff -q` only compares exit code (0=same, 1=differ), no content output. `pipefail` prevents oracle crashes from producing empty output that would be mistaken for "same". The reference output is computed once, not inside the loop.

### 4. Extract the single pass name and capture IR before it

The bisect log prints the last pass run before the miscompilation (e.g. `BISECT: running pass (N) GVN on ...`). Convert this to the `-passes=` form (e.g. `-passes=gvn`). Do NOT guess from filenames.

Capture the IR just before the bad pass:
```
opt -opt-bisect-limit=M-1 -passes='<args>' repro.ll -S > before.ll
```

### 5. llvm-reduce with ONLY the single pass

**CRITICAL: The interestingness script must use only the single pass, NOT the full pipeline.**

**llubi oracle (middle-end):**
```bash
cat > interestingness.sh <<'SCRIPT'
#!/bin/bash
set -eo pipefail
timeout 120 llubi_legacy --reduce-mode --max-steps 1000000 "$1" > _ref.txt
timeout 30 opt -passes='<pass_name>' "$1" -S | timeout 120 llubi_legacy --reduce-mode --max-steps 1000000 - | ! diff -q _ref.txt -
SCRIPT
```
**ACCEPTED RISK:** Crash → interesting. `!` inverts the pipeline exit: if opt or llubi_legacy crashes, the `pipefail` pipeline exits non-zero, `!` flips it to 0 (interesting). The daemon's final verify step independently confirms and rejects crash-confused reductions.

**lli oracle (backend):**
```bash
cat > interestingness.sh <<'SCRIPT'
#!/bin/bash
set -eo pipefail
timeout 120 llubi_legacy --reduce-mode --max-steps 1000000 "$1" > _ref.txt
timeout 30 opt -passes='<pass_name>' "$1" -S | timeout 30 lli - | ! diff -q _ref.txt -
SCRIPT
```
**ACCEPTED RISK:** Crash → interesting. Same `!` + `pipefail` inversion.

Then:
```
chmod +x interestingness.sh
llvm-reduce --test=interestingness.sh before.ll
```
Output: `reduced.ll`

If llvm-reduce gets stuck on a specific delta pass (check its progress output for a pass that keeps running without making progress), kill it and retry with `--skip-delta-passes=<pass_name>` (e.g. `--skip-delta-passes=instructions`). Repeat if it gets stuck on another pass.

### 6. Write checkpoint result (REQUIRED)

**CRITICAL: After llvm-reduce produces a working reduced.ll, write result.json IMMEDIATELY.** This saves a valid result before attempting optional oracle upgrades and manual reduction. The daemon accepts this as a completed reduction even if manual steps run out of time.

**Middle-end (llubi):**
```json
{
  "type": "miscompilation",
  "tool": "opt",
  "args": "-passes=<pass_name>",
  "ir_file": "reduced.ll",
  "reference_file": "repro.ll",
  "oracle": "llubi",
  "llubi_args": "--reduce-mode --max-steps 1000000",
  "alive2_args": ""
}
```

**Backend (lli):**
```json
{
  "type": "miscompilation",
  "tool": "opt",
  "args": "-passes=<pass_name>",
  "ir_file": "reduced.ll",
  "reference_file": "repro.ll",
  "oracle": "lli",
  "llubi_args": "--reduce-mode --max-steps 1000000",
  "lli_args": ""
}
```

### 7. Try alive2 upgrade (middle-end only, optional)

For middle-end bugs with a function pass, try upgrading the oracle from llubi to alive2. This produces a stronger result.

Determine if the buggy pass is a function pass. If YES, extract a single function:
```
llvm-extract -func=<function_name> before.ll -S -o single_func.ll
```

Test with alive-tv:
```
opt -passes='<pass_name>' single_func.ll -S > __opt.ll
alive-tv --disable-undef-input --smt-to=10000 single_func.ll __opt.ll
```

Check the output:
- "incorrect transformation" count > 0 or "ERROR: Value mismatch" → alive2 upgrade succeeded, update result.json with `oracle: "alive2"`, `alive2_args: "--disable-undef-input --smt-to=10000"`, `llubi_args: ""`.
- "0 incorrect transformations" + "Transformation seems to be correct!" → no bug visible to alive2, keep llubi.
- "Alive2 approximated the semantics" → **NOT a valid upgrade**, keep llubi.
- Unsupported intrinsic/metadata/function → **NOT a valid upgrade**, keep llubi.

**Only attempt alive2 for function pass bugs.** For module pass bugs (e.g. inliner, IPSCCP, globalopt), skip this step.

### 8. Additional manual reduction (optional — only if time permits)

After the checkpoint result.json, try these techniques to shrink `reduced.ll` further. Test after each change that the miscompilation still reproduces. If any succeeds, update result.json with the improved `ir_file`.

**Reduce bitwidth:** Replace `i64` with smaller integer types (`i32`, `i16`, `i8`) where possible. Adjust constants accordingly. Test that the miscompilation still reproduces.

**Reduce pointer width:** In the target datalayout, change `p:64:64` to `p:32:32` (or lower). Update `target triple` to match (e.g. use a 32-bit target like `armv7-unknown-linux-gnueabihf`).

**Reduce loop trip count:** If the IR has a loop with a fixed trip count (e.g. `br i1 %cmp, label %loop, label %exit` where %cmp compares induction variable against a constant like 128), reduce the constant (e.g. 128 → 4). This shrinks the loop body that needs to be preserved.

**Loop transformations (alive2):** For bugs involving loop passes, use alive-tv's loop unrolling flags to help it reason about loops:
```
alive-tv --disable-undef-input --smt-to=10000 -src-unroll=4 -tgt-unroll=4 <src> <tgt>
```
This unrolls loops in both source and target up to N iterations.

**NEVER use undef.** Do not introduce `undef` or `poison` values — alive2 handles them differently and they can mask real bugs. Use concrete values instead.

**Strip fast math flags.** If the IR contains `fast` or other fast-math flags on floating-point instructions, decompose `fast` into its constituent flags and keep only `nnan` and `ninf` — rewrite `fast` as `nnan ninf` explicitly. For any other fast-math flags (`nsz`, `arcp`, `contract`, `afn`, `reassoc`), remove them. If the miscompilation is specifically related to `nsz` (no-signed-zeros), prefer to drop `nsz` entirely rather than preserve it.

### 9. Verify final result

Verify the reduced IR still reproduces the miscompilation with the single pass. Write the final `result.json` (update from checkpoint if alive2 upgrade or manual reduction succeeded).

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

**result.json (lli — backend):**
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
- Oracle crash on original IR: report in `error` field
- If bisect cannot isolate a single pass: report the smallest pipeline possible in `args`
- If alive2 reports approximation or unsupported intrinsics: keep llubi, do NOT upgrade
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
