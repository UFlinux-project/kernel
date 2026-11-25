#!/usr/bin/env bash
set -euo pipefail

resolved=$(realpath "${1:?Specify resolved inputs}")
profiles=$(realpath -m "${2:?Specify the profile output directory}")
target=${3:?Specify a target}
mode=${4:?Specify pgo or bolt}
output=$(realpath -e "${5:?Specify the compiled kernel directory}")
bolt=${6:-}
case "$mode" in
    pgo) jq -e '.pgo_instrumented' "$output/build.json" >/dev/null ;;
    bolt) test -x "$bolt"; jq -e '.pgo and (.pgo_instrumented | not)' "$output/build.json" >/dev/null ;;
    *) exit 2 ;;
esac
case "$target" in
    x86_64|virt-x86_64)
        emulator=qemu-system-x86_64
        machine=q35
        serial=ttyS0
        ;;
    aarch64|virt-aarch64)
        emulator=qemu-system-aarch64
        machine=virt
        serial=ttyAMA0
        ;;
    *)
        echo "No QEMU profiling machine configured for $target" >&2
        exit 1
        ;;
esac

workspace=$(mktemp -d "$(dirname "$resolved")/profile.XXXXXX")
trap 'rm -rf -- "$workspace"' EXIT
mkdir -p "$profiles"

kernel="$output/stage/boot/vmlinuz-uflinux-$target"
if [[ "$machine" == virt ]]; then
    gzip -dc "$kernel" > "$workspace/Image"
    kernel="$workspace/Image"
fi
guest=("$emulator" -machine "$machine,accel=tcg" -cpu max -smp 1 -m 1536
       -nographic -monitor none -no-reboot -nic none -kernel "$kernel" -initrd "$output/initramfs"
       -drive "if=virtio,format=raw,snapshot=on,file=$output/boot-test.ext4")
cmdline="nokaslr console=$serial root=/dev/vda rootfstype=ext4 rw init=/sbin/uflinux-profile panic=1"
if [[ "$mode" == pgo ]]; then
    symbols=$(nm "$output/vmlinux")
    start=$(awk '$3 == "__uflinux_pgo_counters" {print $1}' <<< "$symbols")
    end=$(awk '$3 == "__uflinux_pgo_counters_end" {print $1}' <<< "$symbols")
    if [[ -z "$start" || -z "$end" ]]; then
        echo "Missing instrumentation counters in the collection ELF" >&2
        exit 1
    fi
    bytes=$(( 16#$end - 16#$start ))
    test "$bytes" -gt 0
    for snapshot in before after; do
        rm -f "$output/pgo-$snapshot"
        truncate -s "$(( (bytes + 511) / 512 * 512 ))" "$output/pgo-$snapshot"
        guest+=(-drive "if=virtio,format=raw,file=$output/pgo-$snapshot")
    done
    timeout 600 "${guest[@]}" -append "$cmdline uflinux.pgo=1" 2>&1 | tee "$workspace/pgo.log"
    grep -q '^UFLINUX_PROFILE_OK' "$workspace/pgo.log"
    exit 0
fi

qemu_version=$("$emulator" -version | awk 'NR == 1 {print $4}')
case "$qemu_version" in
    10.2.*)
        header=include/qemu/qemu-plugin.h
        ref=v10.2.0
        sha256=aebc639d01685893a9d71b9ef3afa7ea67e0be6a8959781c4057bd4974d05d4d
        ;;
    11.0.*)
        header=include/plugins/qemu-plugin.h
        ref=v11.0.0
        sha256=9592f519d7344b1e38b0123b92c28c4e688faedf41263984910353736bb0b7be
        ;;
    *)
        echo "Unsupported QEMU plugin API for QEMU $qemu_version" >&2
        exit 1
        ;;
esac
curl -fsSL --retry 3 "https://raw.githubusercontent.com/qemu/qemu/$ref/$header" -o "$workspace/qemu-plugin.h"
printf '%s  %s\n' "$sha256" "$workspace/qemu-plugin.h" | sha256sum -c
read -ra glib_flags <<< "$(pkg-config --cflags --libs glib-2.0)"
cc -O2 -Wall -Wextra -Werror -shared -fPIC -I"$workspace" \
    scripts/profile-tcg.c -o "$workspace/profile-tcg.so" "${glib_flags[@]}"

text_start=$(nm "$output/vmlinux" | awk '$3 == "_stext" {print $1}')
text_end=$(nm "$output/vmlinux" | awk '$3 == "_etext" {print $1}')
test -n "$text_start"
test -n "$text_end"
trace="$workspace/$mode.trace"
marker="$workspace/$mode.start"
# Use the ELF's addresses directly and collect only after guest setup.
timeout 600 "${guest[@]}" \
    -plugin "$workspace/profile-tcg.so,start=$text_start,end=$text_end,output=$trace,marker=$marker" \
    -append "$cmdline" \
    2>&1 | awk -v marker="$marker" '
        { print; fflush() }
        /^UFLINUX_PROFILE_START/ { print 1 > marker; close(marker) }
    ' | tee "$workspace/$mode.log"
grep -q '^UFLINUX_PROFILE_OK' "$workspace/$mode.log"
grep -q '^F ' "$trace"
grep -q '^B ' "$trace"
LC_ALL=C sort "$trace" -o "$trace"

manifest="$output/build.json"
profile="$profiles/kernel.fdata"
# The BOLT format also has a misprediction count for each branch.
awk '$1 == "B" {print $0 " 0"; next} {print}' "$trace" > "$workspace/kernel.preaggregated"
bolt_options=()
if [[ "$machine" == q35 ]]; then
    bolt_options+=("--linux-bug-entry-size=$(jq -er '.bolt_bug_entry_size' "$manifest")")
fi
"$bolt" --aggregate-only --pa --relocs=0 "${bolt_options[@]}" \
    -p "$workspace/kernel.preaggregated" -o "$workspace/kernel.fdata" "$output/vmlinux" \
    2>&1 | tee "$workspace/bolt-conversion.log"
grep -Fxq 'BOLT-INFO: Linux kernel binary detected' "$workspace/bolt-conversion.log"
test -s "$workspace/kernel.fdata"
mv "$workspace/kernel.fdata" "$profile"
build_id=$(readelf -n "$output/vmlinux" | awk '/Build ID:/ {print $3; exit}')
test -n "$build_id"
rewriter=$bolt
if [[ -f "${bolt%/*}/lib/optimizer" ]]; then
    rewriter="${bolt%/*}/lib/optimizer"
fi
jq --arg sha256 "$(sha256sum "$profile" | cut -d' ' -f1)" --arg build_id "$build_id" \
    --arg input_sha256 "$(sha256sum "$output/vmlinux" | cut -d' ' -f1)" \
    --arg rewriter_sha256 "$(sha256sum "$rewriter" | cut -d' ' -f1)" \
    '{source, build_id: $build_id, input_sha256: $input_sha256, sha256: $sha256, rewriter_sha256: $rewriter_sha256}' \
    "$manifest" > "$profiles/bolt.json"
