# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2025 Yingwei Zheng
# This file is licensed under the Apache-2.0 License.
# See the LICENSE file for more information.

import os
import sys
import subprocess

LLVM_BIN_PATH = os.environ.get("LLVM_BIN_PATH", "/usr/bin")
CLANG = os.path.join(LLVM_BIN_PATH, "clang")
LLVM_REDUCE = os.path.join(LLVM_BIN_PATH, "llvm-reduce")
LLVM_OPT = os.path.join(LLVM_BIN_PATH, "opt")
LLVM_LLC = os.path.join(LLVM_BIN_PATH, "llc")

input_file = sys.argv[1]
if len(sys.argv) > 2:
    extra_args = sys.argv[2:]
temp_file = input_file + ".temp"
reduced_file = input_file + ".ll"

# Middle-end crash or backend crash?
is_middle_end = False
try:
    # Compile the input file to LLVM IR
    subprocess.check_call([CLANG, "-S", "-emit-llvm", "-o", temp_file] + extra_args + [input_file])
except Exception as e:
    is_middle_end = True
    pass

# TODO: use bugpoint

if is_middle_end:
    # Run llvm-reduce
    pass
else:
    # Run llvm-reduce
    pass
