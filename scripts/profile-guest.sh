#!/bin/sh
# shellcheck shell=busybox
set -eu
set -o pipefail
export PATH=/sbin:/usr/sbin:/bin:/usr/bin

if [ "${1:-}" = workload ]; then
    ip link set lo up
    iperf3 -s -1 >/tmp/iperf-server.log 2>&1 &
    server=$!
    trap 'kill "$server" 2>/dev/null || true' EXIT
    sleep 1
    fio --name=guest-io --filename=/profile-io --size=64M --rw=randrw \
        --bs=4k --direct=1 --time_based --runtime=30 >/tmp/fio.log &
    storage=$!
    iperf3 -c 127.0.0.1 -t 30
    wait "$storage"
    wait "$server"
    rm /profile-io
    exit 0
fi

profile_failed() {
    echo "UFLINUX_PROFILE_FAILED (exit $1)" >&2
    poweroff -f
}
trap 'profile_failed "$?"' EXIT
grep -qs ' /proc proc ' /proc/mounts || mount -t proc proc /proc
grep -qs ' /sys sysfs ' /proc/mounts || mount -t sysfs sysfs /sys
mount -o remount,rw /
pgo=false
if grep -qw uflinux.pgo=1 /proc/cmdline; then
    pgo=true
    test -r /proc/uflinux-pgo-counters
    cat /proc/uflinux-pgo-counters > /dev/vdb
fi
echo UFLINUX_PROFILE_START
"$0" workload
if "$pgo"; then
    cat /proc/uflinux-pgo-counters > /dev/vdc
fi
sync
echo UFLINUX_PROFILE_OK
trap - EXIT
exec poweroff -f
