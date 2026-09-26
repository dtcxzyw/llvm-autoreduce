---
name: llvm-miscompile-reduce
description: Reduce LLVM miscompilation reproducers — LLUBI/LLI oracle + opt-bisect-limit + llvm-reduce
---

## Tools
All LLVM tools are on PATH: `opt`, `llc`, `lli`, `llvm-reduce`, `clang`, `alive-tv`, `llubi`, `llvm-extract`.

**Timeout rule: wrap every standalone `opt`, `llc`, `lli`, or `clang` command with `timeout 60`.** llubi `--max-steps 1000000` is sufficient. interestingness.sh commands already carry timeouts — no extra wrapping needed there.

## Miscompilation Reduction Pipeline

**CRITICAL: Reduction operates exclusively on LLVM IR. Never compile IR to native binaries for verification — use the oracle tools (llubi, lli) directly on IR.** `alive-tv` may be used as a diagnostic (confirm the bug, locate the miscompiled function, read the counterexample), but it is never the submitted oracle: the reduced IR MUST stay executable by llubi (middle-end) or lli (backend).

### 0. Read metadata from extract.json
Read `extract.json` and note:
- `oracle` — `opt` for middle-end, `llc` for backend.
- `args` — the opt/llc/lli arguments. For oracle=opt this is the opt pipeline (e.g. `-passes='default<O2>'`). For oracle=llc this is the llc/lli args (usually `""` for backend miscompilation — the reproducer IR is already fully optimized by clang).
- `reproducer_file` — the `.ll` file to reduce. For backend miscompilation with oracle=llc, this is `full_opt.ll` (already optimized — no bisect needed).
- **`pattern`** — how the miscompilation manifests: `wrong_output`, `nonzero_exit`, or `infinite_loop`. The interestingness script MUST be written to preserve this exact pattern type — do NOT change wrong_output into a crash check or vice versa.

Create a symlink for convenience:
```
ln -sf <reproducer_file> repro.ll
```

### 1. Choose bisect/reduce oracle

Based on `extract.json` oracle:
- `oracle=opt` (middle-end) → use **llubi** for bisect and reduce
- `oracle=llc` (backend) → use **lli** for reduce (no bisect needed — the reproducer IR from clang is already fully optimized)

**CRITICAL — the result oracle is always llubi or lli.** `alive-tv` may confirm a miscompilation and show a counterexample, but `result.json` MUST use `oracle=llubi` (middle-end) or `oracle=lli` (backend). The daemon rejects `oracle=alive2`, and an alive2-shaped single-function repro cannot be executed, verified, or bisected. If alive2 shows a mismatch, convert its `Example:` input values into a runnable `i32 @main()` program that prints the miscompiled result (keep `i32 @main(` and avoid `external global`), then reduce that program with the llubi oracle.

**CRITICAL — lli preprocessing:** Before using the `lli` oracle, preprocess the IR to remove `main()` argument dependencies. If `main()` uses `argc`/`argv`, strip those references from the IR (e.g., replace `argc` with a constant). Without this, `llubi` and `lli` may produce different output even on a correct backend because llubi passes its own command-line arguments to `main()` (argv[0] = the input file name) and only fills unknown signatures with null values. Note: for a main() that does not match `int main(int, char**)`, llubi prints `warning: The signature of function 'main' does not match 'int main(int, char**)', passing null values for all arguments.` and CONTINUES with nulls — this warning is benign for the mid-end (llubi) oracle because both the reference and transformed runs see the same signature. If the program dereferences the null-filled arguments, the llubi reference run fails with `Immediate UB detected` — preprocess the IR first (strip main() parameters, replace uses with constants) exactly as for the lli path.

### 2. Reproduce the miscompilation

**Middle-end (llubi):**
```
set -o pipefail
timeout 60 llubi --max-steps 1000000 repro.ll > ref_ubi
! opt -passes='<args>' repro.ll -S | llubi --max-steps 1000000 - | diff -q ref_ubi -
```
**Backend (lli — no bisect, IR is already optimized):**
```
set -e
timeout 60 llubi --max-steps 1000000 repro.ll > ref_ubi
timeout 10 lli <args> repro.ll > _lli_out
! diff -q ref_ubi _lli_out
```
**ACCEPTED RISK:** Crashes in the pipeline (opt, llubi, or lli segfault) are treated as miscompilation: `pipefail` makes the pipeline exit non-zero on crash, `!` inverts that to exit 0 ("miscompilation found"). The daemon's final `verify()` step independently checks the reduced IR and will reject cases where the miscompilation does not actually reproduce, so a crash-confused reduction is caught at verification time. **llubi unsupported-instruction failures are NOT miscompilations:** llubi exits non-zero with `Unrecognized instruction` on stderr for instructions/intrinsics it does not implement (e.g. target-specific intrinsics). The nonzero_exit templates below reject such candidates via `grep -q 'Unrecognized instruction' _err.txt && exit 1` — do NOT remove that guard. If the ORIGINAL reproducer fails this way, llubi cannot handle the IR: remove the unsupported construct from the IR (or reject the issue) instead of treating it as a bug.

### 3. opt-bisect-limit binary search to find single pass

**Middle-end only (oracle=opt).** For backend miscompilation (oracle=llc), skip to step 5 — the reproducer IR is already optimized and no bisect is needed.

First, pre-compute the reference output and get total pass count:
```
timeout 60 llubi --max-steps 1000000 repro.ll > ref_ubi
timeout 60 opt -opt-bisect-limit=-1 -passes='<args>' repro.ll -S -o /dev/null 2>&1   → total=N
```

**Write a bisect script — do NOT run the binary search inline (llvm-reduce style):**

**Mid-end (llubi oracle):**
```bash
cat > bisect.sh <<'SCRIPT'
#!/bin/bash
set -e
M="$1"
ref="$2"
ir="$3"
timeout 30 opt -opt-bisect-limit="$M" -passes='<args>' "$ir" -S > _bisect_opt.ll
timeout 120 llubi --max-steps 1000000 _bisect_opt.ll > _bisect_out.txt
! diff -q "$ref" _bisect_out.txt
SCRIPT
chmod +x bisect.sh
```
Then binary search: `lo=1`, `hi=N`. At each step run `bisect.sh M ref_ubi repro.ll`:
- exit 0 → miscompilation at or before M → hi=M
- exit 1 → correct up to M → lo=M+1

**Backend (lli oracle):**
```bash
cat > bisect.sh <<'SCRIPT'
#!/bin/bash
set -e
M="$1"
ref="$2"
ir="$3"
timeout 30 opt -opt-bisect-limit="$M" -passes='<args>' "$ir" -S > _bisect_opt.ll
timeout 10 lli _bisect_opt.ll > _bisect_out.txt
! diff -q "$ref" _bisect_out.txt
SCRIPT
chmod +x bisect.sh
```
Then binary search, same as above.

**ACCEPTED RISK:** Crash → miscompilation. `set -e` causes the script to exit non-zero if opt crashes, and `! diff -q` inverts: if oracle crashes, `diff` exits non-zero (ref exists, _bisect_out.txt missing/empty), `!` returns 0. The daemon's `verify()` step independently confirms.

**IMPORTANT:** `diff -q` only compares exit code (0=same, 1=differ), no content output. `set -e` exits early if opt fails (no _bisect_opt.ll). The reference output is computed once, not inside the loop. Each bisect step uses temp files, avoiding pipefail complexity.

### 4. Extract the single pass name and capture IR before it

The bisect log prints the last pass run before the miscompilation (e.g. `BISECT: running pass (N) GVN on ...`). Convert this to the `-passes=` form (e.g. `-passes=gvn`). Do NOT guess from filenames.

**If the miscompiling pass is InstCombine, you MUST always write it as `instcombine<no-verify-fixpoint>`** — never bare `instcombine`. This applies everywhere the pass is used: bisect commands, interestingness.sh, and the `args` field in result.json. The daemon rejects `instcombine` without `instcombine<no-verify-fixpoint>` in both extract.json and result.json args. This only constrains how instcombine is written when it is already present — it is NOT a requirement to include instcombine.

Capture the IR just before the bad pass:
```
opt -opt-bisect-limit=M-1 -passes='<args>' repro.ll -S > before.ll
```

### 5. llvm-reduce with ONLY the single pass

**CRITICAL: The interestingness script MUST match the pattern from extract.json.** Choose the template for the pattern type. Preserving the exact pattern type ensures the reduced IR manifests the same kind of miscompilation — wrong_output stays wrong_output, nonzero_exit stays nonzero_exit, infinite_loop stays infinite_loop.

**All miscompilation interestingness scripts MUST also reject IR containing `undef`** — undef masks genuine miscompilations. Add `if grep -q " undef" "$1"; then exit 1; fi` as the first check in every template below.

**All interestingness scripts MUST also reject IR with target intrinsics but no target-features** — llvm-reduce will strip the attribute otherwise, breaking oracle commands. Add the following guard after the undef check in every template below:
```bash
if grep -qP 'declare.*@llvm\.(x86|aarch64|arm|nvptx|amdgcn)\.' "$1"; then
  grep -q 'target-features' "$1" || exit 1
fi
```

**main()/self-containment guards — differ per oracle.** The lli (backend) templates reject IR where `main()` has parameters (`grep -qP 'define\s+\S+\s+@main\s*\(\s*\)' "$1" || exit 1`): llubi and lli may pass different argv to a parameterized main, so the ref-vs-test comparison would be unreliable. The llubi (middle-end) templates guard runnability and self-containment instead — every candidate MUST keep an `i32 @main(` definition (`grep -q "i32 @main(" "$1" || exit 1`) and MUST NOT contain `external global` (`grep -q "external global" "$1" && exit 1`). Without these guards llvm-reduce would delete or retype the entry function (llubi then has nothing runnable to execute) or strip global initializers into `external global` references — external symbols cannot be resolved by llubi/lli, so such IR is not self-contained and would not behave identically under the daemon's verify step or in an upstream reproduction. The i32-main guard deliberately permits a parameterized `i32 @main(i32 %argc, ptr %argv)` — both the reference and transformed runs execute the SAME candidate, so llubi applies the same signature rule (real argv for `main(i32, ptr)`, null-fill with a warning for anything else) to both runs and a signature change can never fake a ref-vs-test difference. Candidates that dereference null-filled main arguments make the llubi reference run fail with `Immediate UB detected` and are rejected by the ref `|| exit 1` / `set -e` — an implicit safety net. If llvm-reduce strips an unused main() parameter during reduction, the resulting candidate's behavior changes symmetrically (both runs see it), so it is simply not interesting and gets discarded. The daemon's `verify_llubi` statically enforces the same two requirements on both the extract-stage reproducer and the reduced IR.

**llubi oracle (middle-end) — pattern=wrong_output:**
```bash
cat > interestingness.sh <<'SCRIPT'
#!/bin/bash
set -eo pipefail
if grep -q " undef" "$1"; then exit 1; fi
if grep -qP 'declare.*@llvm\.(x86|aarch64|arm|nvptx|amdgcn)\.' "$1"; then
  grep -q 'target-features' "$1" || exit 1
fi
# Reject IR without a runnable `i32 @main(` — and with external globals (not self-contained)
if ! grep -q "i32 @main(" "$1"; then exit 1; fi
if grep -q "external global" "$1"; then exit 1; fi
timeout 120 llubi --max-steps 1000000 "$1" > _ref.txt
timeout 30 opt -passes='<pass_name>' "$1" -S > _opt.ll
timeout 120 llubi --max-steps 1000000 _opt.ll > _out.txt
! diff -q _ref.txt _out.txt
SCRIPT
```

**llubi oracle (middle-end) — pattern=nonzero_exit:**
```bash
cat > interestingness.sh <<'SCRIPT'
#!/bin/bash
set -o pipefail
if grep -q " undef" "$1"; then exit 1; fi
if grep -qP 'declare.*@llvm\.(x86|aarch64|arm|nvptx|amdgcn)\.' "$1"; then
  grep -q 'target-features' "$1" || exit 1
fi
# Reject IR without a runnable `i32 @main(` — and with external globals (not self-contained)
if ! grep -q "i32 @main(" "$1"; then exit 1; fi
if grep -q "external global" "$1"; then exit 1; fi
timeout 120 llubi --max-steps 1000000 "$1" > _ref.txt || exit 1
timeout 30 opt -passes='<pass_name>' "$1" -S | timeout 120 llubi --max-steps 1000000 - > _out.txt 2> _err.txt
ret=$?
# Reject IR that llubi cannot interpret (unsupported instruction/intrinsic) — NOT a miscompilation
grep -q 'Unrecognized instruction' _err.txt && exit 1
# Exit 0 (interesting) if pipeline failed with crash/signal/assert — NOT timeout (124)
test $ret -ne 0 -a $ret -ne 124
SCRIPT
```

**llubi oracle (middle-end) — pattern=infinite_loop:**
```bash
cat > interestingness.sh <<'SCRIPT'
#!/bin/bash
set -o pipefail
if grep -q " undef" "$1"; then exit 1; fi
if grep -qP 'declare.*@llvm\.(x86|aarch64|arm|nvptx|amdgcn)\.' "$1"; then
  grep -q 'target-features' "$1" || exit 1
fi
# Reject IR without a runnable `i32 @main(` — and with external globals (not self-contained)
if ! grep -q "i32 @main(" "$1"; then exit 1; fi
if grep -q "external global" "$1"; then exit 1; fi
timeout 120 llubi --max-steps 1000000 "$1" > _ref.txt || exit 1
timeout 30 opt -passes='<pass_name>' "$1" -S | timeout 120 llubi --max-steps 1000000 -
ret=$?
# Exit 0 (interesting) only if pipeline timed out
test $ret -eq 124
SCRIPT
```

**lli oracle (backend) — pattern=wrong_output:**
```bash
cat > interestingness.sh <<'SCRIPT'
#!/bin/bash
set -eo pipefail
if grep -q " undef" "$1"; then exit 1; fi
if grep -qP 'declare.*@llvm\.(x86|aarch64|arm|nvptx|amdgcn)\.' "$1"; then
  grep -q 'target-features' "$1" || exit 1
fi
# Reject IR where main() has parameters — llubi and lli may pass different argv
grep -qP 'define\s+\S+\s+@main\s*\(\s*\)' "$1" || exit 1
timeout 120 llubi --max-steps 1000000 "$1" > _ref.txt
timeout 10 lli <args> "$1" > _out.txt
! diff -q _ref.txt _out.txt
SCRIPT
```

**lli oracle (backend) — pattern=nonzero_exit:**
```bash
cat > interestingness.sh <<'SCRIPT'
#!/bin/bash
set -o pipefail
if grep -q " undef" "$1"; then exit 1; fi
if grep -qP 'declare.*@llvm\.(x86|aarch64|arm|nvptx|amdgcn)\.' "$1"; then
  grep -q 'target-features' "$1" || exit 1
fi
# Reject IR where main() has parameters — llubi and lli may pass different argv
grep -qP 'define\s+\S+\s+@main\s*\(\s*\)' "$1" || exit 1
timeout 120 llubi --max-steps 1000000 "$1" > _ref.txt || exit 1
timeout 10 lli <args> "$1" > /dev/null
ret=$?
# Exit 0 (interesting) if pipeline failed with crash/signal/assert — NOT timeout (124)
test $ret -ne 0 -a $ret -ne 124
SCRIPT
```

**lli oracle (backend) — pattern=infinite_loop:**
```bash
cat > interestingness.sh <<'SCRIPT'
#!/bin/bash
set -o pipefail
if grep -q " undef" "$1"; then exit 1; fi
if grep -qP 'declare.*@llvm\.(x86|aarch64|arm|nvptx|amdgcn)\.' "$1"; then
  grep -q 'target-features' "$1" || exit 1
fi
# Reject IR where main() has parameters — llubi and lli may pass different argv
grep -qP 'define\s+\S+\s+@main\s*\(\s*\)' "$1" || exit 1
timeout 120 llubi --max-steps 1000000 "$1" > _ref.txt || exit 1
timeout 10 lli <args> "$1" > /dev/null
ret=$?
# Exit 0 (interesting) only if pipeline timed out
test $ret -eq 124
SCRIPT
```
**lli oracle (backend) — pattern=nonzero_exit:**

```bash
cat > interestingness.sh <<'SCRIPT'
#!/bin/bash
set -o pipefail
if grep -q " undef" "$1"; then exit 1; fi
if grep -qP 'declare.*@llvm\.(x86|aarch64|arm|nvptx|amdgcn)\.' "$1"; then
  grep -q 'target-features' "$1" || exit 1
fi
timeout 120 llubi --max-steps 1000000 "$1" > _ref.txt || exit 1
timeout 30 opt -passes='<pass_name>' "$1" -S | timeout 120 lli -
ret=$?
# Exit 0 (interesting) if pipeline failed with crash/signal/assert — NOT timeout (124)
test $ret -ne 0 -a $ret -ne 124
SCRIPT
```

**lli oracle (backend) — pattern=infinite_loop:**

```bash
cat > interestingness.sh <<'SCRIPT'
#!/bin/bash
set -o pipefail
if grep -q " undef" "$1"; then exit 1; fi
if grep -qP 'declare.*@llvm\.(x86|aarch64|arm|nvptx|amdgcn)\.' "$1"; then
  grep -q 'target-features' "$1" || exit 1
fi
timeout 120 llubi --max-steps 1000000 "$1" > _ref.txt || exit 1
timeout 30 opt -passes='<pass_name>' "$1" -S | timeout 120 lli -
ret=$?
# Exit 0 (interesting) only if pipeline timed out
test $ret -eq 124
SCRIPT
```
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
  "args": "-passes=<pass_name>",
  "ir_file": "reduced.ll",
  "reference_file": "repro.ll",
  "oracle": "llubi",
  "llubi_args": "--max-steps 1000000"
}
```

**Backend (lli):**
```json
{
  "type": "miscompilation",
  "args": "<lli_args from extract.json>",
  "ir_file": "reduced.ll",
  "reference_file": "repro.ll",
  "oracle": "lli",
  "llubi_args": "--max-steps 1000000",
  "lli_args": ""
}
```

### 7. Alive2 is a diagnostic only (optional)

`alive-tv` can help confirm a middle-end miscompilation, locate the miscompiled function, and print a concrete counterexample. It is NOT a submittable oracle:

- Never set `oracle: "alive2"` in result.json — the daemon rejects it.
- Never submit an alive2-shaped repro (single function, no `i32 @main(`) — it has nothing for llubi to execute, cannot be verified by the daemon, and cannot be bisected by llvm-bisect-service.
- If alive2 reports `ERROR: Value mismatch` / `N incorrect transformations` with an `Example:` section, read the counterexample inputs and build an `i32 @main()` driver that calls the function with those inputs and prints the result. Reduce that runnable program with the llubi oracle (the extractor agent docs contain a complete conversion example).
- If alive2 reports "Transformation seems to be correct", "Alive2 approximated the semantics", or unsupported intrinsic/metadata, just continue with llubi.

### 8. Additional manual reduction (optional — only if time permits)

After the checkpoint result.json, try these techniques to shrink `reduced.ll` further. Test after each change that the miscompilation still reproduces. If any succeeds, update result.json with the improved `ir_file`.

**Reduce bitwidth:** Replace `i64` with smaller integer types (`i32`, `i16`, `i8`) where possible. Adjust constants accordingly. Test that the miscompilation still reproduces.

**Reduce pointer width:** In the target datalayout, change pointer size to `p:8:8` (or appropriate small size for the target).

**Reduce loop trip count:** If the IR has a loop with a fixed trip count (e.g. `br i1 %cmp, label %loop, label %exit` where %cmp compares induction variable against a constant like 128), reduce the constant (e.g. 128 → 4). This shrinks the loop body that needs to be preserved.

**NEVER use undef.** The reduced IR MUST NOT contain `undef` values — they cause non-deterministic behavior and can mask real miscompilations across all oracles (llubi, lli). If the original reproducer contains `undef`, replace it with `zeroinitializer` (for aggregates), `null` (for pointers), or explicit constant values (e.g. `i32 0`). The interestingness script and verification step will reject IR that still contains `undef`.

**Strip fast math flags.** If the IR contains `fast` or other fast-math flags on floating-point instructions, decompose `fast` into its constituent flags and keep only `nnan` and `ninf` — rewrite `fast` as `nnan ninf` explicitly. For any other fast-math flags (`nsz`, `arcp`, `contract`, `afn`, `reassoc`), remove them. If the miscompilation is specifically related to `nsz` (no-signed-zeros), prefer to drop `nsz` entirely rather than preserve it.

### 9. Verify final result

Verify the reduced IR still reproduces the miscompilation with the single pass. Write the final `result.json` (update from checkpoint if manual reduction succeeded).

**args field requirements:** After bisect isolates the bug to a single pass (or a few specific passes), `args` MUST include that pass (e.g. `-passes=gvn`). Auxiliary flags that help reproduce the bug (e.g. `-slp-threshold=-99999`) may be included alongside the pass when relevant. **If the pass is instcombine, write it as `instcombine<no-verify-fixpoint>`** (e.g. `-passes=instcombine<no-verify-fixpoint>`) — the daemon rejects bare `instcombine`. The `args` field MUST NOT contain `-opt-bisect-limit` (bisect is a diagnostic step, NOT stored in result.json) and MUST NOT contain `default<` (the full O1/O2/O3 pipeline — bisect already narrowed it to the specific problematic pass). **Backend/codegen passes MUST use legacy PM:** when invoking backend passes like codegenprepare with `opt`, use `-codegenprepare` (legacy syntax), never `-passes=codegenprepare` (the new pass manager does not register codegen passes).

**result.json (llubi):**
```json
{
  "type": "miscompilation",
  "args": "-passes=gvn",
  "ir_file": "reduced.ll",
  "reference_file": "repro.ll",
  "oracle": "llubi",
  "llubi_args": "--max-steps 1000000"
}
```

**result.json (lli — backend):**
```json
{
  "type": "miscompilation",
  "args": "<lli_args from extract.json>",
  "ir_file": "reduced.ll",
  "reference_file": "repro.ll",
  "oracle": "lli",
  "llubi_args": "--max-steps 1000000",
  "lli_args": ""
}
```

## Error handling
- Oracle crash on original IR: report in `error` field
- If bisect cannot isolate a single pass: report the smallest pipeline possible in `args`
- If all reduction attempts fail, write `result.json` with the FULL schema plus an `error` field describing the reason. The daemon requires all schema fields to be present — a bare `{"error": "..."}` will fail validation. Use:
```json
{
  "type": "miscompilation",
  "args": "",
  "ir_file": "error.ll",
  "reference_file": "repro.ll",
  "oracle": "llubi",
  "llubi_args": "--max-steps 1000000",
  "error": "brief description of what failed"
}
```
- Do NOT generate a report.md file — the daemon handles report generation
- CRITICAL: All files stay in current working directory, never /tmp, /home, /etc, /var, or any other system path
