#!/usr/bin/env python3
"""Replace abuild's temporary APK v2 signature without rewriting package data."""

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zlib


def end_of_member(stream, max_size):
    decoder = zlib.decompressobj(wbits=31)
    size = 0
    while not decoder.eof:
        packet = stream.read(4096)
        if not packet:
            raise ValueError("Truncated APK gzip member")
        size += len(decoder.decompress(packet, max_size - size + 1))
        if size > max_size or decoder.unconsumed_tail:
            raise ValueError("Oversized APK metadata")
    stream.seek(-len(decoder.unused_data), os.SEEK_CUR)
    return stream.tell()


def sign(package, key, public_key):
    with package.open("rb") as source, tempfile.TemporaryDirectory(dir=package.parent) as folder:
        start = end_of_member(source, 65536)
        end = end_of_member(source, 2 * 1024 * 1024)
        source.seek(start)
        control = Path(folder) / "control.tar.gz"
        control.write_bytes(source.read(end - start))
        subprocess.run(["abuild-sign", "-t", "RSA256", "-k", str(key), "-p", str(public_key), str(control)], check=True)
        result = Path(folder) / package.name
        with result.open("wb") as target, control.open("rb") as metadata:
            shutil.copyfileobj(metadata, target)
            shutil.copyfileobj(source, target)
        subprocess.run(["apk", "verify", str(result)], check=True)
        result.replace(package)


def main(directory):
    secret = os.environ.pop("UFLINUX_APK_SIGNING_KEY", "").strip()
    if not secret:
        raise SystemExit("Missing UFLINUX_APK_SIGNING_KEY")
    os.umask(0o077)
    public = directory / "uflinux-kernel.rsa.pub"
    with tempfile.TemporaryDirectory() as temp:
        key = Path(temp) / "uflinux-kernel.rsa"
        key.write_text(secret + "\n")
        subprocess.run(["openssl", "rsa", "-in", str(key), "-pubout", "-out", str(public)], check=True)
        shutil.copyfile(public, Path("/etc/apk/keys") / public.name)
        packages = sorted(directory.glob("*.apk"))
        if not packages:
            raise SystemExit("No APKs to sign")
        for package in packages:
            sign(package, key, public)
            package.chmod(0o644)
    manifest = directory / "SHA256SUMS"
    with manifest.open("w") as output:
        for file in sorted(directory.iterdir()):
            if file.is_file() and file != manifest:
                with file.open("rb") as data:
                    digest = hashlib.file_digest(data, "sha256").hexdigest()
                    print(f"{digest}  {file.name}", file=output)
    public.chmod(0o644)
    manifest.chmod(0o644)


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
