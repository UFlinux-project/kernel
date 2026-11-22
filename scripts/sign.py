#!/usr/bin/env python3
"""Replace the temporary APKv3 signatures with the UFlinux P-384 key."""

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def require_zstd(package):
    with package.open("rb") as stream:
        header = stream.read(10)
    # ADBc is followed by the compression algorithm and level, then the frame.
    if header[:5] != b"ADBc\x02" or header[6:] != b"\x28\xb5\x2f\xfd":
        raise SystemExit(f"{package.name}: expected a Zstandard-compressed APKv3 package")


def main(directory):
    secret = os.environ.pop("UFLINUX_APK_SIGNING_KEY", "").strip()
    if not secret:
        raise SystemExit("Missing UFLINUX_APK_SIGNING_KEY")
    apk = os.environ.get("UFLINUX_APK", "apk")
    os.umask(0o077)
    public = directory / "uflinux-kernel.ec.pem.pub"
    packages = sorted(directory.glob("*.apk"))
    if not packages:
        raise SystemExit("No APKs to sign")
    for package in packages:
        require_zstd(package)
    with tempfile.TemporaryDirectory() as temp:
        key = Path(temp) / "uflinux-kernel.ec.pem"
        keys = Path(temp) / "trusted"
        keys.mkdir()
        trusted_key = keys / public.name
        key.write_text(secret + "\n")
        subprocess.run(["openssl", "ec", "-in", str(key), "-check", "-param_enc", "named_curve",
                        "-pubout", "-out", str(trusted_key)], check=True)
        details = subprocess.check_output(["openssl", "pkey", "-pubin", "-in", str(trusted_key),
                                           "-text_pub", "-noout"], text=True)
        if "ASN1 OID: secp384r1" not in details.splitlines():
            raise SystemExit("UFLINUX_APK_SIGNING_KEY must use the P-384 (secp384r1) curve")
        for package in packages:
            subprocess.run([apk, "adbsign", "--allow-untrusted", "--reset-signatures", "--compression", "zstd:9",
                            "--sign-key", str(key), str(package)], check=True)
            require_zstd(package)
            subprocess.run([apk, "verify", "--keys-dir", str(keys), str(package)], check=True)
            package.chmod(0o644)
        shutil.copyfile(trusted_key, public)
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
