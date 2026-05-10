#!/usr/bin/env python3
"""Verify extract.json: reproduce the bug and check main() params.

Usage:
    python scripts/verify-extract.py

Uses the current working directory as the workdir.
Exit 0 if the bug reproduces and all checks pass, exit 1 otherwise.
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

    meta = read_json(wd / "extract.json")
    bug_type = meta.get("type", "")
    oracle = meta.get("oracle", "")
    print(f"extract.json: type={bug_type} oracle={oracle}")

    # Check main() params for backend miscompilation
    if bug_type == "miscompilation" and oracle == "llc":
        reproducer = meta.get("reproducer_file", "")
        if reproducer and not daemon._check_main_no_params(reproducer, wd):
            print("FAIL: backend miscomp reproducer main() has parameters")
            sys.exit(1)
        print("PASS: main() has no parameters")

    # Reproduce the bug
    if daemon.verify_extract(meta, wd):
        print("PASS: bug reproduces")
        sys.exit(0)
    else:
        print("FAIL: bug does not reproduce")
        sys.exit(1)


if __name__ == "__main__":
    main()
