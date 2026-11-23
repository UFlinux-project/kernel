#!/usr/bin/env bash
set -euo pipefail

resolved=$(realpath "${1:?Specify resolved inputs}")
profiles=$(realpath -m "${2:?Specify the profile output directory}")
profgen=${3:?Specify llvm-profgen}
perf2bolt=${4:?Specify perf2bolt}
target=virt-x86_64

if [[ ! -c /dev/kvm || ! -r /dev/kvm || ! -w /dev/kvm ]]; then
    echo "Kernel profiling requires read and write access to the /dev/kvm device" >&2
    exit 1
fi
if ! printf '%s\n' '{"execute":"qmp_capabilities"}' '{"execute":"query-kvm"}' '{"execute":"quit"}' |
    timeout 15 qemu-system-x86_64 -machine q35,accel=kvm -cpu host,pmu=on \
        -S -nodefaults -display none -m 128 -qmp stdio |
    jq -es 'any(.[]; .return.enabled? == true)' >/dev/null; then
    echo "QEMU could not initialize KVM on this profiling runner" >&2
    exit 1
fi

workspace=$(mktemp -d "$(dirname "$resolved")/profile.XXXXXX")
trap 'sudo rm -rf -- "$workspace"' EXIT
mkdir -p "$profiles"
export KERNEL_AUTOFDO_EVENT=${KERNEL_AUTOFDO_EVENT:-br_inst_retired.near_taken:k}

for mode in autofdo bolt; do
    output="$workspace/$mode"
    mkdir -p "$output/profiles"
    if [[ "$mode" == bolt ]]; then
        cp "$profiles/kernel.afdo" "$profiles/autofdo.json" "$output/profiles/"
    fi
    UFLINUX_PROFILE_GUEST="$mode" scripts/container.sh "$target" "$resolved" "$output" --profile-build
    grep -qx CONFIG_AUTOFDO_CLANG=y "$output/kernel.config"
    if [[ "$mode" == bolt ]]; then
        jq -e '.autofdo == true' "$output/build.json" >/dev/null
    fi

    disk="$workspace/guest.ext4"
    cp --sparse=always "$output/boot-test.ext4" "$disk"
    chmod u+w "$disk"
    timeout 300 qemu-system-x86_64 -machine q35,accel=kvm -cpu host,pmu=on \
        -smp 2 -m 1536 -nographic -monitor none -no-reboot -nic none \
        -kernel "$output/stage/boot/vmlinuz-uflinux-$target" -initrd "$output/initramfs" \
        -drive "if=virtio,format=raw,file=$disk" \
        -append "console=ttyS0 root=/dev/vda rootfstype=ext4 rw init=/sbin/uflinux-profile panic=1" \
        2>&1 | tee "$workspace/guest.log"
    if ! grep -q '^UFLINUX_PROFILE_OK' "$workspace/guest.log"; then
        echo "The guest did not complete profiling. Check perf errors and guest PMU support." >&2
        exit 1
    fi
    data="$workspace/kernel.perf.data"
    debugfs -R "dump /kernel.perf.data $data" "$disk"
    test -s "$data"

    manifest="$output/build.json"
    if [[ "$mode" == autofdo ]]; then
        profile="$profiles/kernel.afdo"
        "$profgen" --kernel --binary="$output/vmlinux" --perfdata="$data" -o "$profile"
        test -s "$profile"
        jq --arg sha256 "$(sha256sum "$profile" | cut -d' ' -f1)" \
            '{source, config_sha256, sha256: $sha256}' "$manifest" > "$profiles/autofdo.json"
    else
        profile="$profiles/kernel.fdata"
        "$perf2bolt" -p "$data" -o "$profile" "$output/vmlinux"
        test -s "$profile"
        build_id=$(readelf -n "$output/vmlinux" | awk '/Build ID:/ {print $3; exit}')
        test -n "$build_id"
        jq --arg sha256 "$(sha256sum "$profile" | cut -d' ' -f1)" --arg build_id "$build_id" \
            '{source, build_id: $build_id, sha256: $sha256}' "$manifest" > "$profiles/bolt.json"
    fi
    sudo rm -rf -- "$output"
    rm -- "$disk" "$data"
done
