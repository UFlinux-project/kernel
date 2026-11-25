#!/usr/bin/env bash
set -euo pipefail

case "${1:?Specify a kernel target}" in
  orangepi-rv2)
    sudo apt-get update
    if apt-cache show qemu-user-static 2>/dev/null | grep '^Package: qemu-user-static$' >/dev/null; then
      sudo apt-get install -y qemu-user-static binfmt-support
    else
      sudo apt-get install -y qemu-user-binfmt
    fi
    if [[ ! -r /proc/sys/fs/binfmt_misc/qemu-riscv64 ]]; then
      sudo systemctl restart systemd-binfmt
    fi
    grep -Eq '^flags:.*F' /proc/sys/fs/binfmt_misc/qemu-riscv64
    ;;
  aarch64|virt-aarch64)
    sudo apt-get update
    sudo apt-get install -y qemu-system-arm
    ;;
  x86_64|virt-x86_64)
    sudo apt-get update
    sudo apt-get install -y qemu-system-x86
    ;;
esac
