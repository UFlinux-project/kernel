#!/bin/sh
set -eu

aports_ref=$1
destination=$2
test -n "$aports_ref"
tmp=$(mktemp -d)
trap 'rm -r "$tmp"' EXIT

apk add --no-progress build-base curl meson linux-headers openssl-dev openssl-libs-static \
    zlib-dev zlib-static zstd-dev zstd-static
base="https://raw.githubusercontent.com/alpinelinux/aports/$aports_ref/main/apk-tools"
curl -fsSL --retry 3 "$base/APKBUILD" -o "$tmp/APKBUILD"
version=$(sed -n 's/^pkgver=\([0-9.]*\)$/\1/p' "$tmp/APKBUILD")
test -n "$version"
archive="apk-tools-v$version.tar.gz"
curl -fsSL --retry 3 "https://gitlab.alpinelinux.org/alpine/apk-tools/-/archive/v$version/$archive" -o "$tmp/$archive"
curl -fsSL --retry 3 "$base/repo-hash-sha1.patch" -o "$tmp/repo-hash-sha1.patch"
(
    cd "$tmp"
    for file in "$archive" repo-hash-sha1.patch; do
        digest=$(awk -v file="$file" '$2 == file && length($1) == 128 { print $1 }' APKBUILD)
        test -n "$digest"
        printf '%s  %s\n' "$digest" "$file" | sha512sum -c
    done
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
