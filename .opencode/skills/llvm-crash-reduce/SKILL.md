---
name: llvm-crash-reduce
description: Reduce LLVM crash reproducers — opt-bisect-limit to find pass, then llvm-reduce IR
---

## Tools
All LLVM tools are on PATH: `opt`, `llc`, `lli`, `llvm-reduce`, `clang`, `alive-tv`, `llubi_legacy`.

## Crash Reduction Pipeline

### 1. Reproduce the crash
Run the crash command from the issue. Extract a crash signature (e.g., "Assertion `X && Y` failed at Pass.cpp:1234").

### 2. Build opt pipeline
If the issue gives a specific pass, use it directly. Otherwise try `-passes='default<O2>'` or `-passes='default<O3>'`.

### 3. opt-bisect-limit binary search
```
opt -opt-bisect-limit=-1 -passes='<pipeline>' repro.ll 2>&1   → total=N
```
Binary search lo=1, hi=N:
```
opt -opt-bisect-limit=M -passes='<pipeline>' repro.ll 2>&1
  crash → hi=M
  ok    → lo=M+1
```
Converge to M.

### 4. Capture IR before crashing pass
```
opt -opt-bisect-limit=M-1 -passes='<pipeline>' repro.ll -S > before.ll
```
Extract pass name from crash output or bisect log.

### 5. Handle llc/lli crashes
If crash is in llc or lli (not opt), skip bisect and go directly to llvm-reduce with an interestingness script that runs llc/lli and checks for the crash signature.

### 6. llvm-reduce
```bash
cat > interestingness.sh <<'EOF'
#!/bin/bash
opt -passes='<pass>' "$1" 2>&1 | grep -q "<signature>"
EOF
chmod +x interestingness.sh
llvm-reduce --test=interestingness.sh before.ll
```
Output: `reduced.ll`

### 7. Verify
Run the pass on `reduced.ll`, confirm crash signature still matches.

### 8. Write results

**result.json:**
```json
{
  "type": "crash",
  "tool": "opt",
  "pass_name": "licm",
  "opt_args": "",
  "ir_file": "reduced.ll",
  "crash_pattern": "Assertion.*failed"
}
```

**report.md:**
```markdown
## Reduced: crash in `<pass_name>`

### Reproduce
`opt -passes='<pass_name>' reduced.ll -S`

### Reduced IR
```llvm
...contents of reduced.ll...
```
```

## Error handling
- If any step fails, write error to result.json `{"error": "..."}` and to report.md
- All files stay in current workdir, never /tmp
