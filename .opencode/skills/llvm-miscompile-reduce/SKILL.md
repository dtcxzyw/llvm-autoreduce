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
Default: `llubi_legacy`. Fallback: `alive-tv`.

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

### 4. Capture IR before bad pass
```
opt -opt-bisect-limit=M-1 -passes='<pipeline>' repro.ll -S > before.ll
```

### 5. llvm-reduce

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

Then: `chmod +x interestingness.sh && llvm-reduce --test=interestingness.sh before.ll`

### 6. Write results

**result.json:**
```json
{
  "type": "miscompilation",
  "tool": "opt",
  "pass_name": "gvn",
  "ir_file": "reduced.ll",
  "reference_file": "repro.ll",
  "oracle": "llubi",
  "llubi_args": "--max-steps 1000000",
  "alive2_args": "--smt-to=10000"
}
```

**report.md:**
```markdown
## Reduced: miscompilation in `<pass_name>`

**Oracle:** llubi / alive2

### Reproduce
`opt -passes='<pass_name>' reduced.ll -S`

### Reduced IR
```llvm
...contents of reduced.ll...
```
```

## Error handling
- Oracle crash on original IR: try the other oracle
- x86 verification fails: abort, write `{"error": "not x86 reproducible"}` to result.json
- Write errors to result.json and report.md
- CRITICAL: All files stay in current working directory, never /tmp, /home, /etc, /var, or any other system path
