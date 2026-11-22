#!/usr/bin/env bash
set -euo pipefail

target=$1
output=$2
case "$target" in
    x86_64|virt-x86_64) ;;
    *) exit 0 ;;
esac
test -s "profiles/$target/kernel.fdata" || exit 0

sudo apt-get update
package=$(apt-cache search --names-only '^bolt-[0-9]+$' | cut -d' ' -f1 | sort -V | tail -1)
test -n "$package" || exit 0
sudo apt-get install -y "$package"
binary=$(command -v "llvm-bolt-${package#bolt-}")
mkdir -p "$output/bolt/lib"
cp -L "$binary" "$output/bolt/lib/optimizer"
ldd "$binary" | awk '{for(i=1;i<=NF;i++) if($i~/^\//) {print $i; break}}' |
    while IFS= read -r library; do cp -L "$library" "$output/bolt/lib/"; done
cat > "$output/bolt/llvm-bolt" <<'WRAPPER'
#!/bin/sh
set -eu
lib=${0%/*}/lib
exec "$lib/ld-linux-x86-64.so.2" --library-path "$lib" "$lib/optimizer" "$@"
WRAPPER
chmod +x "$output/bolt/llvm-bolt"
"$output/bolt/llvm-bolt" --version
