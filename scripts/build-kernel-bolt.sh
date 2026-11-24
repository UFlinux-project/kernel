#!/usr/bin/env bash
set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
output=$(realpath -m "${1:?Specify the BOLT installation directory}")
version=22.1.2
llvm_config=llvm-config-22
archive=llvm-project-$version.src.tar.xz
sha256=62f2f13ff25b1bb28ea507888e858212d19aafb65e8e72b4a65ee0629ec4ae0c

if [[ $("$llvm_config" --version) != "$version" ]]; then
    echo "Building the kernel rewriter requires LLVM $version development files" >&2
    exit 1
fi

workspace=$(mktemp -d)
trap 'rm -rf -- "$workspace"' EXIT
curl -fsSL --retry 3 "https://github.com/llvm/llvm-project/releases/download/llvmorg-$version/$archive" \
    -o "$workspace/$archive"
printf '%s  %s\n' "$sha256" "$workspace/$archive" | sha256sum -c
tar -xJf "$workspace/$archive" -C "$workspace" --strip-components=1 --wildcards \
    '*/bolt' '*/cmake' '*/llvm/cmake' '*/llvm/include' '*/llvm/lib/Target/X86'
git -C "$workspace" apply "$repo/patches/llvm-bolt/linux-kernel.patch"

cmake -S "$workspace/bolt" -B "$workspace/build" -G Ninja \
    -DLLVM_DIR="$("$llvm_config" --cmakedir)" -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_C_COMPILER=clang-22 -DCMAKE_CXX_COMPILER=clang++-22 \
    -DLLVM_INCLUDE_TESTS=OFF -DLLVM_LINK_LLVM_DYLIB=ON -DBOLT_ENABLE_RUNTIME=OFF \
    -DBOLT_TARGETS_TO_BUILD=X86
cmake --build "$workspace/build" --target llvm-bolt --parallel "${UFLINUX_BOLT_JOBS:-4}"
install -Dm755 "$workspace/build/bin/llvm-bolt" "$output/llvm-bolt"
"$output/llvm-bolt" --version
