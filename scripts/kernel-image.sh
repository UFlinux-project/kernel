#!/usr/bin/env bash
# QEMU's -kernel option only loads raw or self-decompressing gzip images, so a
# Zstandard-compressed arm64 Image has to be unpacked before it is handed over.

decompress_kernel() {
    case "$(head -c 4 "$1" | od -An -tx1 | tr -d ' \n')" in
        28b52ffd) zstd -dc -q "$1" ;;
        1f8b*) gzip -dc "$1" ;;
        *) cat "$1" ;;
    esac
}
