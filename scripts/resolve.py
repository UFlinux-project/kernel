#!/usr/bin/env python3
"""Resolve moving upstream inputs once for the entire release matrix."""

import argparse
import hashlib
import json
import os
import io
import tarfile
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
ALPINE = "https://dl-cdn.alpinelinux.org/alpine"
API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
PLATFORMS = {"x86_64": "amd64", "aarch64": "arm64", "riscv64": "riscv64"}


def fetch(url, *, missing=False):
    headers = {"User-Agent": "uflinux-kernel-builder"}
    token = os.getenv("GH_TOKEN")
    if token and urllib.parse.urlparse(url).hostname == "api.github.com":
        headers["Authorization"] = f"Bearer {token}"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if missing and error.code == 404:
                return None
            if error.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise


def github(path, *, missing=False):
    data = fetch(f"{API}/repos/{path}", missing=missing)
    return json.loads(data) if data is not None else None


def commit(repo, ref):
    return github(f"{repo}/commits/{urllib.parse.quote(ref, safe='')}")["sha"]


def raw(repo, ref, path):
    return f"{RAW}/{repo}/{ref}/{path}"


def tree(repo, ref, recursive=False):
    suffix = "?recursive=1" if recursive else ""
    return github(f"{repo}/git/trees/{ref}{suffix}")["tree"]


def gh_source(repo, ref):
    makefile = fetch(raw(repo, ref, "Makefile")).decode()
    parts = [re.search(rf"^{name}\s*=\s*(\d+)\s*$", makefile, re.M)[1]
             for name in ("VERSION", "PATCHLEVEL", "SUBLEVEL")]
    extra = re.search(r"^EXTRAVERSION\s*=([^\n]*)", makefile, re.M)[1].strip()
    if extra.startswith("-rc"):
        raise ValueError(f"Release candidate selected for {repo}: {ref}")
    return {"version": ".".join(parts), "ref": ref,
            "url": f"https://codeload.github.com/{repo}/tar.gz/{ref}"}


def stable():
    data = json.loads(fetch("https://www.kernel.org/releases.json"))
    version = data["latest_stable"]["version"]
    release = next(item for item in data["releases"] if item["version"] == version)
    return {"version": version, "ref": f"v{version}", "url": release["source"]}


def orange():
    repo = "orangepi-xunlong/orangepi-build"
    ref = commit(repo, github(repo)["default_branch"])
    prefix = "external/config"
    board = fetch(raw(repo, ref, f"{prefix}/boards/orangepirv2.conf")).decode()
    branch = re.search(r'^KERNEL_TARGET="([^"]+)"', board, re.M)[1]
    family = fetch(raw(repo, ref, f"{prefix}/sources/families/ky.conf")).decode()
    stanza = re.search(rf"^\s*{re.escape(branch)}\)(.*?)^\s*;;", family, re.M | re.S)[1]
    kernel_branch = re.search(r'KERNELBRANCH="branch:([^"]+)"', stanza)[1]
    kernel = "orangepi-xunlong/linux-orangepi"
    source = gh_source(kernel, commit(kernel, kernel_branch))
    patches = [item["path"] for item in tree(repo, ref, True)
               if re.search(rf"/ky-{re.escape(branch)}/[^/]+\.patch$", item["path"])]
    source.update(recipe=ref, config_url=raw(repo, ref, f"{prefix}/kernel/linux-ky-{branch}.config"),
                  dtbs=[re.search(r'^BOOT_FDT_FILE="([^"]+)"', board, re.M)[1]],
                  vendor_patches=[raw(repo, ref, path) for path in sorted(patches)])
    return source


def radxa():
    repo = "radxa-pkg/linux-qcom"
    latest = github(f"{repo}/releases/latest")
    releases = [latest]
    if not any(a["name"].startswith("linux-image-radxa-dragon-q6a_") for a in latest["assets"]):
        releases = github(f"{repo}/releases?per_page=100")
    release = next(item for item in releases if not (item["draft"] or item["prerelease"])
                   and any(a["name"].startswith("linux-image-radxa-dragon-q6a_") for a in item["assets"]))
    ref = commit(repo, release["tag_name"])
    source_ref = next(item["sha"] for item in tree(repo, ref) if item["path"] == "src")
    source = gh_source("radxa/kernel", source_ref)
    branch = source["version"].rsplit(".", 1)[0].replace(".", "_")
    config = f"radxa_qcom_{branch}_defconfig"
    if fetch(raw("radxa/kernel", source_ref, f"arch/arm64/configs/{config}"), missing=True):
        source["defconfig"] = [config]
    else:
        vendor = fetch(raw(repo, ref, ".github/local/Makefile.local"), missing=True)
        rule = re.search(r"^KERNEL_DEFCONFIG\s*:?=\s*(.+)$", vendor.decode(), re.M) if vendor else None
        if not rule:
            raise ValueError("Could not resolve the Radxa Q6A vendor defconfig")
        source["defconfig"] = rule[1].split()
    source.update(recipe=ref, vendor_release=release["tag_name"])
    return source


def pi():
    repo = "raspberrypi/firmware"
    release = github(f"{repo}/releases/latest")["tag_name"]
    ref = fetch(raw(repo, release, "extra/git_hash")).decode().strip()
    if not re.fullmatch(r"[a-f0-9]{40}", ref):
        raise ValueError("Invalid Raspberry Pi firmware kernel commit")
    source = gh_source("raspberrypi/linux", ref)
    source.update(vendor_release=release)
    return source


def alpine_root(arch):
    listing = fetch(f"{ALPINE}/latest-stable/releases/{arch}/latest-releases.yaml").decode()
    match = re.search(r"^  file: (alpine-minirootfs-([0-9.]+)-[^\s]+\.tar\.gz)$", listing, re.M)
    version = match[2]
    series = "v" + ".".join(version.split(".")[:2])
    url = f"{ALPINE}/{series}/releases/{arch}/{match[1]}"
    return {"version": version, "series": series, "url": url, "platform": PLATFORMS[arch],
            "sha256": fetch(f"{url}.sha256").decode().split()[0]}


def toolchain(alpine, arch):
    url = f"{ALPINE}/{alpine['series']}/main/{arch}/APKINDEX.tar.gz"
    with tarfile.open(fileobj=io.BytesIO(fetch(url))) as archive:
        blocks = archive.extractfile("APKINDEX").read().decode().split("\n\n")
    packages = {}
    for block in blocks:
        fields = dict(line.split(":", 1) for line in block.splitlines() if ":" in line)
        if re.fullmatch(r"(clang|lld|llvm|llvm-bolt|bolt)\d*|gcc|binutils|abuild", fields.get("P", "")):
            packages[fields["P"]] = fields["V"]
    versions = {int(pkg[5:]) for pkg in packages if re.fullmatch(r"clang\d+", pkg)}
    major = max(v for v in versions if f"lld{v}" in packages and f"llvm{v}" in packages)
    return {"major": major, "versions": packages}


def topic_patch(series):
    repo = "CachyOS/linux"
    ref = urllib.parse.quote(f"{series}/bbr3", safe="")
    history = github(f"{repo}/commits?sha={ref}&per_page=12", missing=True)
    if not history:
        return []
    for entry in history:
        title = entry["commit"]["message"].splitlines()[0]
        if re.search(r"add BBRv3 congestion control", title, re.I):
            base = entry["parents"][0]["sha"]
            return [f"https://github.com/{repo}/compare/{base}...{history[0]['sha']}.diff"]
    return []


def candidates(repo, ref, paths, pattern, series, *, previous=0):
    regex = re.compile(pattern)
    major, minor = map(int, series.split("."))
    compatible_series = {f"{major}.{sublevel}" for sublevel in range(max(0, minor - previous), minor + 1)}
    matches = [(tuple(int(x) for x in re.findall(r"\d+", Path(path).name)), path)
               for path in paths if (match := regex.search(path)) and match[1] in compatible_series]
    return [raw(repo, ref, path) for _, path in sorted(matches, reverse=True)]


def resolve(selected=None):
    targets = json.loads((ROOT / "targets.json").read_text())
    if selected:
        targets = {selected: targets[selected]}
    resolvers = {"stable": stable, "orange": orange, "radxa": radxa, "pi": pi}
    sources = {name: resolvers[name]() for name in sorted({t["source"] for t in targets.values()})}
    alpine = {arch: alpine_root(arch) for arch in sorted({t["apk"] for t in targets.values()} | {"x86_64", "aarch64"})}
    series = alpine["x86_64"]["series"]
    if any(item["series"] != series for item in alpine.values()):
        raise ValueError("Alpine mirrors disagree on the current stable series")
    aports = commit("alpinelinux/aports", f"{series[1:]}-stable")
    patch_repos = {}
    for name, repo, branch in [
        ("bore", "firelzrd/bore-scheduler", "main"),
        ("le9uo", "firelzrd/le9uo", "main"),
        ("cachy", "CachyOS/kernel-patches", "master"),
    ]:
        ref = commit(repo, branch)
        patch_repos[name] = (repo, ref, [item["path"] for item in tree(repo, ref, True)])
    groups = {}
    for source in sources.values():
        kernel_series = source["version"].rsplit(".", 1)[0]
        if kernel_series in groups:
            continue
        bore = candidates(*patch_repos["bore"], r"^patches/stable/[^/]*linux(\d+\.\d+)\D", kernel_series)
        bore += candidates(*patch_repos["cachy"], r"^(\d+\.\d+)/sched/[^/]*-bore\.patch$", kernel_series)
        le9 = candidates(*patch_repos["le9uo"], r"^le9uo_patches/stable/base/[^/]*linux(\d+\.\d+)\D", kernel_series)
        le9 += candidates(*patch_repos["cachy"], r"^(\d+\.\d+)/misc/[^/]*le9uo\.patch$", kernel_series)
        nearby = candidates(*patch_repos["le9uo"], r"^le9uo_patches/stable/base/[^/]*linux(\d+\.\d+)\D",
                            kernel_series, previous=2)
        le9.extend(url for url in nearby if url not in le9)
        bbr = topic_patch(kernel_series)
        bbr.extend(candidates(*patch_repos["cachy"], r"^(\d+\.\d+)/[^/]*bbr3\.patch$", kernel_series))
        groups[kernel_series] = {"bore": bore, "le9uo": le9, "bbr": bbr}
    for name, target in targets.items():
        target.update(sources[target["source"]])
        target.update(groups[target["version"].rsplit(".", 1)[0]])
        if "config" in target:
            target["config_url"] = raw("alpinelinux/aports", aports, f"main/linux-lts/{target['config']}")
        target["alpine"] = alpine[target["apk"]]
        target["builder"] = "aarch64" if target["arch"] == "arm64" else "x86_64"
    return {"targets": targets, "alpine": alpine, "aports": aports,
            "toolchains": {arch: toolchain(alpine[arch], arch) for arch in ("x86_64", "aarch64")}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--target")
    parser.add_argument("--output", type=Path, default=Path("work/resolved.json"))
    args = parser.parse_args()
    result = resolve(args.target)
    canonical = json.dumps(result, sort_keys=True).encode()
    result["fingerprint"] = hashlib.sha256(canonical).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
