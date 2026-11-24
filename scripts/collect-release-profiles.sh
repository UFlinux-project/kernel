#!/usr/bin/env bash
set -euo pipefail

resolved=$(realpath "${1:?Specify resolved inputs}")
profiles=$(realpath -m "${2:?Specify the profile output directory}")
profgen=${3:?Specify llvm-profgen}
bolt=${4?Specify llvm-bolt or pass an empty string for AutoFDO only}
profdata=${5:?Specify llvm-profdata}
target=${6:-virt-x86_64}
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
trap 'sudo rm -rf -- "$workspace"' EXIT
mkdir -p "$profiles"

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

for mode in autofdo bolt; do
    output="$workspace/$mode"
    mkdir -p "$output/profiles"
    if [[ "$mode" == bolt ]]; then
        cp "$profiles/kernel.afdo" "$profiles/autofdo.json" "$output/profiles/"
    fi
    UFLINUX_PROFILE_GUEST=1 scripts/container.sh "$target" "$resolved" "$output" --profile-build
    grep -qx CONFIG_AUTOFDO_CLANG=y "$output/kernel.config"
    if [[ "$mode" == bolt ]]; then
        jq -e '.autofdo == true' "$output/build.json" >/dev/null
    fi

    text_start=$(nm "$output/vmlinux" | awk '$3 == "_stext" {print $1}')
    text_end=$(nm "$output/vmlinux" | awk '$3 == "_etext" {print $1}')
    test -n "$text_start"
    test -n "$text_end"
    trace="$workspace/$mode.trace"
    marker="$workspace/$mode.start"
    kernel="$output/stage/boot/vmlinuz-uflinux-$target"
    if [[ "$machine" == virt ]]; then
        gzip -dc "$kernel" > "$workspace/Image"
        kernel="$workspace/Image"
    fi
    # Use the ELF's addresses directly and collect only after guest setup.
    timeout 600 "$emulator" -machine "$machine,accel=tcg" -cpu max \
        -smp 1 -m 1536 -nographic -monitor none -no-reboot -nic none \
        -plugin "$workspace/profile-tcg.so,start=$text_start,end=$text_end,output=$trace,marker=$marker" \
        -kernel "$kernel" -initrd "$output/initramfs" \
        -drive "if=virtio,format=raw,snapshot=on,file=$output/boot-test.ext4" \
        -append "nokaslr console=$serial root=/dev/vda rootfstype=ext4 rw init=/sbin/uflinux-profile panic=1" \
        2>&1 | awk -v marker="$marker" '
            { print; fflush() }
            /^UFLINUX_PROFILE_START/ { print 1 > marker; close(marker) }
        ' | tee "$workspace/$mode.log"
    grep -q '^UFLINUX_PROFILE_OK' "$workspace/$mode.log"
    grep -q '^F ' "$trace"
    grep -q '^B ' "$trace"
    LC_ALL=C sort "$trace" -o "$trace"

    manifest="$output/build.json"
    if [[ "$mode" == autofdo ]]; then
        profile="$profiles/kernel.afdo"
        # LLVM's unsymbolized format stores counted ranges and branches separately.
        awk '
            $1 == "F" { ranges[++nr] = $2 "-" $3 ":" $4 }
            $1 == "B" { branches[++nb] = $2 "->" $3 ":" $4 }
            END {
                print nr
                for (i = 1; i <= nr; ++i) print ranges[i]
                print nb
                for (i = 1; i <= nb; ++i) print branches[i]
            }
        ' "$trace" > "$workspace/kernel.unsymbolized"
        "$profgen" --kernel --use-offset=false --binary="$output/vmlinux" \
            --unsymbolized-profile="$workspace/kernel.unsymbolized" -o "$profile"
        "$profdata" merge --sample --text "$profile" -o "$workspace/kernel.afdo.txt"
        grep -Eq '^[^[:space:]].*:[1-9][0-9]*:[0-9]+$' "$workspace/kernel.afdo.txt"
        jq --arg sha256 "$(sha256sum "$profile" | cut -d' ' -f1)" \
            '{source, config_sha256, sha256: $sha256}' "$manifest" > "$profiles/autofdo.json"
        if [[ -z "$bolt" ]]; then
            break
        fi
    else
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
        jq --arg sha256 "$(sha256sum "$profile" | cut -d' ' -f1)" --arg build_id "$build_id" \
            --arg rewriter_sha256 "$(sha256sum "$(command -v "$bolt")" | cut -d' ' -f1)" \
            '{source, build_id: $build_id, sha256: $sha256, rewriter_sha256: $rewriter_sha256}' \
            "$manifest" > "$profiles/bolt.json"
    fi
    sudo rm -rf -- "$output"
done
