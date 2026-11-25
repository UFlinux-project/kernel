"""Install selective kernel instrumentation and convert its counters to LLVM PGO."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess

# Keep collection and profile use on the same built-in code. Early boot,
# entry code, the counter exporter, and modules are outside these directories.
DIRECTORIES = "block fs/ext4 mm net/core net/ipv4 net/ipv6"
COUNTER_FLAGS = "-mllvm -disable-vp"
COLLECTION_FLAGS = f"-fprofile-generate {COUNTER_FLAGS}"
RAW_FORMATS = {21: (10, 64), 22: (10, 64), 23: (11, 72)}


def install(kernel, collect):
    makefile = kernel / "scripts/Makefile.lib"
    makefile.write_text(makefile.read_text() + f"""
# Apply profile flags only to selected built-in C objects.
_c_flags += $(if $(and $(part-of-builtin),$(if $(part-of-module),,y),$(filter {DIRECTORIES},$(obj))),$(UFLINUX_PGO_FLAGS))
""")
    # Clang can defer PGO passes to the ThinLTO linker backend.
    with (kernel / "Makefile").open("a") as makefile:
        makefile.write(f"\nKBUILD_LDFLAGS += {COUNTER_FLAGS}\n")
    if not collect:
        return

    linker = kernel / "include/asm-generic/vmlinux.lds.h"
    text = linker.read_text()
    marker = re.search(r"^#define DATA_DATA\s*\\$", text, re.M)
    if not marker:
        raise SystemExit("Cannot locate the kernel data linker layout for PGO")
    regions = ""
    for name, section in (("data", "data"), ("counters", "cnts"), ("names", "names")):
        symbol = f"__uflinux_pgo_{name}"
        regions += f"\n\t. = ALIGN(8); {symbol} = .; KEEP(*(__llvm_prf_{section})) {symbol}_end = .; \\"
    linker.write_text(text[:marker.end()] + regions + text[marker.end():])
    (kernel / "kernel/uflinux-pgo.c").write_bytes(Path(__file__).with_name("kernel-pgo.c").read_bytes())
    with (kernel / "kernel/Makefile").open("a") as makefile:
        makefile.write("\nobj-y += uflinux-pgo.o\n")


def checksum(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def elf_regions(elf):
    symbols = subprocess.check_output(["nm", str(elf)], text=True)
    addresses = {name: int(address, 16) for address, name in
                 re.findall(r"^([0-9a-f]+) \w __uflinux_pgo_(\w+)$", symbols, re.M)}
    sections = subprocess.check_output(["readelf", "-SW", str(elf)], text=True)
    section = re.search(r"\]\s+\.data\s+PROGBITS\s+([0-9a-f]+)\s+([0-9a-f]+)\s+([0-9a-f]+)", sections)
    if not section:
        raise SystemExit("Missing kernel ELF data section")
    base, offset, length = (int(value, 16) for value in section.groups())
    regions = {}
    with elf.open("rb") as stream:
        if stream.read(6) != b"\x7fELF\x02\x01":
            raise SystemExit("Kernel PGO requires a little-endian 64-bit ELF")
        for name in ("data", "counters", "names"):
            start, end = addresses.get(name, 0), addresses.get(f"{name}_end", 0)
            if not start or not base <= start < end <= base + length:
                raise SystemExit(f"Invalid PGO {name} region in the kernel ELF")
            stream.seek(offset + start - base)
            regions[name] = (start, stream.read(end - start))
    return regions


def merge(output, profiles, profdata):
    manifest = json.loads((output / "build.json").read_text())
    identity = manifest["pgo_identity"]
    if not manifest["pgo_instrumented"]:
        raise SystemExit("Expected an instrumented kernel")
    major = int(re.search(r"clang version (\d+)\.", identity["compiler"])[1])
    if major not in RAW_FORMATS:
        raise SystemExit(f"Unsupported LLVM instrumentation profile ABI: {major}")
    version, record_size = RAW_FORMATS[major]
    regions = elf_regions(output / "vmlinux")
    data_address, data = regions["data"]
    counters_address, counters = regions["counters"]
    names_address, names = regions["names"]
    if len(data) % record_size or len(counters) % 8:
        raise SystemExit("Unexpected LLVM instrumentation section layout")

    size = len(counters)
    before = (output / "pgo-before").read_bytes()[:size]
    after = (output / "pgo-after").read_bytes()[:size]
    if len(before) != size or len(after) != size:
        raise SystemExit("Incomplete guest counter snapshot")
    counts = [(end - start) % (1 << 64) for (start,), (end,) in
              zip(struct.iter_unpack("<Q", before), struct.iter_unpack("<Q", after), strict=True)]
    if not any(counts) or any(count > (1 << 63) for count in counts):
        raise SystemExit("Invalid guest counter snapshots")

    # LLVM InstrProfData.inc's 64-bit raw header, with no bitmap, binary IDs,
    # uniform counters, vtables or value profile payload.
    header = [0xff6c70726f667281, (1 << 56) | version, 0, len(data) // record_size,
              0, len(counts), 0, 0, 0]
    if version == 11:
        header.extend((0, 0, 0))
    header.extend((len(names), (counters_address - data_address) % (1 << 64),
                   0, names_address, 0, 0, 2))
    raw = output / "kernel.profraw"
    raw.write_bytes(struct.pack(f"<{len(header)}Q", *header) + data +
                    struct.pack(f"<{len(counts)}Q", *counts) + names + bytes(-len(names) % 8))
    profiles.mkdir(parents=True, exist_ok=True)
    profile = profiles / "kernel.profdata"
    subprocess.run([profdata, "merge", "-o", str(profile), str(raw)], check=True)
    report = subprocess.check_output([profdata, "show", str(profile)], text=True)
    if not re.search(r"^Maximum function count: [1-9]\d*$", report, re.M):
        raise SystemExit("LLVM profile contains no executed functions")
    metadata = {"source": manifest["source"], "identity": identity, "sha256": checksum(profile)}
    (profiles / "pgo.json").write_text(json.dumps(metadata, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("profiles", type=Path)
    parser.add_argument("profdata")
    args = parser.parse_args()
    merge(args.output, args.profiles, args.profdata)
