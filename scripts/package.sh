#!/bin/sh
set -eu

name=$1
version=$2
revision=$3
architecture=$4
profile_guest=${5:-}
apk add --no-progress openssl kmod mkinitfs e2fsprogs iproute2 findutils
keydir=$(mktemp -d)
trap 'rm -rf "$keydir"' EXIT
key="$keydir/uflinux-build-$name.ec.pem"
openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:secp384r1 -out "$key"
openssl pkey -in "$key" -pubout -out "$key.pub"
cp "$key.pub" /etc/apk/keys/
package="linux-uflinux-$name"
release="$version-r$revision"
/out/apk-zstd mkpkg --xattrs=no --files /out/stage --compression zstd:9 --sign-key "$key" \
    --info "name:$package" --info "version:$release" --info "arch:$architecture" \
    --info "description:UFlinux kernel for $name" --info "license:GPL-2.0-only" \
    --info "url:https://github.com/UFlinux-project/kernel" --info "depends:mkinitfs kmod" \
    --output "/out/$package-$release.apk"
cp "$key.pub" /out/

# shellcheck source=scripts/guest-root.sh
. "$(dirname "$0")/guest-root.sh"
install_guest_root "$architecture"
# Run test-root tools in their own root, including apk's kernel triggers.
cp /out/*.apk /guest/test-kernel.apk
cp /out/apk-zstd /guest/tmp/apk-zstd
chroot /guest /tmp/apk-zstd add /test-kernel.apk
rm /guest/test-kernel.apk /guest/tmp/apk-zstd
for path in "boot/vmlinuz-uflinux-$name" "boot/initramfs-uflinux-$name"; do
    test -s "/guest/$path"
done
abi=$(find /out/stage/lib/modules -mindepth 1 -maxdepth 1 -type d -printf '%f\n')
test -s "/guest/lib/modules/$abi/modules.dep"
module=$(find "/guest/lib/modules/$abi" -type f -name '*.ko*' -print -quit)
test -n "$module"
test "$(modinfo -F vermagic "$module" | cut -d ' ' -f1)" = "$abi"
cp "/guest/boot/initramfs-uflinux-$name" /out/initramfs
chmod 644 /out/initramfs
/out/apk-zstd verify /out/*.apk

case "$name" in
  x86|x86_64|aarch64|virt-*) ;;
  *) exit 0;;
esac
if [ -n "$profile_guest" ]; then
    apk --root /guest add --no-progress perf fio iperf3
    install -m 755 "$(dirname "$0")/profile-guest.sh" /guest/sbin/uflinux-profile
    printf '%s\n' "$profile_guest" "${6:-}" > /guest/etc/uflinux-profile
fi
python3 - /out/build.json <<'PY'
import json, sys
from pathlib import Path
meta = json.loads(Path(sys.argv[1]).read_text())
script = '''#!/bin/sh
set -eu
grep -qs " /proc proc " /proc/mounts || mount -t proc proc /proc
grep -qs " /sys sysfs " /proc/mounts || mount -t sysfs sysfs /sys
test "$(uname -r)" = "@ABI@"
test "$(cat /proc/sys/net/ipv4/tcp_congestion_control)" = "@BBR@"
ip link set lo up
nc -l -p 19000 </dev/zero >/dev/null &
server=$!
nc 127.0.0.1 19000 </dev/zero >/dev/null &
client=$!
sleep 1
ss -tin | grep -w @BBR@
kill "$server" "$client" || :
@MGLRU@
@LE9UO@
modprobe virtio_net || test -d /sys/class/net/eth0
ip link set eth0 up
ip addr add 10.0.2.15/24 dev eth0
ping -c 1 -W 3 10.0.2.2
test -b /dev/vda
echo UFLINUX_BOOT_OK
poweroff -f
'''
script = script.replace("@ABI@", meta["abi"]).replace("@BBR@", meta["bbrv3"])
script = script.replace("@MGLRU@", 'test "$(( $(cat /sys/kernel/mm/lru_gen/enabled) & 1 ))" = 1' if meta["mglru"] else ':')
script = script.replace("@LE9UO@", """test -r /proc/sys/vm/workingset_protection
mount -t cgroup2 none /sys/fs/cgroup
echo +memory >/sys/fs/cgroup/cgroup.subtree_control
mkdir -p /sys/fs/cgroup/reclaim
echo 67108864 >/sys/fs/cgroup/reclaim/memory.max
timeout 30 sh -ec '
    echo "$$" >/sys/fs/cgroup/reclaim/cgroup.procs
    dd if=/dev/zero of=/reclaim-test bs=1M count=96
    dd if=/reclaim-test of=/dev/null bs=1M
    rm /reclaim-test
'
grep -qx 'oom_kill 0' /sys/fs/cgroup/reclaim/memory.events
rmdir /sys/fs/cgroup/reclaim
""" if "source" in meta["patches"]["le9uo"] else ':')
path = Path("/guest/sbin/uflinux-test")
path.write_text(script)
path.chmod(0o755)
PY
truncate -s 640M /out/boot-test.ext4
mkfs.ext4 -q -F -d /guest /out/boot-test.ext4
