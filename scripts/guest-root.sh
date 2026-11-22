#!/bin/sh
# Run inside the target Alpine container.
set -eu

install_guest_root() {
    architecture=$1
    mkdir -p /guest/etc/apk/keys /guest/etc/mkinitfs
    cp /etc/apk/repositories /guest/etc/apk/
    cp /etc/resolv.conf /guest/etc/
    cp /etc/apk/keys/* /guest/etc/apk/keys/
    printf '%s\n' 'features="base ext4 virtio"' >/guest/etc/mkinitfs/mkinitfs.conf
    apk --root /guest --arch "$architecture" --initdb add alpine-base iproute2 kmod
}
