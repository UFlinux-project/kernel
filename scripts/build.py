#!/usr/bin/env python3
"""Fetch, patch, configure, and stage one kernel for Alpine packaging."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from resolve import ROOT, fetch


def run(*args, cwd=None, capture=False):
    return subprocess.run(args, cwd=cwd, check=True, text=True,
                          stdout=subprocess.PIPE if capture else None).stdout


def values(config):
    return dict(re.findall(r"^CONFIG_(\w+)=(.*)$", config.read_text(), re.M))


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


def configure(kernel, obj, target, name, kbuild, bbr, autofdo, gcc=False):
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
    set_config("--set-str", "LOCALVERSION", f"-uflinux-{name}", "-d", "LOCALVERSION_AUTO",
               "-e", "MODULES", "-d", "MODULE_SIG", "-d", "MODULE_SIG_ALL", "--set-str", "SYSTEM_TRUSTED_KEYS", "",
               "--set-str", "SYSTEM_REVOCATION_KEYS", "", "-d", "WERROR", "-d", "RUST",
               "-d", "GCC_PLUGINS", "-e", "DEBUG_INFO_NONE", "-d", "DEBUG_INFO_BTF", "-d", "DEBUG_INFO_BTF_MODULES",
               "-e", "TCP_CONG_ADVANCED", "-e", "TCP_CONG_CUBIC", "-e", symbol, "-e", selector,
               "-e", "LRU_GEN", "-e", "LRU_GEN_ENABLED", "-e", "SWAP", "-e", "ZSWAP", "-d", "ZSWAP_DEFAULT_ON",
               "-e", "ZSWAP_COMPRESSOR_DEFAULT_ZSTD", "-e", "CRYPTO_LZ4", "-e", "CRYPTO_ZSTD",
               "-m", "ZRAM", "-e", "ZRAM_BACKEND_LZ4", "-e", "ZRAM_BACKEND_ZSTD",
               "-e", "ZRAM_DEF_COMP_ZSTD", "-m", "F2FS_FS", "-e", "F2FS_FS_COMPRESSION",
               "-e", "F2FS_FS_LZ4", "-e", "F2FS_FS_ZSTD")
    if name == "rpi":
        set_config("-e", "ARM64_4K_PAGES")
    virt = name.startswith("virt-")
    for driver in ("DRM_AMDGPU", "DRM_RADEON", "DRM_NOUVEAU", "DRM_XE"):
        set_config("-d" if virt else "-m", driver)
    set_config("-m" if name in {"x86", "x86_64"} else "-d", "DRM_I915")
    if virt:
        set_config("-e", "PCI", "-e", "EFI", "-e", "EFI_STUB", "-e", "VIRTIO_PCI",
                   "-e", "VIRTIO_MMIO", "-e", "VIRTIO_BLK", "-e", "SCSI", "-e", "BLK_DEV_SD",
                   "-e", "SCSI_VIRTIO", "-e", "VIRTIO_NET", "-e", "VIRTIO_CONSOLE",
                   "-e", "DRM", "-m", "DRM_VIRTIO_GPU", "-e", "DEVTMPFS", "-e", "BLK_DEV_INITRD",
                   "-e", "EXT4_FS", "-e", "SERIAL_8250_CONSOLE" if target["arch"] == "x86"
                   else "SERIAL_AMBA_PL011_CONSOLE")
    set_config("-e" if autofdo else "-d", "AUTOFDO_CLANG", "-d", "LTO_CLANG_FULL")
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
    expected_i915 = name in {"x86", "x86_64"}
    if (expected_i915 and effective.get("DRM_I915") != "m") or (
            not expected_i915 and effective.get("DRM_I915") in {"y", "m"}):
        raise SystemExit("Unexpected i915 configuration")
    if name == "rpi" and effective.get("ARM64_4K_PAGES") != "y":
        raise SystemExit("Raspberry Pi needs a 4 KiB kernel")
    return effective


def profile(name, kind, target):
    folder = ROOT / "profiles" / name
    path = folder / ("kernel.afdo" if kind == "autofdo" else "kernel.fdata")
    metadata = folder / f"{kind}.json"
    if not path.is_file() or not metadata.is_file():
        return None, {}
    data = json.loads(metadata.read_text())
    with path.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    if data.get("source") != target["ref"] or data.get("sha256") != checksum:
        return None, {}
    return path, data


def bolt_hook(kernel):
    script = kernel / "scripts/link-vmlinux.sh"
    text = script.read_text()
    marker = "# For fixdep"
    if text.count(marker) != 1:
        return False
    block = r'''
if [ -n "${UFLINUX_BOLT_PROFILE:-}" ]; then
    bolt_input=${VMLINUX:-vmlinux}
    build_id=$(readelf -n "$bolt_input" | awk '/Build ID:/ {print $3; exit}')
    if [ -n "$build_id" ] && [ "$build_id" = "$UFLINUX_BOLT_BUILD_ID" ]; then
        if llvm-bolt "$bolt_input" -o "$bolt_input.bolt" --data "$UFLINUX_BOLT_PROFILE" \
            --eliminate-unreachable=0 --reorder-blocks=ext-tsp \
            --simplify-conditional-tail-calls=0 --skip-funcs=__entry_text_start,irq_entries_start; then
            mv "$bolt_input.bolt" "$bolt_input"
            touch .uflinux-bolt-applied
        else
            rm -f "$bolt_input.bolt"
            echo "BOLT failed, using the linked vmlinux" >&2
        fi
    fi
fi
'''
    script.write_text(text.replace(marker, block + "\n" + marker))
    return True


def stage(kernel, obj, dest, name, target, kbuild, image, abi):
    boot = dest / "boot"
    boot.mkdir(parents=True)
    shutil.copy2(image, boot / f"vmlinuz-uflinux-{name}")
    shutil.copy2(obj / ".config", boot / f"config-uflinux-{name}")
    run(*kbuild, f"INSTALL_MOD_PATH={dest}", "INSTALL_MOD_STRIP=1", "modules_install")
    modules = dest / "lib/modules" / abi
    for link in ("build", "source"):
        (modules / link).unlink(missing_ok=True)
    (modules / "kernel-suffix").write_text(f"-uflinux-{name}\n")
    (modules / "vmlinuz").symlink_to(f"/boot/vmlinuz-uflinux-{name}")
    if name in {"x86", "x86_64"} and not list(modules.rglob("i915.ko*")):
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("target")
    parser.add_argument("resolved", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--profile-build", action="store_true")
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    name = args.target
    target = json.loads(args.resolved.read_text())["targets"][name]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    kernel, obj = output / "kernel", output / "obj"
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

    os.environ.update(KBUILD_BUILD_USER="uflinux", KBUILD_BUILD_HOST="kernel",
                      KBUILD_BUILD_TIMESTAMP="Thu Jan  1 00:00:00 UTC 1970", KBUILD_BUILD_VERSION="1",
                      SOURCE_DATE_EPOCH="0", KCONFIG_NOTIMESTAMP="1", LC_ALL="C")
    gcc = target.get("compiler") == "gcc"
    toolchain = ["CC=gcc", "HOSTCC=gcc", "HOSTCXX=g++"] if gcc else ["LLVM=1", "LLVM_IAS=1"]
    kbuild = ["make", "-C", str(kernel), f"O={obj}", f"ARCH={target['arch']}", "LOCALVERSION=", *toolchain]
    afdo, afdo_data = profile(name, "autofdo", target)
    bolt, bolt_data = profile(name, "bolt", target)
    settings = configure(kernel, obj, target, name, kbuild, bbr, bool(afdo) or args.profile_build, gcc)
    baseline_config = hashlib.sha256((obj / ".config").read_bytes()).hexdigest()
    if afdo and afdo_data.get("config_sha256") != baseline_config:
        afdo = None
        settings = configure(kernel, obj, target, name, kbuild, bbr, args.profile_build, gcc)
    if afdo and settings.get("AUTOFDO_CLANG") == "y":
        kbuild.append(f"CLANG_AUTOFDO_PROFILE={afdo}")
    else:
        afdo = None
    if bolt and not args.profile_build and target["apk"] == "x86_64" and bolt_data.get("build_id") and shutil.which("llvm-bolt") and bolt_hook(kernel):
        os.environ.update(UFLINUX_BOLT_PROFILE=str(bolt), UFLINUX_BOLT_BUILD_ID=bolt_data["build_id"])
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
    flags = ["KCFLAGS=-O3"] if not target.get("disable_o3") else []
    image = obj / target["image"]
    goals = [image.name, "modules"]
    if target["arch"] != "x86" and not name.startswith("virt-"):
        goals.append("dtbs")
    jobs = f"-j{min(4, os.cpu_count() or 1)}"
    omissions = {}
    try:
        run(*kbuild, *flags, jobs, *goals)
    except subprocess.CalledProcessError:
        if not flags:
            raise
        print("Build failed at -O3, retrying with Kbuild's optimization level", flush=True)
        (obj / ".uflinux-bolt-applied").unlink(missing_ok=True)
        run(*kbuild, jobs, *goals)
        flags = []
        omissions["O3"] = "Build succeeded only with Kbuild's optimization level"
    machine = {
        "x86": r"Intel 80386", "x86_64": r"Advanced Micro Devices X86-64",
        "aarch64": r"AArch64", "riscv64": r"RISC-V",
    }[target["apk"]]
    header = run("readelf", "-h", str(obj / "vmlinux"), capture=True)
    if not re.search(rf"^\s*Machine:\s*{machine}\s*$", header, re.M):
        raise SystemExit(f"Unexpected kernel ELF machine for {target['apk']}")
    abi = run(*kbuild, "-s", "kernelrelease", capture=True).strip()
    stage(kernel, obj, output / "stage", name, target, kbuild, image, abi)
    shutil.copy2(obj / ".config", output / "kernel.config")
    if args.profile_build:
        unstripped = obj / "vmlinux.unstripped"
        shutil.copy2(unstripped if unstripped.is_file() else obj / "vmlinux", output / "vmlinux")
    bolt_applied = (obj / ".uflinux-bolt-applied").exists()
    if not afdo:
        omissions["AutoFDO"] = "No compatible UFlinux sample profile or Kconfig support"
    if not bolt_applied:
        omissions["BOLT"] = "No supported x86_64 kernel hook, LLVM BOLT tool, or exact matching profile"
    if settings.get("LTO_CLANG_THIN_DIST") != "y":
        omissions["Distributed ThinLTO"] = "Unavailable in the effective Kconfig"
    if settings.get("LRU_GEN_ENABLED") != "y":
        omissions["MGLRU"] = "No MGLRU boot-default support in the effective Kconfig"
    metadata = {"target": name, "arch": target["apk"], "version": target["version"],
                "source": target["ref"], "source_url": target["url"], "abi": abi, "patches": records,
                "autofdo": bool(afdo), "bolt": bolt_applied, "omissions": omissions,
                "bbrv3": bbr[0], "mglru": settings.get("LRU_GEN_ENABLED") == "y",
                "lto": next((key for key in ("LTO_CLANG_THIN_DIST", "LTO_CLANG_THIN") if settings.get(key) == "y"), "none"),
                "optimization": "-O3" if flags else "Kbuild default",
                "config_sha256": hashlib.sha256((obj / ".config").read_bytes()).hexdigest(),
                "compiler": run("gcc" if gcc else "clang", "--version", capture=True).splitlines()[0]}
    (output / "build.json").write_text(json.dumps(metadata, indent=2) + "\n")
    run("tar", "--exclude=.git", "-I", "zstd -T2", "-cf", str(output / f"source-{name}.tar.zst"),
        "-C", str(output), "kernel", "kernel.config", "build.json")


if __name__ == "__main__":
    main()
