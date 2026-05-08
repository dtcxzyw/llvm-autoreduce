---
name: llvm-miscompile-reduce
description: Reduce LLVM miscompilation reproducers — LLUBI/Alive2 oracle + opt-bisect-limit + llvm-reduce
---

## Tools
All LLVM tools are on PATH: `opt`, `llc`, `lli`, `llvm-reduce`, `clang`, `alive-tv`, `llubi_legacy`.

## Miscompilation Reduction Pipeline

### 1. x86 verification
```
clang -target x86_64-linux repro.ll -O0 -o ref && ./ref > ref_out
opt -passes='<pipeline>' repro.ll | llc | clang -x assembler - -o test && ./test > test_out
diff ref_out test_out
```
If no diff: not reproducible on x86, abort.

### 2. Choose oracle
Default: `llubi_legacy`. Fallback: `alive-tv`. For backend miscompilations (where the bug is in codegen/instruction selection), use `lli` oracle: llubi_legacy verifies the IR semantics, lli runs the transformed IR through the JIT backend, and differing outputs indicate a backend miscompilation.

### 3. opt-bisect-limit binary search with oracle

**llubi_legacy:**
```
llubi_legacy --max-steps 1000000 repro.ll > ref_ubi
```
Binary search:
```
opt -opt-bisect-limit=N -passes='<pipeline>' repro.ll -S | llubi_legacy --max-steps 1000000 - > test_ubi
diff ref_ubi test_ubi
  same    → lo=N+1
  diff    → hi=N
```

**alive-tv:**
```
opt -opt-bisect-limit=N -passes='<pipeline>' repro.ll -S > step.ll
alive-tv --disable-undef-input --smt-to=10000 repro.ll step.ll
  "Transformation seems to be correct!" in output → lo=N+1
  counterexample in output → hi=N
```

**lli:**
```
llubi_legacy --max-steps 1000000 repro.ll > ref_ubi
```
Binary search:
```
opt -opt-bisect-limit=N -passes='<pipeline>' repro.ll -S | lli - > test_out
diff ref_ubi test_out
  same    → lo=N+1
  diff    → hi=N
```

### 4. Capture IR before bad pass
```
opt -opt-bisect-limit=M-1 -passes='<pipeline>' repro.ll -S > before.ll
```

### 5. llvm-reduce

<!-- NOTE: llvm-reduce executes the interestingness.sh script generated below. -->
<!-- This is an accepted risk — the workdir is isolated and the security -->
<!-- reviewer screens reproducer content before this stage is reached. -->
<!-- NOTE: re-running llubi_legacy on repro.ll inside the llubi test script -->
<!-- below is a known performance hit — the reference output is recomputed -->
<!-- for every llvm-reduce candidate. Accepted trade-off to keep the test -->
<!-- script self-contained and stateless, avoiding stale-reference bugs. -->

**llubi_legacy oracle:**
```
cat > interestingness.sh <<'SCRIPT'
#!/bin/bash
llubi_legacy --max-steps 1000000 repro.ll > ref_ubi.txt
opt -passes='<pass>' "$1" -S | llubi_legacy --max-steps 1000000 - > test_ubi.txt
! diff -q ref_ubi.txt test_ubi.txt
SCRIPT
```

**alive-tv oracle:**
```
cat > interestingness.sh <<'SCRIPT'
#!/bin/bash
opt -passes='<pass>' "$1" -S > opt_output.ll
alive-tv --disable-undef-input --smt-to=10000 "$1" opt_output.ll 2>&1 | grep -qv "Transformation seems to be correct!"
SCRIPT
```

**lli oracle:**
```
cat > interestingness.sh <<'SCRIPT'
#!/bin/bash
llubi_legacy --max-steps 1000000 repro.ll > ref_ubi.txt
opt -passes='<pass>' "$1" -S | lli - > test_out.txt
! diff -q ref_ubi.txt test_out.txt
SCRIPT
```

Then: `chmod +x interestingness.sh && llvm-reduce --test=interestingness.sh before.ll`

### 6. Write results

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

**result.json (lli — backend miscompilation):**
```json
{
  "type": "miscompilation",
  "tool": "opt",
  "args": "-passes='default<O2>'",
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
- x86 verification fails: abort, write `{"error": "not x86 reproducible"}` to result.json
- Write errors to result.json
- Do NOT generate a report.md file — the daemon handles report generation
- CRITICAL: All files stay in current working directory, never /tmp, /home, /etc, /var, or any other system path
