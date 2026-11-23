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
{
    IFS= read -r mode
    IFS= read -r event
} </etc/uflinux-profile
case "$mode" in
    autofdo) set -- -e "${event:?Missing AutoFDO perf event}" -c 500009 -N -j any,k ;;
    bolt) set -- -e cycles:k -F 1000 -j any,k ;;
    *) exit 2 ;;
esac
perf record -a "$@" -o /kernel.perf.data -- "$0" workload
if ! perf script -i /kernel.perf.data -F brstack | awk 'NF { samples++ } END { exit !samples }'; then
    echo "perf did not produce readable branch samples for $mode" >&2
    exit 1
fi
sync
echo UFLINUX_PROFILE_OK
trap - EXIT
exec poweroff -f
