#!/usr/bin/env python3
"""Fetch, patch, configure, and stage one kernel for Alpine packaging."""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import kernel_pgo
from resolve import ROOT, fetch


def run(*args, cwd=None, capture=False):
    return subprocess.run(args, cwd=cwd, check=True, text=True,
                          stdout=subprocess.PIPE if capture else None).stdout


def values(config):
    return dict(re.findall(r"^CONFIG_(\w+)=(.*)$", config.read_text(), re.M))


# Every MODULE_SIG hash choice member across the supported sources. Older trees have no
# SHA-3 members; scripts/config writes them anyway and olddefconfig drops the unknown names.
MODULE_SIG_HASHES = ("MODULE_SIG_SHA1", "MODULE_SIG_SHA224", "MODULE_SIG_SHA256",
                     "MODULE_SIG_SHA384", "MODULE_SIG_SHA512", "MODULE_SIG_SHA3_256",
                     "MODULE_SIG_SHA3_384", "MODULE_SIG_SHA3_512")


def get(url, dest):
    run("curl", "--fail", "--location", "--retry", "3", "--silent", "--show-error",
        "--output", str(dest), url)


def apply_candidates(kernel, name, urls, records, required=False):
    for url in urls:
        patch = fetch(url, missing=True)
        if not patch:
            continue
        path = kernel.parent / f"{name}.patch"
        path.write_bytes(patch)
        check = subprocess.run(["git", "apply", "--check", str(path)], cwd=kernel,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if check.returncode:
            print(f"{name}: {url}: {check.stderr.strip()}", flush=True)
            continue
        run("git", "apply", str(path), cwd=kernel)
        records[name] = {"source": url, "sha256": hashlib.sha256(patch).hexdigest()}
        return True
    if required:
        raise SystemExit(f"{name}: no patch applies to this kernel")
    records[name] = {"omitted": "No compatible patch for this source"}
    return False


def bbr_implementation(kernel):
    for path in sorted((kernel / "net/ipv4").glob("tcp_bbr*.c"), reverse=True):
        text = path.read_text()
        if not re.search(r"^\s*#\s*define\s+BBR_VERSION\s+3\b", text, re.M):
            continue
        name = re.search(r'\.name\s*=\s*"(bbr3?)"', text)
        if name:
            return name[1], "TCP_CONG_" + path.stem.removeprefix("tcp_").upper()
    return None


def bbr_default(kernel, name, symbol):
    kconfig = kernel / "net/ipv4/Kconfig"
    text = kconfig.read_text()
    choice = re.search(r"^\s*config DEFAULT_\w+\n\s+bool \"[^\"]+\" if " + symbol + "=y", text, re.M)
    if choice:
        selector = choice[0].strip().splitlines()[0].split()[1]
        if re.search(rf'default "{name}" if {selector}\b', text):
            return selector
    marker = re.search(r"^([ \t]*)config DEFAULT_CUBIC$", text, re.M)
    if not marker or "config DEFAULT_TCP_CONG\n" not in text:
        raise SystemExit("TCP congestion-control Kconfig layout changed")
    selector = "DEFAULT_UFLINUX_BBR"
    indent = marker[1]
    option = f'{indent}config {selector}\n{indent}\tbool "BBRv3" if {symbol}=y\n\n'
    text = text[:marker.start()] + option + text[marker.start():]
    text = text.replace("config DEFAULT_TCP_CONG\n\tstring",
                        f'config DEFAULT_TCP_CONG\n\tstring\n\tdefault "{name}" if {selector}')
    kconfig.write_text(text)
    return selector


def module_signing_key(kernel, obj, target, name):
    """Install the release module-signing key and return its in-tree path.

    The secret arrives as base64-encoded PEM in UFLINUX_MODULE_SIGNING_KEY, or as
    a file holding that same value in UFLINUX_MODULE_SIGNING_KEY_FILE. The value is
    the same combined PEM the kernel generates for itself: a PKCS#8 private key and
    the matching self-signed X.509 certificate. Kbuild needs both in one file,
    because `extract-cert` reads the certificate out of it to embed in the kernel
    while `sign-file` reads the private key out of it to sign modules.
    The key is written into the object tree under a name that is not the kernel
    default, so `certs/Makefile` never falls back to generating a random key: every
    release and debug build of a target then signs modules with the same identity,
    with the same embedded certificate bytes.
    """
    encoded = os.environ.get("UFLINUX_MODULE_SIGNING_KEY", "")
    if not encoded:
        path = os.environ.get("UFLINUX_MODULE_SIGNING_KEY_FILE", "")
        if path:
            source = Path(path)
            if source.is_file():
                encoded = source.read_text().strip()
    if not encoded:
        if target.get("module_signing"):
            # Fork pull requests have no repository secrets. Skip signing instead of
            # generating a random per-build key, which would be useless downstream; the
            # release workflow refuses to build without the key.
            print(f"{name}: no module signing key available, building unsigned", flush=True)
        return None
    try:
        key = base64.b64decode(encoded, validate=True)
    except Exception:
        raise SystemExit("UFLINUX_MODULE_SIGNING_KEY is not base64-encoded PEM")
    if b"-----BEGIN PRIVATE KEY-----" not in key or b"-----END PRIVATE KEY-----" not in key:
        raise SystemExit("UFLINUX_MODULE_SIGNING_KEY must hold a PKCS#8 private key in PEM form")
    if b"-----BEGIN CERTIFICATE-----" not in key:
        raise SystemExit("UFLINUX_MODULE_SIGNING_KEY must also hold the matching X.509 "
                         "certificate, as a combined key and certificate PEM")
    openssl = shutil.which("openssl")
    if openssl is None:
        raise SystemExit("openssl is required to validate the module signing key")
    relative = "certs/uflinux-signing_key.pem"
    dest = obj / relative
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(key)
    dest.chmod(0o600)
    # Pin the timestamp so a later repack that re-materialises the key does not make
    # the already extracted certificate look stale and relink the kernel.
    os.utime(dest, (0, 0))
    # The key is EC NIST P-384 by default, so check it through the algorithm agnostic
    # `pkey` interface; `rsa -check` and `-modulus` both fail on an elliptic curve key.
    run(openssl, "pkey", "-in", str(dest), "-check", "-noout")
    run(openssl, "pkey", "-in", str(dest), "-pubout", "-out", "/dev/null")
    # Reject a certificate that does not belong to the key, and one that Kbuild would
    # refuse to extract or that the kernel could not use for the whole release window.
    run(openssl, "x509", "-in", str(dest), "-noout", "-checkend", "315360000")
    # Compare the public part as PEM text. DER would not work: the two commands emit the
    # key with different surrounding structures. This holds for RSA and EC alike.
    key_public = run(openssl, "pkey", "-in", str(dest), "-pubout", capture=True)
    cert_public = run(openssl, "x509", "-in", str(dest), "-noout", "-pubkey", capture=True)
    if key_public.strip() != cert_public.strip():
        raise SystemExit("the certificate in UFLINUX_MODULE_SIGNING_KEY does not match its key")
    return relative


def configure(kernel, obj, target, name, kbuild, bbr, gcc=False):
    config = obj / ".config"
    if "config_url" in target:
        get(target["config_url"], config)
    else:
        run(*kbuild, *target["defconfig"])
    config_tool = [str(kernel / "scripts/config"), "--file", str(config)]

    def set_config(*options):
        run(*config_tool, *options)

    def normalize():
        run(*kbuild, "olddefconfig")
        return values(config)

    algorithm, symbol = bbr
    selector = bbr_default(kernel, algorithm, symbol)
    crypto = ("CRYPTO", "CRYPTO_HMAC", "CRYPTO_MD5", "CRYPTO_SHA3", "CRYPTO_ECDSA", "CRYPTO_CRC32C",
              "CRYPTO_BLAKE2B")
    if re.search(r"^config CRYPTO_MLDSA\b", (kernel / "crypto/Kconfig").read_text(), re.M):
        crypto += ("CRYPTO_MLDSA",)
    # Lockdown can select MODULE_SIG. Clear the key too so Kbuild does not
    # embed a random certificate and change the ELF build ID between builds.
    # Signing is switched back on below when a release signing key is available.
    set_config("--set-str", "LOCALVERSION", f"-uflinux-{name}", "-d", "LOCALVERSION_AUTO",
               "-e", "MODULES", "-d", "MODULE_SIG", "-d", "MODULE_SIG_ALL", "--set-str", "MODULE_SIG_KEY", "",
               "--set-str", "SYSTEM_TRUSTED_KEYS", "",
               "--set-str", "SYSTEM_REVOCATION_KEYS", "", "-d", "WERROR", "-d", "RUST",
               "-d", "GCC_PLUGINS", "-e", "DEBUG_INFO_NONE", "-d", "DEBUG_INFO_BTF", "-d", "DEBUG_INFO_BTF_MODULES",
               # DEBUG_INFO_NONE is a choice member: clear the DWARF options the vendor configs
               # enable, or olddefconfig keeps DEBUG_INFO (and its debug sections) switched on.
               "-d", "DEBUG_INFO_DWARF5", "-d", "DEBUG_INFO_DWARF4",
               "-d", "DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT",
               "-e", "TCP_CONG_ADVANCED", "-e", "TCP_CONG_CUBIC", "-e", symbol, "-e", selector,
               "-e", "LRU_GEN", "-e", "LRU_GEN_ENABLED", "-e", "SWAP", "-e", "ZSWAP", "-d", "ZSWAP_DEFAULT_ON",
               "-e", "ZSWAP_COMPRESSOR_DEFAULT_ZSTD", "-e", "CRYPTO_LZ4", "-e", "CRYPTO_ZSTD",
               "-m", "ZRAM", "-e", "ZRAM_BACKEND_LZ4", "-e", "ZRAM_BACKEND_ZSTD",
               "-e", "ZRAM_DEF_COMP_ZSTD", "-m", "F2FS_FS", "-e", "F2FS_FS_COMPRESSION",
               "-e", "F2FS_FS_LZ4", "-e", "F2FS_FS_ZSTD",
               *(option for key in crypto for option in ("-e", key)))
    if name == "rpi":
        set_config("-e", "ARM64_4K_PAGES")
    virt = name.startswith("virt-")
    for driver in ("DRM_AMDGPU", "DRM_RADEON", "DRM_NOUVEAU", "DRM_XE"):
        set_config("-d" if virt else "-m", driver)
    set_config("-m" if name == "x86_64" else "-d", "DRM_I915")
    if virt:
        set_config("-e", "PCI", "-e", "EFI", "-e", "EFI_STUB", "-e", "VIRTIO_PCI",
                   "-e", "VIRTIO_MMIO", "-e", "VIRTIO_BLK", "-e", "SCSI", "-e", "BLK_DEV_SD",
                   "-e", "SCSI_VIRTIO", "-e", "VIRTIO_NET", "-e", "VIRTIO_CONSOLE",
                   "-e", "DRM", "-m", "DRM_VIRTIO_GPU", "-e", "DEVTMPFS", "-e", "BLK_DEV_INITRD",
                   "-e", "EXT4_FS", "-e", "SERIAL_8250_CONSOLE" if target["arch"] == "x86"
                   else "SERIAL_AMBA_PL011_CONSOLE")
    set_config("-d", "AUTOFDO_CLANG", "-d", "LTO_CLANG_FULL")
    # Resident-footprint policy. Everything here is data in targets.json so a flavor can
    # opt back in without touching code. See README "Memory footprint policy".
    set_config("-d", "LATENCYTOP", "-d", "KALLSYMS_ALL", "-d", "LIVEPATCH",
               "-d", "PRINTK_INDEX", "-d", "BOOT_PRINTK_DELAY")
    # LATENCYTOP selects KALLSYMS_ALL and SCHEDSTATS, so it has to be cleared in the same
    # pass; KALLSYMS_ALL is what makes perf lose data symbols, and it is the single largest
    # saving available (1.4 MiB of .rodata on virt-x86_64).
    if not target.get("schedstats", False):
        set_config("-d", "SCHEDSTATS")
    else:
        set_config("-e", "SCHEDSTATS")
    if not target.get("dynamic_debug", False):
        set_config("-d", "DYNAMIC_DEBUG")
    if not target.get("ikconfig", True):
        set_config("-d", "IKCONFIG", "-d", "IKCONFIG_PROC")
    if not target.get("init_on_alloc", True):
        set_config("-d", "INIT_ON_ALLOC_DEFAULT_ON")
    for option, key in (("LOG_BUF_SHIFT", "log_buf_shift"), ("NR_CPUS", "nr_cpus")):
        if key in target:
            set_config("--set-val", option, str(target[key]))
    if "hz" in target:
        set_config(*[flag for hz in (100, 250, 300, 1000)
                     for flag in ("-e" if hz == target["hz"] else "-d", f"HZ_{hz}")])
    # Module loading: sign every module with the release key and store them zstd-compressed.
    # ZSTD_DECOMPRESS is already built in everywhere, so in-kernel decompression is free and
    # keeps initramfs generators from needing their own zstd support.
    signing_key = module_signing_key(kernel, obj, target, name) if target.get("module_signing") else None
    # EC NIST P-384 with sha384 by default: the signature trailer is ~380 B smaller per
    # module than RSA-4096/sha256 once zstd-compressed, and verifying an ECDSA-P384 signature
    # is cheaper at load than an RSA-4096 one. CRYPTO_RSA stays resident regardless, because
    # MODULE_SIG selects SYSTEM_DATA_VERIFICATION, which selects it.
    # sha384 is the only hash every supported source allows next to an EC key: 6.50 onwards
    # MODULE_SIG_KEY_TYPE_ECDSA depends on !(MODULE_SIG_SHA256 || MODULE_SIG_SHA3_256), and
    # Kconfig would silently fall back to RSA. A flavor can still pick rsa/sha256.
    signing_type = target.get("module_sig_key_type", "ECDSA")
    signing_hash = target.get("module_sig_hash", "sha384")
    if signing_key:
        # MODULE_SIG_FORCE stays off: out-of-tree modules built locally (DKMS) are unsigned,
        # and lockdown integrity already requires a valid signature when it is active.
        # The hashes are a choice, so clear the alternatives: scripts/config writes the lines
        # and olddefconfig would otherwise keep whichever member the base config selected.
        wanted_hash = f"MODULE_SIG_{signing_hash.upper()}"
        set_config("-e", "MODULE_SIG", "-e", "MODULE_SIG_ALL", "-d", "MODULE_SIG_FORCE",
                   "-e", f"MODULE_SIG_KEY_TYPE_{signing_type}",
                   *(option for option in MODULE_SIG_HASHES
                     for option in ("-e" if option == wanted_hash else "-d", option)),
                   "--set-str", "MODULE_SIG_KEY", signing_key)
    else:
        set_config("-d", "MODULE_SIG", "-d", "MODULE_SIG_ALL", "--set-str", "MODULE_SIG_KEY", "")
    # Module unload stays on everywhere: it is the only way a rarely used driver gives its
    # memory back at runtime. TRIM_UNUSED_KSYMS is deliberately never enabled because it
    # breaks out-of-tree (DKMS) modules, which are built against the full export table.
    set_config("-e", "MODULE_UNLOAD", "-d", "TRIM_UNUSED_KSYMS")
    if target.get("module_compression", "zstd") == "zstd":
        options = ["-d", "MODULE_COMPRESS_NONE", "-d", "MODULE_COMPRESS_GZIP",
                   "-d", "MODULE_COMPRESS_XZ", "-e", "MODULE_COMPRESS_ZSTD",
                   "-e", "MODULE_DECOMPRESS"]
        if re.search(r"^config MODULE_COMPRESS_ALL\b", (kernel / "kernel/module/Kconfig").read_text(), re.M):
            # Compressed module installation is gated on this option from 6.7 onwards.
            options += ["-e", "MODULE_COMPRESS", "-e", "MODULE_COMPRESS_ALL"]
        set_config(*options)
    # Boot-image compression: the zstd decompressor is already resident on every target that
    # can select an image compressor, so this only changes the on-disk image and the boot path.
    if target.get("kernel_compression") == "zstd":
        set_config("-d", "KERNEL_GZIP", "-d", "KERNEL_LZ4", "-d", "KERNEL_LZMA", "-d", "KERNEL_XZ",
                   "-e", "KERNEL_ZSTD")
    if re.search(r"^config SCHED_BORE\b", (kernel / "init/Kconfig").read_text(), re.M):
        set_config("-e", "SCHED_BORE")
    if not gcc:
        set_config("-e", "LTO_CLANG_THIN_DIST", "-d", "LTO_NONE")
        effective = normalize()
        if effective.get("LTO_CLANG_THIN_DIST") != "y":
            set_config("-e", "LTO_CLANG_THIN", "-d", "LTO_NONE")
    else:
        set_config("-e", "LTO_NONE", "-d", "LTO_CLANG_THIN", "-d", "LTO_CLANG_THIN_DIST")
    effective = normalize()
    if effective.get(symbol) != "y" or effective.get("DEFAULT_TCP_CONG") != f'"{algorithm}"':
        raise SystemExit("BBRv3 was not built in as the default TCP congestion control")
    missing_crypto = [f"CONFIG_{key}" for key in crypto if effective.get(key) != "y"]
    if missing_crypto:
        raise SystemExit(f"{name}: required crypto options are not built in: {', '.join(missing_crypto)}")
    # Footprint invariants: catch a vendor or upstream config that re-enables a dropped
    # option through a select, and make sure the compression codecs stay in the kernel.
    forbidden = ["LATENCYTOP", "KALLSYMS_ALL", "TRIM_UNUSED_KSYMS", "DEBUG_INFO"]
    if not target.get("schedstats", False):
        forbidden.append("SCHEDSTATS")
    if not target.get("dynamic_debug", False):
        forbidden.append("DYNAMIC_DEBUG")
    if not target.get("ikconfig", True):
        forbidden.append("IKCONFIG")
    if not target.get("init_on_alloc", True):
        forbidden.append("INIT_ON_ALLOC_DEFAULT_ON")
    unexpected = [option for option in forbidden if effective.get(option) in {"y", "m"}]
    for option, key in (("LOG_BUF_SHIFT", "log_buf_shift"), ("NR_CPUS", "nr_cpus"), ("HZ", "hz")):
        if key in target and effective.get(option) != str(target[key]):
            unexpected.append(f"{option}={target[key]}")
    if unexpected:
        raise SystemExit(f"{name}: footprint policy violated by {', '.join(unexpected)}")
    if signing_key and (effective.get("MODULE_SIG_ALL") != "y"
                        or effective.get("MODULE_SIG_KEY") != f'"{signing_key}"'):
        raise SystemExit(f"{name}: module signing is not using {signing_key}")
    if signing_key:
        # A base config that keeps MODULE_SIG_SHA256 selected silently voids the ECDSA
        # choice, and Kbuild then builds an RSA key into the kernel while the modules are
        # signed with the EC one. Check the effective values, not the requested ones.
        if effective.get(f"MODULE_SIG_KEY_TYPE_{signing_type}") != "y":
            raise SystemExit(f"{name}: module signing key type is not {signing_type}")
        if effective.get("MODULE_SIG_HASH") != f'"{signing_hash}"':
            raise SystemExit(f"{name}: modules are not signed with {signing_hash}")
    # Codecs are additive here: dropping one saves ~37 KiB and breaks formats users rely on.
    for option in ("ZLIB_INFLATE", "XZ_DEC", "LZ4_DECOMPRESS", "LZO_DECOMPRESS", "ZSTD_DECOMPRESS",
                   "DECOMPRESS_GZIP", "DECOMPRESS_ZSTD", "RD_ZSTD", "SQUASHFS_ZSTD"):
        if option in effective and effective[option] not in {"y", "m"}:
            raise SystemExit(f"{name}: compression codec CONFIG_{option} disappeared")
    if target.get("module_compression", "zstd") == "zstd" and effective.get("MODULE_COMPRESS_ZSTD") != "y":
        raise SystemExit(f"{name}: modules are not configured for zstd compression")
    if target.get("kernel_compression") == "zstd" and effective.get("KERNEL_ZSTD") != "y":
        raise SystemExit(f"{name}: the kernel image is not configured for zstd compression")
    expected_i915 = name == "x86_64"
    if (expected_i915 and effective.get("DRM_I915") != "m") or (
            not expected_i915 and effective.get("DRM_I915") in {"y", "m"}):
        raise SystemExit("Unexpected i915 configuration")
    if name == "rpi" and effective.get("ARM64_4K_PAGES") != "y":
        raise SystemExit("Raspberry Pi needs a 4 KiB kernel")
    return effective


def profile(name, kind, target):
    folder = Path(os.environ.get("UFLINUX_PROFILE_DIR", ROOT / "profiles" / name))
    path = folder / ("kernel.profdata" if kind == "pgo" else "kernel.fdata")
    metadata = folder / f"{kind}.json"
    if not path.is_file() or not metadata.is_file():
        return None, {}
    data = json.loads(metadata.read_text())
    with path.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    if data.get("source") != target["ref"] or data.get("sha256") != checksum:
        return None, {}
    return path, data


def pgo_flags(path):
    return f"UFLINUX_PGO_FLAGS=-fprofile-use={path} {kernel_pgo.COUNTER_FLAGS} -Werror=profile-instr-out-of-date"


def bolt_hook(kernel, x86, *, reuse_link=False):
    script = kernel / "scripts/link-vmlinux.sh"
    text = script.read_text()
    marker = "# For fixdep"
    if text.count(marker) != 1 or "mksysmap()" not in text:
        return False
    if reuse_link:
        link_start = '${MAKE} -f "${srctree}/scripts/Makefile.build" obj=init init/version-timestamp.o'
        if text.count(link_start) != 1:
            return False
        # The repack target reruns the image recipes against the profiled ELF.
        text = text.replace(link_start, 'if [ -z "${UFLINUX_BOLT_REPACK:-}" ]; then\n' + link_start)
        text = text.replace(marker, "fi\n\n" + marker)
    if x86:
        recipe = kernel / "arch/x86/boot/compressed/Makefile"
        recipe_text = recipe.read_text()
        relocs = "$(CMD_RELOCS) $< > $@;$(CMD_RELOCS) --abs-relocs $<"
        if recipe_text.count(relocs) != 1:
            return False
        # BOLT drops static relocations. The patched x86 rewriter leaves
        # the runtime relocation sites at their original addresses.
        recipe.write_text(recipe_text.replace(
            relocs,
            'input="$<"; if [ -f "$<.pre-bolt" ]; then input="$<.pre-bolt"; fi; '
            '$(CMD_RELOCS) "$$input" > $@;$(CMD_RELOCS) --abs-relocs "$$input"'))
    block = r'''
if [ -n "${UFLINUX_BOLT_PROFILE:-}" ]; then
    bolt_input=${VMLINUX:-vmlinux}
    rm -f "$bolt_input.pre-bolt" .uflinux-bolt-applied
    build_id=$(readelf -n "$bolt_input" | awk '/Build ID:/ {print $3; exit}')
    if [ -n "$build_id" ] && [ "$build_id" = "$UFLINUX_BOLT_BUILD_ID" ]; then
        bolt_log="$bolt_input.bolt.log"
        if llvm-bolt "$bolt_input" -o "$bolt_input.bolt" --data "$UFLINUX_BOLT_PROFILE" \
            ${UFLINUX_BOLT_X86_BUG_SIZE:+--linux-bug-entry-size="$UFLINUX_BOLT_X86_BUG_SIZE"} \
            --relocs=0 --eliminate-unreachable=0 --reorder-blocks=ext-tsp \
            --simplify-conditional-tail-calls=0 --skip-funcs=__entry_text_start,irq_entries_start \
            >"$bolt_log" 2>&1 && [ -s "$bolt_input.bolt" ] &&
            grep -Fxq 'BOLT-INFO: Linux kernel binary detected' "$bolt_log" &&
            readelf -SW "$bolt_input.bolt" | grep -F '.note.bolt_info' >/dev/null; then
            if [ -n "${UFLINUX_BOLT_X86_BUG_SIZE:-}" ]; then
                mv "$bolt_input" "$bolt_input.pre-bolt"
            fi
            mv "$bolt_input.bolt" "$bolt_input"
            # The original map was generated before BOLT moved symbols.
            mksysmap "$bolt_input" System.map
            touch .uflinux-bolt-applied
        else
            rm -f "$bolt_input.bolt"
            echo "BOLT could not optimize this Linux kernel, using the linked vmlinux" >&2
        fi
        cat "$bolt_log"
        rm -f "$bolt_log"
    else
        echo "BOLT profile build ID $UFLINUX_BOLT_BUILD_ID does not match $bolt_input (${build_id:-no build ID})" >&2
    fi
fi
'''
    script.write_text(text.replace(marker, block + "\n" + marker))
    return True


def x86_bug_entry_size(kernel, settings):
    # The x86 kernel uses relative pointers and aligns the entry to 4 bytes.
    size = 12 if settings.get("DEBUG_BUGVERBOSE") == "y" else 8
    if "#define HAVE_ARCH_BUG_FORMAT\n" in (kernel / "arch/x86/include/asm/bug.h").read_text():
        size += 4
    return size


def stage(kernel, obj, dest, name, target, kbuild, image, abi):
    boot = dest / "boot"
    boot.mkdir(parents=True)
    shutil.copy2(image, boot / f"vmlinuz-uflinux-{name}")
    shutil.copy2(obj / ".config", boot / f"config-uflinux-{name}")
    shutil.copy2(obj / "System.map", boot / f"System.map-uflinux-{name}")
    run(*kbuild, f"INSTALL_MOD_PATH={dest}", "INSTALL_MOD_STRIP=1", "modules_install")
    modules = dest / "lib/modules" / abi
    for link in ("build", "source"):
        (modules / link).unlink(missing_ok=True)
    (modules / "kernel-suffix").write_text(f"-uflinux-{name}\n")
    (modules / "vmlinuz").symlink_to(f"/boot/vmlinuz-uflinux-{name}")
    if name == "x86_64" and not list(modules.rglob("i915.ko*")):
        raise SystemExit("Missing i915 module")
    if target["arch"] != "x86":
        dtbs = boot / f"dtbs-uflinux-{name}"
        if not name.startswith("virt-"):
            run(*kbuild, f"INSTALL_DTBS_PATH={dtbs}", "dtbs_install")
        for pattern in target.get("dtbs", []):
            if not list(dtbs.glob(pattern)):
                raise SystemExit(f"Missing device tree: {pattern}")
        if name == "rpi":
            overlays = obj / "arch/arm64/boot/dts/overlays"
            overlay_dest = dtbs / "overlays"
            overlay_dest.mkdir(exist_ok=True)
            files = list(overlays.glob("*.dtbo"))
            if not files:
                raise SystemExit("Missing Raspberry Pi overlays")
            for overlay in files:
                shutil.copy2(overlay, overlay_dest / overlay.name)
            if (overlays / "overlay_map.dtb").exists():
                shutil.copy2(overlays / "overlay_map.dtb", overlay_dest / "overlay_map.dtb")
            readme = kernel / "arch/arm/boot/dts/overlays/README"
            if readme.exists():
                shutil.copy2(readme, overlay_dest / "README")
            shutil.copy2(image, boot / "kernel8.img")


def write_build_metadata(output, name, metadata):
    (output / "build.json").write_text(json.dumps(metadata, indent=2) + "\n")
    run("tar", "--exclude=.git", "-I", "zstd -T2", "-cf", str(output / f"source-{name}.tar.zst"),
        "-C", str(output), "kernel", "kernel.config", "build.json")


def apply_collected_bolt(output, name, target, kbuild):
    kernel, obj = output / "kernel", output / "obj"
    metadata = json.loads((output / "build.json").read_text())
    pgo, pgo_data = profile(name, "pgo", target)
    bolt, bolt_data = profile(name, "bolt", target)
    module_key = (metadata.get("module_signing_key") or "").strip('"')
    if module_key:
        # The repack relinks vmlinux, and `certs/signing_key.x509` lists the private key as a
        # prerequisite, so the key has to exist again even though it is never published.
        module_signing_key(kernel, obj, target, name)
    optimizer = shutil.which("llvm-bolt")
    if (metadata["target"] != name or metadata["source"] != target["ref"] or not metadata["pgo"]
            or not pgo or pgo_data.get("identity") != metadata["pgo_identity"]
            or pgo_data.get("sha256") != metadata.get("pgo_sha256")
            or not bolt or not bolt_data.get("build_id") or not optimizer):
        raise SystemExit(f"{name}: missing PGO build or matching BOLT inputs")
    bundled_optimizer = Path(optimizer).parent / "lib/optimizer"
    with (bundled_optimizer if bundled_optimizer.is_file() else Path(optimizer)).open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != bolt_data.get("rewriter_sha256"):
            raise SystemExit(f"{name}: llvm-bolt differs from the rewriter used to convert the trace")
    with (obj / "vmlinux.unstripped").open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != bolt_data.get("input_sha256"):
            raise SystemExit(f"{name}: the linked ELF differs from the profiled ELF")
    notes = run("readelf", "-n", str(obj / "vmlinux.unstripped"), capture=True)
    build_id = re.search(r"Build ID: ([0-9a-f]+)", notes)
    if not build_id or build_id[1] != bolt_data["build_id"]:
        raise SystemExit(f"{name}: the linked ELF build ID differs from the profiled ELF")
    if kernel_pgo.checksum(obj / ".config") != metadata["config_sha256"]:
        raise SystemExit(f"{name}: the kernel configuration changed before BOLT repack")

    x86 = target["arch"] == "x86"
    if not bolt_hook(kernel, x86, reuse_link=True):
        raise SystemExit(f"{name}: Kbuild layout changed, cannot repack the profiled kernel")
    os.environ.update(UFLINUX_BOLT_PROFILE=str(bolt), UFLINUX_BOLT_BUILD_ID=bolt_data["build_id"],
                      UFLINUX_BOLT_REPACK="1")
    if x86:
        os.environ["UFLINUX_BOLT_X86_BUG_SIZE"] = str(metadata["bolt_bug_entry_size"])
    flags = ["KCFLAGS=-O3"] if metadata["optimization"] == "-O3" else []
    image = obj / target["image"]
    jobs = f"-j{min(4, os.cpu_count() or 1)}"
    run(*kbuild, pgo_flags(pgo), *flags, jobs, image.name)
    if not (obj / ".uflinux-bolt-applied").is_file():
        raise SystemExit(f"{name}: BOLT did not rewrite the profiled kernel")

    sections = run("readelf", "-SW", str(obj / "vmlinux.unstripped"), capture=True)
    if ".note.bolt_info" not in sections:
        raise SystemExit(f"{name}: missing BOLT note after boot image regeneration")

    boot = output / "stage" / "boot"
    shutil.copy2(image, boot / f"vmlinuz-uflinux-{name}")
    shutil.copy2(obj / "System.map", boot / f"System.map-uflinux-{name}")
    module_key = (metadata.get("module_signing_key") or "").strip('"')
    if module_key:
        (obj / module_key).unlink(missing_ok=True)
    metadata["bolt"] = True
    metadata["bolt_input_sha256"] = bolt_data["input_sha256"]
    metadata["bolt_profile_sha256"] = bolt_data["sha256"]
    metadata["bolt_rewriter_sha256"] = bolt_data["rewriter_sha256"]
    metadata["omissions"].pop("BOLT", None)
    write_build_metadata(output, name, metadata)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("target", choices=json.loads((ROOT / "targets.json").read_text()))
    parser.add_argument("resolved", type=Path)
    parser.add_argument("output", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--profile-build", action="store_true")
    mode.add_argument("--bolt-baseline", action="store_true")
    mode.add_argument("--pgo-build", action="store_true")
    mode.add_argument("--prepare-only", action="store_true")
    mode.add_argument("--apply-collected-bolt", action="store_true")
    args = parser.parse_args()
    name = args.target
    target = json.loads(args.resolved.read_text())["targets"][name]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    kernel, obj = output / "kernel", output / "obj"
    os.environ.update(KBUILD_BUILD_USER="uflinux", KBUILD_BUILD_HOST="kernel",
                      KBUILD_BUILD_TIMESTAMP="Thu Jan  1 00:00:00 UTC 1970", KBUILD_BUILD_VERSION="1",
                      SOURCE_DATE_EPOCH="0", KCONFIG_NOTIMESTAMP="1", LC_ALL="C")
    gcc = target.get("compiler") == "gcc"
    toolchain = ["CC=gcc", "HOSTCC=gcc", "HOSTCXX=g++"] if gcc else ["LLVM=1", "LLVM_IAS=1"]
    kbuild = ["make", "-C", str(kernel), f"O={obj}", f"ARCH={target['arch']}", "LOCALVERSION=", *toolchain]
    if args.apply_collected_bolt:
        apply_collected_bolt(output, name, target, kbuild)
        return
    kernel.mkdir()
    obj.mkdir()
    archive = output / "upstream.tar"
    get(target["url"], archive)
    run("tar", "-xf", str(archive), "-C", str(kernel), "--strip-components=1")
    archive.unlink()
    run("git", "init", "--quiet", cwd=kernel)

    records = {}
    for index, url in enumerate(target.get("vendor_patches", [])):
        apply_candidates(kernel, f"vendor-{index}", [url], records, required=True)
    patch_root = ROOT / "patches"
    source_patches = patch_root / f"{target['source']}-{target['arch']}"
    target_patches = patch_root / name
    for patch in sorted(source_patches.glob("*.patch")) + sorted(target_patches.glob("*.patch")):
        run("git", "apply", str(patch), cwd=kernel)
        records[patch.name] = {"sha256": hashlib.sha256(patch.read_bytes()).hexdigest()}
    if bbr_implementation(kernel) is None:
        apply_candidates(kernel, "bbrv3", target["bbr"], records, required=True)
    else:
        records["bbrv3"] = {"source": target["ref"]}
    bbr = bbr_implementation(kernel)
    if bbr is None:
        raise SystemExit("Patch did not install a verifiable BBRv3 implementation")
    apply_candidates(kernel, "bore", target["bore"], records)
    apply_candidates(kernel, "le9uo", target["le9uo"], records)
    if args.prepare_only:
        print(json.dumps(records, indent=2))
        return

    pgo, pgo_data = profile(name, "pgo", target)
    bolt, bolt_data = profile(name, "bolt", target)
    settings = configure(kernel, obj, target, name, kbuild, bbr, gcc)
    flags = ["KCFLAGS=-O3"] if not target.get("disable_o3") else []
    compiler = run("gcc" if gcc else "clang", "--version", capture=True).splitlines()[0]
    identity = {
        "target": name, "source": target["ref"], "patches": records,
        "instrumentation_sha256": kernel_pgo.checksum(Path(kernel_pgo.__file__)),
        "exporter_sha256": kernel_pgo.checksum(Path(kernel_pgo.__file__).with_name("kernel-pgo.c")),
        "config_sha256": kernel_pgo.checksum(obj / ".config"), "compiler": compiler,
        "optimization": flags,
    }
    if args.profile_build or (pgo and pgo_data.get("identity") != identity):
        pgo = None
    if (args.pgo_build or args.bolt_baseline) and not pgo:
        raise SystemExit(f"{name}: the requested build requires a matching PGO profile")
    if args.profile_build or pgo:
        version = re.search(r"clang version (\d+)\.", compiler)
        if not version or int(version[1]) not in kernel_pgo.RAW_FORMATS:
            raise SystemExit(f"{name}: unsupported compiler for kernel instrumentation PGO: {compiler}")
        kernel_pgo.install(kernel, args.profile_build)
        if args.profile_build:
            if settings.get("PROC_FS") != "y":
                raise SystemExit(f"{name}: PGO collection requires PROC_FS")
            kbuild.append(f"UFLINUX_PGO_FLAGS={kernel_pgo.COLLECTION_FLAGS}")
        else:
            kbuild.append(pgo_flags(pgo))
    x86 = target["apk"] == "x86_64"
    if x86:
        bug_entry_size = x86_bug_entry_size(kernel, settings)
    if (bolt and not (args.profile_build or args.bolt_baseline) and target["apk"] in {"x86_64", "aarch64"}
            and bolt_data.get("build_id") and shutil.which("llvm-bolt") and bolt_hook(kernel, x86)):
        os.environ.update(UFLINUX_BOLT_PROFILE=str(bolt), UFLINUX_BOLT_BUILD_ID=bolt_data["build_id"])
        if x86:
            os.environ["UFLINUX_BOLT_X86_BUG_SIZE"] = str(bug_entry_size)
    compression = {
        "zram": ("ZRAM", "ZRAM_DEF_COMP", "CRYPTO_LZ4", "CRYPTO_ZSTD"),
        "zswap": ("ZSWAP", "ZSWAP_COMPRESSOR_DEFAULT", "CRYPTO_LZ4", "CRYPTO_ZSTD"),
        "f2fs": ("F2FS_FS_COMPRESSION", "F2FS_FS_LZ4", "F2FS_FS_ZSTD"),
    }
    for feature, symbols in compression.items():
        if any(key not in settings for key in symbols):
            raise SystemExit(f"{feature}: compression settings did not survive Kconfig")
    if settings["ZRAM_DEF_COMP"] != '"zstd"' or settings["ZSWAP_COMPRESSOR_DEFAULT"] != '"zstd"':
        raise SystemExit("zram and zswap must default to Zstandard")
    if "ZRAM_BACKEND_LZ4" in settings and "ZRAM_BACKEND_ZSTD" not in settings:
        raise SystemExit("Missing zram Zstandard backend")
    image = obj / target["image"]
    goals = [image.name, "modules"]
    if target["arch"] != "x86" and not name.startswith("virt-"):
        goals.append("dtbs")
    jobs = f"-j{min(4, os.cpu_count() or 1)}"
    omissions = {}
    try:
        run(*kbuild, *flags, jobs, *goals)
    except subprocess.CalledProcessError:
        if not flags or pgo or args.profile_build:
            raise
        print("Build failed at -O3, retrying with Kbuild's optimization level", flush=True)
        (obj / ".uflinux-bolt-applied").unlink(missing_ok=True)
        run(*kbuild, jobs, *goals)
        flags = []
        omissions["O3"] = "Build succeeded only with Kbuild's optimization level"
    machine = {
        "x86_64": r"Advanced Micro Devices X86-64",
        "aarch64": r"AArch64", "riscv64": r"RISC-V",
    }[target["apk"]]
    header = run("readelf", "-h", str(obj / "vmlinux"), capture=True)
    if not re.search(rf"^\s*Machine:\s*{machine}\s*$", header, re.M):
        raise SystemExit(f"Unexpected kernel ELF machine for {target['apk']}")
    abi = run(*kbuild, "-s", "kernelrelease", capture=True).strip()
    stage(kernel, obj, output / "stage", name, target, kbuild, image, abi)
    module_key = (settings.get("MODULE_SIG_KEY") or '""').strip('"')
    if settings.get("MODULE_SIG_ALL") == "y" and module_key:
        # Publish the certificate modules are signed against. The private key stays in the
        # object tree only while a BOLT repack still needs it, so it never reaches an artifact.
        certificate = obj / "certs" / "signing_key.x509"
        if not certificate.is_file():
            raise SystemExit(f"{name}: module signing produced no certificate")
        shutil.copy2(certificate, output / "stage" / "boot" / "uflinux-module-cert.x509")
        (obj / module_key).unlink(missing_ok=True)
    shutil.copy2(obj / ".config", output / "kernel.config")
    if args.profile_build or args.bolt_baseline:
        unstripped = obj / "vmlinux.unstripped"
        if args.bolt_baseline and not unstripped.is_file():
            raise SystemExit(f"{name}: missing the unstripped ELF needed for BOLT")
        shutil.copy2(unstripped if unstripped.is_file() else obj / "vmlinux", output / "vmlinux")
        if args.profile_build:
            kernel_pgo.elf_regions(output / "vmlinux")
    bolt_applied = (obj / ".uflinux-bolt-applied").exists()
    if not pgo:
        omissions["PGO"] = "No matching UFlinux instrumentation profile"
    if not bolt_applied:
        omissions["BOLT"] = "No compatible Linux kernel rewriter or exact matching profile, or BOLT failed"
    if settings.get("LTO_CLANG_THIN_DIST") != "y":
        omissions["Distributed ThinLTO"] = "Unavailable in the effective Kconfig"
    if settings.get("LRU_GEN_ENABLED") != "y":
        omissions["MGLRU"] = "No MGLRU boot-default support in the effective Kconfig"
    metadata = {"target": name, "arch": target["apk"], "version": target["version"],
                "source": target["ref"], "source_url": target["url"], "abi": abi, "patches": records,
                "pgo": bool(pgo), "pgo_instrumented": args.profile_build, "bolt": bolt_applied, "omissions": omissions,
                "bbrv3": bbr[0], "mglru": settings.get("LRU_GEN_ENABLED") == "y",
                "lto": next((key for key in ("LTO_CLANG_THIN_DIST", "LTO_CLANG_THIN") if settings.get(key) == "y"), "none"),
                "optimization": "-O3" if flags else "Kbuild default",
                "config_sha256": hashlib.sha256((obj / ".config").read_bytes()).hexdigest(),
                "module_signing_key": module_key,
                "compiler": compiler, "pgo_identity": identity,
                "pgo_sha256": pgo_data["sha256"] if pgo else None}
    if args.bolt_baseline and x86:
        metadata["bolt_bug_entry_size"] = bug_entry_size
    write_build_metadata(output, name, metadata)


if __name__ == "__main__":
    main()
