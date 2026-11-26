#!/usr/bin/env bash
set -euo pipefail

target=${1:?Specify a target}
output=${2:?Specify the output directory}
test -f "$output/boot-test.ext4" || exit 0

kernel="$output/stage/boot/vmlinuz-uflinux-$target"

# shellcheck source=scripts/kernel-image.sh
. "$(dirname "$0")/kernel-image.sh"

case "$target" in
    aarch64|virt-aarch64)
        emulator=qemu-system-aarch64
        machine=virt
        cpu=cortex-a57
        serial=ttyAMA0
        decompress_kernel "$kernel" >"$output/boot-test.Image"
        kernel="$output/boot-test.Image"
        ;;
    *)
        emulator=qemu-system-x86_64
        machine=q35
        cpu=qemu64
        serial=ttyS0
        ;;
esac

accelerator=tcg
if [[ -r /dev/kvm && -w /dev/kvm ]]; then
    accelerator=kvm
    cpu=host
fi

timeout 240 "$emulator" -machine "$machine,accel=$accelerator" -cpu "$cpu" -m 1024 \
    -nographic -no-reboot -monitor none -kernel "$kernel" -initrd "$output/initramfs" \
    -drive "if=virtio,format=raw,snapshot=on,file=$output/boot-test.ext4" \
    -nic user,model=virtio-net-pci \
    -append "console=$serial root=/dev/vda rootfstype=ext4 rw init=/sbin/uflinux-test panic=1" \
    2>&1 | tee "$output/boot-test.log"
grep -q '^UFLINUX_BOOT_OK' "$output/boot-test.log"
