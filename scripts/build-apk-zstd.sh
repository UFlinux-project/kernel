#!/bin/sh
set -eu

aports_ref=$1
series=$2
destination=$3
test -n "$aports_ref"
tmp=$(mktemp -d)
trap 'rm -r "$tmp"' EXIT

apk add --no-progress build-base curl meson linux-headers openssl-dev openssl-libs-static \
    zlib-dev zlib-static zstd-dev zstd-static
base="https://raw.githubusercontent.com/alpinelinux/aports/$aports_ref/main/apk-tools"
download() {
    printf 'Fetching %s\n' "$1"
    curl -fsSL --retry 3 "$1" -o "$2"
}

check_source() {
    digest=$(awk -v file="$1" '$2 == file && length($1) == 128 { print $1 }' "$tmp/APKBUILD")
    if [ -z "$digest" ]; then
        printf 'Missing Alpine SHA-512 checksum for %s\n' "$1" >&2
        return 1
    fi
    printf '%s  %s\n' "$digest" "$tmp/$1" | sha512sum -c
}

download "$base/APKBUILD" "$tmp/APKBUILD"
version=$(sed -n 's/^pkgver=\([0-9.]*\)$/\1/p' "$tmp/APKBUILD")
test -n "$version"
archive="apk-tools-v$version.tar.gz"
if ! (download "https://distfiles.alpinelinux.org/distfiles/$series/$archive" "$tmp/$archive" &&
      check_source "$archive"); then
    download "https://gitlab.alpinelinux.org/alpine/apk-tools/-/archive/v$version/$archive" "$tmp/$archive"
    check_source "$archive"
fi
download "$base/repo-hash-sha1.patch" "$tmp/repo-hash-sha1.patch"
check_source repo-hash-sha1.patch
(
    cd "$tmp"
    tar xzf "$archive"
    cd "apk-tools-v$version"
    patch -p1 <../repo-hash-sha1.patch
    VERSION="$version" meson setup output --buildtype=release --auto-features=disabled \
        -Darch="$(apk --print-arch)" -Dzstd=enabled -Ddefault_library=static \
        -Dprefer_static=true -Dc_link_args=-static
    ninja -C output -j 4 src/apk
    strip output/src/apk
)
mkdir -p "$destination"
install -m755 "$tmp/apk-tools-v$version/output/src/apk" "$destination/apk-zstd"
tar -czf "$destination/source-apk-zstd.tar.gz" -C "$tmp" "$archive" repo-hash-sha1.patch APKBUILD
