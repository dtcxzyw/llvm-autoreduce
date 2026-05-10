#!/usr/bin/env python3
"""Verify result.json: reproduce the bug with the reduced IR.

Usage:
    python scripts/verify-result.py

Uses the current working directory as the workdir.
Exit 0 if the reduced IR still reproduces the bug, exit 1 otherwise.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from llvm_autoreduce import daemon
from llvm_autoreduce.workdir import read_json


def main():
    wd = Path.cwd()

    if not (wd / "extract.json").exists():
        print(f"FAIL: extract.json not found in {wd}")
        sys.exit(1)
    if not (wd / "result.json").exists():
        print(f"FAIL: result.json not found in {wd}")
        sys.exit(1)

    meta = read_json(wd / "extract.json")
    result = read_json(wd / "result.json")
    bug_type = meta.get("type", "")
    oracle = result.get("oracle", "")
    print(f"extract: type={bug_type}  result: oracle={oracle}")

    # Check main() params for backend miscompilation
    if result.get("oracle") == "lli":
        ir_file = result.get("ir_file", "")
        if ir_file and not daemon._check_main_no_params(ir_file, wd):
            print("FAIL: reduced IR main() has parameters")
            sys.exit(1)
        print("PASS: main() has no parameters")

    # Cross-check extract vs result consistency
    if not daemon.verify_extract_consistency(meta, result, wd):
        print("FAIL: extract/result consistency check failed")
        sys.exit(1)
    print("PASS: extract/result consistent")

    # Full verification
    if daemon.verify(result, wd, meta):
        print("PASS: bug reproduces with reduced IR")
        sys.exit(0)
    else:
        print("FAIL: bug does not reproduce with reduced IR")
        sys.exit(1)


if __name__ == "__main__":
    main()
