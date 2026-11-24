#!/usr/bin/env bash
set -euo pipefail

target=$1
output=$2
repo=$(cd "$(dirname "$0")/.." && pwd)
architecture=$(jq -er --arg target "$target" '.[$target].apk' "$repo/targets.json")
case "$architecture" in
    x86_64|aarch64) ;;
    *) exit 0 ;;
esac
binary=${UFLINUX_BOLT_BINARY:-}
if [[ ${3:-} == --bundle ]]; then
    : "${binary:?Set UFLINUX_BOLT_BINARY when bundling a rewriter}"
else
    test -s "${3:-profiles/$target}/kernel.fdata" || exit 0
fi
if [[ $(uname -m) != "$architecture" ]]; then
    echo "Preparing LLVM BOLT for $target requires a native $architecture host" >&2
    exit 1
fi

if [[ -z "$binary" ]]; then
    binary=$(command -v llvm-bolt || true)
fi
if [[ -z "$binary" ]]; then
    echo "Set UFLINUX_BOLT_BINARY to an llvm-bolt with $architecture Linux kernel rewrite support" >&2
    exit 1
fi

# Bundle the optimizer with its host loader and libraries for use under Alpine.
interpreter=$(readelf -lW "$binary" | awk '/Requesting program interpreter:/ { sub(/]$/, "", $NF); print $NF }')
mkdir -p "$output/bolt"
if [[ -z "$interpreter" ]]; then
    install -m 755 "$binary" "$output/bolt/llvm-bolt"
else
    mkdir -p "$output/bolt/lib"
    cp -L "$binary" "$output/bolt/lib/optimizer"
    cp -L "$interpreter" "$output/bolt/lib/loader"
    ldd "$binary" | awk '{for(i=1;i<=NF;i++) if($i~/^\//) {print $i; break}}' |
        while IFS= read -r library; do cp -L "$library" "$output/bolt/lib/"; done
    cat > "$output/bolt/llvm-bolt" <<'WRAPPER'
#!/bin/sh
set -eu
lib=${0%/*}/lib
exec "$lib/loader" --library-path "$lib" "$lib/optimizer" "$@"
WRAPPER
    chmod +x "$output/bolt/llvm-bolt"
fi
"$output/bolt/llvm-bolt" --version
