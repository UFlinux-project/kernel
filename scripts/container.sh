#!/usr/bin/env bash
set -euo pipefail

runtime=${CONTAINER_RUNTIME:-docker}
container_options=()
if [[ ${runtime##*/} == podman ]]; then
    container_options+=(--security-opt label=disable)
fi

alpine_image() {
    local resolved=$1 architecture=$2 record archive digest platform tag
    record=$(jq -ce --arg arch "$architecture" '.alpine[$arch]' "$resolved") || return
    digest=$(jq -r '.sha256' <<<"$record")
    platform=linux/$(jq -r '.platform' <<<"$record")
    tag="uflinux-alpine:$architecture-${digest:0:16}"
    if ! "$runtime" image inspect "$tag" >/dev/null 2>&1; then
        archive=$(mktemp)
        if ! curl -fsSL --retry 3 "$(jq -r '.url' <<<"$record")" -o "$archive" ||
           ! printf '%s  %s\n' "$digest" "$archive" | sha256sum -c >&2; then
            rm -f "$archive"
            return 1
        fi
        if [[ ${runtime##*/} == podman ]]; then
            "$runtime" import --arch "${platform#linux/}" "$archive" "$tag" >&2 || { rm -f "$archive"; return 1; }
        else
            "$runtime" import --platform "$platform" "$archive" "$tag" >&2 || { rm -f "$archive"; return 1; }
        fi
        rm -f "$archive"
    fi
    printf '%s\n' "$tag"
}

kernel_packages() {
    local resolved=$1 architecture=$2
    jq -er --arg arch "$architecture" '
        .toolchains[$arch] as $toolchain
        | ($toolchain.major | tostring) as $major
        | ["alpine-sdk", "bash", "bc", "bison", "flex", "perl", "python3", "curl", "openssl-dev",
           "elfutils-dev", "linux-headers", "ncurses-dev", "zlib-dev", "zstd", "gzip", "xz",
           "diffutils", "findutils", "tar", "kmod", "cpio", "gmp-dev", "mpc1-dev", "mpfr-dev", "rsync"][],
          (["clang" + $major, "lld" + $major, "llvm" + $major, "gcc", "binutils", "abuild"][] as $name
           | ($toolchain.versions[$name] // error("Missing resolved Alpine package version for " + $name)) as $version
           | "\($name)=\($version)")
    ' "$resolved"
}

fetch_kernel_packages() {
    local resolved=$1 architecture=$2 output=$3 image packages platform
    image=$(alpine_image "$resolved" "$architecture")
    packages=$(kernel_packages "$resolved" "$architecture")
    platform=$(jq -er --arg arch "$architecture" '.alpine[$arch].platform | "linux/"+.' "$resolved")
    local -a package_args
    mapfile -t package_args <<< "$packages"
    mkdir -p "$output"
    "$runtime" run --rm "${container_options[@]}" --platform "$platform" -v "$(realpath "$output"):/packages" \
        "$image" apk fetch --recursive --output /packages "${package_args[@]}"
}

if [[ ${BASH_SOURCE[0]} != "$0" ]]; then
    return
fi

target=${1:?Specify a target}
resolved=$(realpath "${2:-work/resolved.json}")
output=$(realpath -m "${3:-work/$target}")
profile=${4:-}
phase=${UFLINUX_KERNEL_PHASE:-all}
case "$phase" in
    all|compile|package|merge-pgo) ;;
    *) printf 'Unsupported kernel phase: %s\n' "$phase" >&2; exit 1 ;;
esac
repo=$(cd "$(dirname "$0")/.." && pwd)
revision=${UFLINUX_PKGREL:-0}
jq -e --arg name "$target" '.[$name]' "$repo/targets.json" >/dev/null || {
    printf 'Unsupported kernel target: %s\n' "$target" >&2
    exit 1
}
mkdir -p "$output"
jq -e --arg name "$target" '.targets[$name]' "$resolved" >/dev/null
builder=$(jq -r --arg name "$target" '.targets[$name].builder' "$resolved")
architecture=$(jq -r --arg name "$target" '.targets[$name].apk' "$resolved")
version=$(jq -r --arg name "$target" '.targets[$name].version' "$resolved")
major=$(jq -er --arg arch "$builder" '.toolchains[$arch].major' "$resolved")
# Map the rolling mirror to the stable Alpine series chosen by the resolver.
aports_ref=$(jq -er .aports "$resolved")
series=$(jq -r '.alpine.x86_64.series' "$resolved")
printf 'https://dl-cdn.alpinelinux.org/alpine/%s/main\nhttps://dl-cdn.alpinelinux.org/alpine/%s/community\n' "$series" "$series" >"$output/repositories"
cp "$resolved" "$output/resolved.json"
if [[ "$phase" != package ]]; then
    build_image=$(alpine_image "$resolved" "$builder")
    build_options=("${container_options[@]}")
    package_args=()
    if [[ -n ${UFLINUX_KERNEL_PACKAGES:-} ]]; then
        snapshot=$(realpath -e "$UFLINUX_KERNEL_PACKAGES")
        build_options+=(-v "$snapshot:/kernel-packages:ro")
    else
        packages=$(kernel_packages "$resolved" "$builder")
        mapfile -t package_args <<< "$packages"
    fi

    # The literal script expands its arguments inside the Alpine build container.
    # shellcheck disable=SC2016
    "$runtime" run --rm "${build_options[@]}" --platform "$(jq -r --arg a "$builder" '.alpine[$a].platform | "linux/"+.' "$resolved")" \
        -v "$repo:/builder:ro" -v "$output:/out" "$build_image" sh -ec '
        cp /out/repositories /etc/apk/repositories
        major=$1
        target=$2
        profile=$3
        phase=$4
        shift 4
        if [ -d /kernel-packages ]; then
            apk --no-network --repositories-file /dev/null add --no-progress /kernel-packages/*.apk
        else
            apk add --no-progress "$@"
        fi
        mkdir -p /opt/llvm-bin
        for bin in clang clang++ ld.lld llvm-ar llvm-as llvm-nm llvm-objcopy llvm-objdump llvm-readelf llvm-strip llvm-bolt llvm-profdata; do
            for candidate in "/usr/lib/llvm$major/bin/$bin" "/usr/bin/$bin-$major" "/usr/bin/$bin"; do
                if [ -x "$candidate" ]; then ln -sf "$candidate" "/opt/llvm-bin/$bin"; break; fi
            done
        done
        export PATH="/out/bolt:/opt/llvm-bin:$PATH"
        if [ -d /out/profiles ]; then export UFLINUX_PROFILE_DIR=/out/profiles; fi
        if [ "$phase" = merge-pgo ]; then
            exec python3 /builder/scripts/kernel_pgo.py /out /out/profiles llvm-profdata
        fi
        if [ -n "$profile" ]; then set -- "$profile"; else set --; fi
        python3 /builder/scripts/build.py "$target" /out/resolved.json /out "$@"
        ' sh "$major" "$target" "$profile" "$phase" "${package_args[@]}"

fi

if [[ "$phase" == compile || "$phase" == merge-pgo ]]; then
    exit 0
fi

package_image=$(alpine_image "$resolved" "$architecture")
package_platform=$(jq -er --arg a "$architecture" '.alpine[$a].platform | "linux/"+.' "$resolved")
if [[ ! -x "$output/apk-zstd" ]]; then
    # shellcheck disable=SC2016
    "$runtime" run --rm "${container_options[@]}" --platform "$package_platform" \
        -v "$repo:/builder:ro" -v "$output:/out" "$package_image" sh -ec '
        cp /out/repositories /etc/apk/repositories
        sh /builder/scripts/build-apk-zstd.sh "$1" "$2" /out
        ' sh "$aports_ref" "$series"
fi

"$runtime" run --rm "${container_options[@]}" --platform "$package_platform" \
    -v "$repo:/builder:ro" -v "$output:/out" "$package_image" sh -ec '
    cp /out/repositories /etc/apk/repositories
    apk add --no-progress python3
    sh /builder/scripts/package.sh "$@"
    ' sh "$target" "$version" "$revision" "$architecture" \
    "${UFLINUX_PROFILE_GUEST:-}"
