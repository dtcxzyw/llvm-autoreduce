#!/usr/bin/env bash
set -euo pipefail

SKIP_GIT=false
if [[ "${1:-}" == "--skip-git" ]]; then
    SKIP_GIT=true
fi

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
    hash=$(jq -r '.llvm' "$KNOWN_GOOD_FILE")
    checkout_and_build_llvm "$hash" || { echo "FATAL: llvm rollback build failed"; exit 1; }
}

rollback_alive2() {
    echo "ROLLBACK: restoring known-good alive2"
    local hash
    hash=$(jq -r '.alive2' "$KNOWN_GOOD_FILE")
    checkout_and_build_alive2 "$hash" || echo "WARN: alive2 rollback build failed"
}

rollback_llubi() {
    echo "ROLLBACK: restoring known-good llubi"
    local hash
    hash=$(jq -r '.llubi' "$KNOWN_GOOD_FILE")
    checkout_and_build_llubi "$hash" || echo "WARN: llubi rollback build failed"
}

update_known_hash() {
    local component="$1" hash="$2"
    if [ -f "$KNOWN_GOOD_FILE" ]; then
        jq --arg comp "$component" --arg hash "$hash" \
            '.[$comp] = $hash' "$KNOWN_GOOD_FILE" > "$KNOWN_GOOD_FILE.tmp" \
            && mv "$KNOWN_GOOD_FILE.tmp" "$KNOWN_GOOD_FILE"
    else
        echo "{\"$component\": \"$hash\"}" | \
            jq --arg comp "$component" --arg hash "$hash" \
                '.[$comp] = $hash' > "$KNOWN_GOOD_FILE"
    fi
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
# ACCEPTED RISK (F49): Repositories are cloned and always built from the
# latest origin/main (or origin/master) HEAD. There is no commit hash
# pinning, GPG signature verification, or version-lock mechanism. The
# .known-good file stores hashes only for local rollback on build failure,
# not as a trust anchor. If any upstream repository (llvm-project, alive2,
# llvm-ub-aware-interpreter) is compromised, malicious code enters the
# toolchain and is executed by both the daemon's subprocess calls and the
# AI agents' unrestricted bash access. The dockerized runtime and operator
# trust in upstream maintainers are the sole mitigations. This is the
# FINAL design decision — automatic updates are preferred over version
# locking.
if ! $SKIP_GIT; then
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
    # NOTE: branch names are hardcoded (main/master). If an upstream repo
    # renames its default branch, the clone+fetch logic must be updated here
    # and in the clone commands above.

    git -C "$WORK_DIR/llvm-trunk" fetch origin main
    git -C "$WORK_DIR/alive2-trunk" fetch origin master
    git -C "$WORK_DIR/llubi-trunk" fetch origin main
fi

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

# ---- attempt incremental update (all-or-nothing triple rollback) ----
# Build all changed components. If any build fails, roll back the entire
# triple (LLVM, alive2, llubi) to the last known-good release to preserve
# ABI compatibility across the toolchain.

NEED_LLVM=false
NEED_ALIVE2=false
NEED_LLUBI=false
[ "$LLVM_CURRENT" != "$LLVM_LATEST" ] && NEED_LLVM=true
[ "$ALIVE2_CURRENT" != "$ALIVE2_LATEST" ] && NEED_ALIVE2=true
[ "$LLUBI_CURRENT" != "$LLUBI_LATEST" ] && NEED_LLUBI=true

FAILED=false

if $NEED_LLVM; then
    echo "BUILD: LLVM $LLVM_CURRENT → $LLVM_LATEST"
    if checkout_and_build_llvm "$LLVM_LATEST"; then
        echo "OK: LLVM"
    else
        echo "FAIL: LLVM build"
        FAILED=true
    fi
fi

if $NEED_ALIVE2 && ! $FAILED; then
    echo "BUILD: alive2 $ALIVE2_CURRENT → $ALIVE2_LATEST"
    if checkout_and_build_alive2 "$ALIVE2_LATEST"; then
        echo "OK: alive2"
    else
        echo "FAIL: alive2 build"
        FAILED=true
    fi
fi

if $NEED_LLUBI && ! $FAILED; then
    echo "BUILD: llubi $LLUBI_CURRENT → $LLUBI_LATEST"
    if checkout_and_build_llubi "$LLUBI_LATEST"; then
        echo "OK: llubi"
    else
        echo "FAIL: llubi build"
        FAILED=true
    fi
fi

if $FAILED; then
    echo "ROLLBACK: restoring known-good triple"
    rollback_llvm
    rollback_alive2
    rollback_llubi
    exit 2
fi

# All updated components built successfully — record new triple.
if $NEED_LLVM; then
    update_known_hash llvm "$LLVM_LATEST"
fi
if $NEED_ALIVE2; then
    update_known_hash alive2 "$ALIVE2_LATEST"
fi
if $NEED_LLUBI; then
    update_known_hash llubi "$LLUBI_LATEST"
fi
echo "OK: all tools updated"
