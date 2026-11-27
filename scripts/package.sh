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
modules_package="$package-modules"
release="$version-r$revision"
abi=$(find /out/stage/lib/modules -mindepth 1 -maxdepth 1 -type d -printf '%f\n')
# The module tree is its own package, so a guest only pays for the drivers it needs.
mkdir -p /out/modules-root/lib
mv /out/stage/lib/modules /out/modules-root/lib/modules
/out/apk-zstd mkpkg --xattrs=no --files /out/stage --compression zstd:9 --sign-key "$key" \
    --info "name:$package" --info "version:$release" --info "arch:$architecture" \
    --info "description:UFlinux kernel for $name" --info "license:GPL-2.0-only" \
    --info "url:https://github.com/UFlinux-project/kernel" \
    --info "depends:mkinitfs kmod $modules_package=$release" \
    --output "/out/$package-$release.apk"
/out/apk-zstd mkpkg --xattrs=no --files /out/modules-root --compression zstd:9 --sign-key "$key" \
    --info "name:$modules_package" --info "version:$release" --info "arch:$architecture" \
    --info "description:Loadable kernel modules for the UFlinux $name kernel" \
    --info "license:GPL-2.0-only" \
    --info "url:https://github.com/UFlinux-project/kernel" --info "depends:kmod" \
    --output "/out/$modules_package-$release.apk"
mv /out/modules-root/lib/modules /out/stage/lib/modules
rm -rf /out/modules-root
cp "$key.pub" /out/

# shellcheck source=scripts/guest-root.sh
. "$(dirname "$0")/guest-root.sh"
install_guest_root "$architecture"
# Run test-root tools in their own root, including apk's kernel triggers.
i=0
for apk in /out/*.apk; do
    i=$((i + 1))
    cp "$apk" "/guest/test-kernel-$i.apk"
done
test "$i" = 2
cp /out/apk-zstd /guest/tmp/apk-zstd
chroot /guest /tmp/apk-zstd add /test-kernel-1.apk /test-kernel-2.apk
rm /guest/test-kernel-*.apk /guest/tmp/apk-zstd
for path in "boot/vmlinuz-uflinux-$name" "boot/initramfs-uflinux-$name"; do
    test -s "/guest/$path"
done
map="boot/System.map-uflinux-$name"
test -s "/guest/$map"
cmp "/out/stage/$map" "/guest/$map"
abi=$(find /out/stage/lib/modules -mindepth 1 -maxdepth 1 -type d -printf '%f\n')
test -s "/guest/lib/modules/$abi/modules.dep"
module=$(find "/guest/lib/modules/$abi" -type f -name '*.ko*' -print -quit)
test -n "$module"
test "$(modinfo -F vermagic "$module" | cut -d ' ' -f1)" = "$abi"
# A module with no dependencies can be loaded on its own, which is what proves the kernel
# accepted its signature at runtime rather than just carrying the right modinfo fields.
# Prefer a software-only crypto or library module: those never need hardware to load.
depfile="/guest/lib/modules/$abi/modules.dep"
sigmodule=$(awk -F: '$2 == "" && $1 ~ /^kernel\/(crypto|lib)\// {print $1; exit}' "$depfile")
test -n "$sigmodule" || sigmodule=$(awk -F: '$2 == "" && $1 ~ /\.ko/ {print $1; exit}' "$depfile")
sigmodule=${sigmodule##*/}
sigmodule=${sigmodule%%.ko*}
sigmodule=${sigmodule//-/_}
if [ -s "/out/stage/boot/module-cert-uflinux-$name.x509" ]; then
    # The signer field is the subject commonName of the embedded certificate.
    signer=$(openssl x509 -in "/out/stage/boot/module-cert-uflinux-$name.x509" -noout -subject -nameopt multiline,utf8 \
        | sed -n 's/^[[:space:]]*commonName[[:space:]]*=[[:space:]]*//p')
    test -n "$signer"
    test "$(modinfo -F signer "$module")" = "$signer"
fi
cp "/guest/boot/initramfs-uflinux-$name" /out/initramfs
chmod 644 /out/initramfs
/out/apk-zstd verify /out/*.apk

case "$name" in
  x86_64|aarch64|virt-*) ;;
  *) exit 0;;
esac
if [ -n "$profile_guest" ]; then
    apk --root /guest add --no-progress fio iperf3
    install -m 755 "$(dirname "$0")/profile-guest.sh" /guest/sbin/uflinux-profile
fi
python3 - /out/build.json "$sigmodule" <<'PY'
import json, sys
from pathlib import Path
meta = json.loads(Path(sys.argv[1]).read_text())
sigmodule = sys.argv[2] if len(sys.argv) > 2 else ''
script = '''#!/bin/sh
set -eu
grep -qs " /proc proc " /proc/mounts || mount -t proc proc /proc
grep -qs " /sys sysfs " /proc/mounts || mount -t sysfs sysfs /sys
test "$(uname -r)" = "@ABI@"
test "$(cat /proc/sys/net/ipv4/tcp_congestion_control)" = "@BBR@"
ip link set lo up
# Hold the loopback connection open: ss only lists ESTABLISHED sockets, and with
# /dev/zero on both ends the pair can be gone before it runs. The fifo keeps the
# server blocked (and the connection alive) for the length of the check.
mkfifo /dev/bbr-fifo
( sleep 15; echo ) > /dev/bbr-fifo &
holder=$!
nc -l -p 19000 < /dev/bbr-fifo >/dev/null &
server=$!
nc 127.0.0.1 19000 </dev/zero >/dev/null &
client=$!
sleep 1
ss -tin | grep -qw @BBR@
kill "$server" "$client" "$holder" 2>/dev/null || :
rm -f /dev/bbr-fifo
@MGLRU@
@LE9UO@
modprobe virtio_net || test -d /sys/class/net/eth0
@SIGMODULE@
@TAINT@
ip link set eth0 up
ip addr add 10.0.2.15/24 dev eth0
ping -c 1 -W 3 10.0.2.2
test -b /dev/vda
echo UFLINUX_BOOT_OK
poweroff -f
'''
script = script.replace("@ABI@", meta["abi"]).replace("@BBR@", meta["bbrv3"])
# Loading a signed module and then checking the taint word is the only end-to-end proof
# that the embedded key verifies the signatures the build attached.
script = script.replace("@SIGMODULE@", f'modprobe {sigmodule}\ngrep -q "^{sigmodule} " /proc/modules'
                        if meta.get("module_signing_key") and sigmodule else ':')
# Bit 13 is TAINT_UNSIGNED_MODULE. Only an actually signed kernel can be held to it.
script = script.replace("@TAINT@", 'test "$(( $(cat /proc/sys/kernel/tainted) & 8192 ))" = 0'
                        if meta.get("module_signing_key") else ':')
script = script.replace("@MGLRU@", 'test "$(( $(cat /sys/kernel/mm/lru_gen/enabled) & 1 ))" = 1' if meta["mglru"] else ':')
script = script.replace("@LE9UO@", """test -r /proc/sys/vm/workingset_protection
mount -o remount,rw /
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
