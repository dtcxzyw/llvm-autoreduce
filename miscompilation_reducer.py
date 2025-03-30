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
LLUBI = os.environ.get("LLUBI_PATH")

input_file = sys.argv[1]
if len(sys.argv) > 2:
    extra_args = sys.argv[2:]

temp_file = input_file + ".temp"
reduced_file = input_file + ".ll"
OPT_ARGS = ""
EXTRA_ARGS = ""

creduce_template = f"""
#!/usr/bin/bash

clang -O0 test.c -w {EXTRA_ARGS} 2>/dev/null
if [ $? -ne 0 ]; then
    exit 1
fi
a=$(./a.out)

clang -ffp-contract=on -w -mllvm -no-stack-coloring {OPT_ARGS} {EXTRA_ARGS} test.c -Werror -Wno-tautological-compare -Wno-pointer-sign -Wno-implicit-const-int-float-conversion -Wno-tautological-constant-compare -Wno-constant-conversion -Wno-tautological-constant-out-of-range-compare -Wno-parentheses-equality -Wno-tautological-pointer-compare -Wno-unused-value -Wno-constant-logical-operand -Wno-compare-distinct-pointer-types -Wno-overflow
if [ $? -ne 0 ]; then
    exit 2
fi
b=$(./a.out)

if [[ $a == $b ]]; then
    exit 3
fi

clang -O0 test.c -Werror=incompatible-library-redeclaration -Werror=pointer-to-int-cast -Werror=tentative-definition-array -Werror=incompatible-pointer-types -Werror=format -Werror=deprecated-non-prototype {EXTRA_ARGS} -fsanitize=address,undefined 2>/dev/null
if [ $? -ne 0 ]; then
    exit 4
fi
./a.out 2>&1 > /dev/null
if [ $? -ne 0 ]; then
    exit 5
fi
./a.out 2>&1 | grep "er" >/dev/null
if [ $? -eq 0 ]; then
    exit 6
fi

clang -O0 test.c -w {EXTRA_ARGS} -fsanitize=memory,undefined
if [ $? -ne 0 ]; then
    exit 7
fi
./a.out 2>&1 | grep "er" >/dev/null
if [ $? -eq 0 ]; then
    exit 8
fi

gcc -O1 test.c -w {EXTRA_ARGS} -fsanitize=address,undefined 2>/dev/null
if [ $? -eq 0 ]; then
    ./a.out 2>&1 | grep "er" >/dev/null
    if [ $? -eq 0 ]; then
        exit 9
    fi
fi

exit 0
"""

PASS = ""
after_opt_ub = ""

llubi_template = f"""
#!/usr/bin/bash

a=$(llubi --max-steps 1000000 --reduce-mode $1)
if [ $? -ne 0 ]; then
    exit 1
fi
opt -passes={PASS} $1 -o $1.tmp -S
if [ $? -ne 0 ]; then
    exit 1
fi
b=$(llubi --max-steps 1000000 --reduce-mode $1.tmp)
if [ $? -ne 0 ]; then
    exit {0 if after_opt_ub else 1}
fi
if [[ "$a" == "$b" ]]; then
    exit 2
fi

exit 0
"""
