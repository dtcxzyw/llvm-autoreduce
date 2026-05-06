#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORK_DIR="$(cd "$SCRIPT_DIR/.." && pwd)/work"
KNOWN_GOOD_FILE="$WORK_DIR/.known-good"

mkdir -p "$WORK_DIR"

# ---- helpers ----

get_hash() {
    git -C "$1" rev-parse HEAD
}

checkout_and_build_llvm() {
    local hash="$1"
    git -C "$WORK_DIR/llvm-trunk" checkout "$hash"
    cmake -B "$WORK_DIR/llvm-trunk/build" \
        -S "$WORK_DIR/llvm-trunk/llvm" \
        -DCMAKE_BUILD_TYPE=RelWithDebInfo \
        -DBUILD_SHARED_LIBS=ON \
        -G Ninja \
        -DLLVM_ENABLE_PROJECTS=clang \
        -DLLVM_ENABLE_ASSERTIONS=ON \
        -DLLVM_INCLUDE_EXAMPLES=OFF \
        -DLLVM_ENABLE_WARNINGS=OFF \
        -DLLVM_APPEND_VC_REV=OFF \
        -DCMAKE_C_COMPILER_LAUNCHER=ccache \
        -DCMAKE_CXX_COMPILER_LAUNCHER=ccache \
        -DLLVM_ENABLE_RTTI=ON \
        -DLLVM_ENABLE_EH=ON \
        -DLLVM_ENABLE_ZSTD=OFF
    cmake --build "$WORK_DIR/llvm-trunk/build" --target opt llc lli llvm-reduce clang
}

checkout_and_build_alive2() {
    local hash="$1"
    git -C "$WORK_DIR/alive2-trunk" checkout "$hash"
    cmake -B "$WORK_DIR/alive2-trunk/build" \
        -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DLLVM_DIR="$WORK_DIR/llvm-trunk/build/lib/cmake/llvm" \
        -DBUILD_TV=ON
    cmake --build "$WORK_DIR/alive2-trunk/build" --target alive-tv
}

checkout_and_build_llubi() {
    local hash="$1"
    git -C "$WORK_DIR/llubi-trunk" checkout "$hash"
    cmake -B "$WORK_DIR/llubi-trunk/build" \
        -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DLLVM_DIR="$WORK_DIR/llvm-trunk/build/lib/cmake/llvm"
    cmake --build "$WORK_DIR/llubi-trunk/build" --target llubi_legacy
}

# ---- per-component rollback helpers ----

rollback_llvm() {
    echo "ROLLBACK: restoring known-good LLVM"
    local hash
    hash=$(python3 -c "import json; print(json.load(open('$KNOWN_GOOD_FILE'))['llvm'])")
    checkout_and_build_llvm "$hash" || { echo "FATAL: llvm rollback build failed"; exit 1; }
}

rollback_alive2() {
    echo "ROLLBACK: restoring known-good alive2"
    local hash
    hash=$(python3 -c "import json; print(json.load(open('$KNOWN_GOOD_FILE'))['alive2'])")
    checkout_and_build_alive2 "$hash" || echo "WARN: alive2 rollback build failed"
}

rollback_llubi() {
    echo "ROLLBACK: restoring known-good llubi"
    local hash
    hash=$(python3 -c "import json; print(json.load(open('$KNOWN_GOOD_FILE'))['llubi'])")
    checkout_and_build_llubi "$hash" || echo "WARN: llubi rollback build failed"
}

update_known_hash() {
    local component="$1" hash="$2"
    python3 -c "
import json, os
path = '${KNOWN_GOOD_FILE}'
data = {}
if os.path.exists(path):
    with open(path) as f:
        data = json.load(f)
data['${component}'] = '${hash}'
with open(path, 'w') as f:
    json.dump(data, f, indent=2)
"
}

build_all() {
    checkout_and_build_llvm "$LLVM_LATEST" || { echo "FAIL: LLVM build"; exit 1; }
    checkout_and_build_alive2 "$ALIVE2_LATEST" || { echo "FAIL: alive2 build"; exit 1; }
    checkout_and_build_llubi "$LLUBI_LATEST" || { echo "FAIL: llubi build"; exit 1; }
}

record_known_good() {
    cat > "$KNOWN_GOOD_FILE" <<JSONEOF
{
  "llvm": "$LLVM_LATEST",
  "alive2": "$ALIVE2_LATEST",
  "llubi": "$LLUBI_LATEST"
}
JSONEOF
}

# ---- clone if missing ----

if [ ! -d "$WORK_DIR/llvm-trunk/.git" ]; then
    echo "CLONE: llvm-project"
    git clone https://github.com/llvm/llvm-project "$WORK_DIR/llvm-trunk"
fi

if [ ! -d "$WORK_DIR/alive2-trunk/.git" ]; then
    echo "CLONE: alive2"
    git clone https://github.com/AliveToolkit/alive2 "$WORK_DIR/alive2-trunk"
fi

if [ ! -d "$WORK_DIR/llubi-trunk/.git" ]; then
    echo "CLONE: llvm-ub-aware-interpreter"
    git clone https://github.com/dtcxzyw/llvm-ub-aware-interpreter "$WORK_DIR/llubi-trunk"
fi

# ---- fetch latest ----

git -C "$WORK_DIR/llvm-trunk" fetch origin main
git -C "$WORK_DIR/alive2-trunk" fetch origin master
git -C "$WORK_DIR/llubi-trunk" fetch origin main

# ---- detect state ----

LLVM_CURRENT=$(get_hash "$WORK_DIR/llvm-trunk")
LLVM_LATEST=$(git -C "$WORK_DIR/llvm-trunk" rev-parse origin/main)
ALIVE2_CURRENT=$(get_hash "$WORK_DIR/alive2-trunk")
ALIVE2_LATEST=$(git -C "$WORK_DIR/alive2-trunk" rev-parse origin/master)
LLUBI_CURRENT=$(get_hash "$WORK_DIR/llubi-trunk")
LLUBI_LATEST=$(git -C "$WORK_DIR/llubi-trunk" rev-parse origin/main)

FIRST_BUILD=false
if [ ! -f "$KNOWN_GOOD_FILE" ]; then
    FIRST_BUILD=true
fi
if [ ! -x "$WORK_DIR/llvm-trunk/build/bin/opt" ]; then
    FIRST_BUILD=true
fi

# ---- first-time build ----

if [ "$FIRST_BUILD" = true ]; then
    echo "FIRST-BUILD: building all tools from latest"
    build_all
    record_known_good
    echo "OK: first build complete"
    exit 0
fi

# ---- check if any upstream changes ----

if [ "$LLVM_CURRENT" = "$LLVM_LATEST" ] && [ "$ALIVE2_CURRENT" = "$ALIVE2_LATEST" ] && [ "$LLUBI_CURRENT" = "$LLUBI_LATEST" ]; then
    echo "UP-TO-DATE: no changes"
    exit 0
fi

# ---- attempt incremental update (per-component rollback) ----

if [ "$LLVM_CURRENT" != "$LLVM_LATEST" ]; then
    echo "BUILD: LLVM $LLVM_CURRENT → $LLVM_LATEST"
    if checkout_and_build_llvm "$LLVM_LATEST"; then
        update_known_hash llvm "$LLVM_LATEST"
        LLVM_CURRENT=$LLVM_LATEST
    else
        echo "FAIL: LLVM build, rolling back"
        rollback_llvm
        exit 2
    fi
else
    echo "UP-TO-DATE: LLVM"
fi

if [ "$ALIVE2_CURRENT" != "$ALIVE2_LATEST" ]; then
    echo "BUILD: alive2 $ALIVE2_CURRENT → $ALIVE2_LATEST"
    if checkout_and_build_alive2 "$ALIVE2_LATEST"; then
        update_known_hash alive2 "$ALIVE2_LATEST"
        ALIVE2_CURRENT=$ALIVE2_LATEST
    else
        echo "FAIL: alive2 build, rolling back"
        rollback_alive2
        exit 2
    fi
else
    echo "UP-TO-DATE: alive2"
fi

if [ "$LLUBI_CURRENT" != "$LLUBI_LATEST" ]; then
    echo "BUILD: llubi $LLUBI_CURRENT → $LLUBI_LATEST"
    if checkout_and_build_llubi "$LLUBI_LATEST"; then
        update_known_hash llubi "$LLUBI_LATEST"
        LLUBI_CURRENT=$LLUBI_LATEST
    else
        echo "FAIL: llubi build, rolling back"
        rollback_llubi
        exit 2
    fi
else
    echo "UP-TO-DATE: llubi"
fi

record_known_good
echo "OK: all tools updated"
