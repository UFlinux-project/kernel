# UFlinux kernel builder

GitHub Actions builds installable Alpine APKs for four generic or virtual-machine targets and three board targets. A nightly run resolves upstream sources and skips a release whose inputs have already been published. A manual run can build the whole matrix or export a profiling kernel for a single flavor.

| Target | Alpine architecture | Kernel source |
|---|---|---|
| `x86_64`, `aarch64` | Matching Alpine architecture | Latest stable kernel.org release, with Alpine's stable generic configs |
| `virt-x86_64`, `virt-aarch64` | `x86_64`, `aarch64` | Same kernel.org source, with Alpine's stable VirtIO guest configs |
| `orangepi-rv2` | `riscv64` | Orange Pi's current RV2 vendor recipe |
| `dragon-q6a` | `aarch64` | The kernel referenced by Radxa's latest stable Q6A `linux-qcom` release |
| `rpi` | `aarch64` | The kernel referenced by the latest stable Pi firmware release, with one 4 KiB `kernel8.img` for Pi 3, 4, and 5 |

The x86_64 targets run on x86_64 workers. ARM64 targets use ARM64 workers. Orange Pi uses x86_64 Clang to cross-compile and QEMU user emulation for RISC-V APK packaging and install validation.

## Build locally

Install Python 3, Bash, curl, jq, and Docker. Install the corresponding QEMU system emulator to run the boot tests, and register RISC-V binfmt emulation on x86_64 to package Orange Pi. Podman is also supported via `CONTAINER_RUNTIME=podman`.

```sh
python3 scripts/resolve.py --target virt-x86_64
scripts/container.sh virt-x86_64 work/resolved.json work/virt-x86_64
scripts/boot-test.sh virt-x86_64 work/virt-x86_64
```

Use a fresh output directory when repeating a build. Run `python3 scripts/resolve.py` to resolve the entire matrix. For source and patch validation without compilation, run `python3 scripts/build.py virt-x86_64 work/resolved.json work/patch-check --prepare-only`. A GitHub token in `GH_TOKEN` can raise the source resolver's API rate limit.

Place target patches under `patches/<target>/` using numbered `.patch` filenames. Required local and vendor patches apply in order before BBRv3, BORE, and le9uo. BBRv3 is required and is built in as the default TCP congestion control. MGLRU is enabled at boot where the final Kconfig supports it. BORE and le9uo are used when the kernel series has a patch that applies cleanly. Inspect `build-<target>.json` in the release for patch hashes, feature status, and omitted optimizations.

The generic kernels keep Alpine's CPU baselines. Optimizations use Clang, ThinLTO in distributed mode where supported, and `-O3` when the kernel compiles with it. The BOLT integration uses the exact linked binary and runs before the boot image is generated.

### UFlinux profiles

AutoFDO and BOLT profiles are accepted for `x86_64`, `virt-x86_64`, `aarch64`, `virt-aarch64`, `dragon-q6a`, and `rpi` when the kernel and tools support them. For an AutoFDO collection kernel, choose a target in the workflow's `profile_target` input or add `--profile-build` as the fourth argument to `scripts/container.sh`. The profiling build exports `vmlinux` and a manifest. Use a representative workload and the kernel’s [AutoFDO instructions](https://docs.kernel.org/dev-tools/autofdo.html) to create `profiles/<target>/kernel.afdo`. Add `autofdo.json` with `source`, `sha256`, and `config_sha256` from the collection build and the profile file. On ARM64, collect samples on hardware that can expose a supported trace source such as SPE or CoreSight ETM.

For BOLT, profile the exact baseline kernel. Use `obj/vmlinux.unstripped` from a normal local build without a BOLT profile. If using AutoFDO too, make the AutoFDO profile available under `profiles/<target>/` before building that baseline. Add `profiles/<target>/kernel.fdata` and `bolt.json` with the kernel `source`, profile `sha256`, and baseline ELF `build_id` from `readelf -n vmlinux`. Keep the compiler, build configuration, and AutoFDO inputs identical to the baseline. See the [LLVM BOLT kernel instructions](https://github.com/llvm/llvm-project/blob/main/bolt/docs/OptimizingLinux.md) for the x86_64 collection workflow.

On x86_64, build the kernel rewriter with `scripts/build-kernel-bolt.sh <tools>` on Ubuntu 26.04 with the LLVM 22.1.2 development package. On ARM64, supply a rewriter with AArch64 Linux kernel support. Locally, provide a container-compatible executable at `<output>/bolt/llvm-bolt`, or pass a native executable as `UFLINUX_BOLT_BINARY` to `scripts/prepare-bolt.sh <target> <output>`. For CI builds using checked-in ARM64 profiles, `KERNEL_AARCH64_BUILD_RUNNER` can select a native runner with a compatible `llvm-bolt` on PATH. Profiles are optional for ordinary builds, and single-target profiling runs produce artifacts without publishing a release.

To generate profiles during a full release, dispatch the workflow on the default branch with `profile_target=none` and `generate_release_profiles=true`. The **profile-release** job records kernel execution in QEMU's TCG emulator during mixed block I/O and loopback TCP, then converts the execution counts to profiles.

The workflow profiles `x86_64`, `virt-x86_64`, `aarch64`, and `virt-aarch64` independently and requires each release kernel to apply its own generated AutoFDO profile. `dragon-q6a` and `rpi` consume profiles supplied for their own board kernels.

The second pass profiles the AutoFDO kernel so the BOLT profile matches its exact ELF build ID. On x86_64, the workflow builds a patched LLVM 22 rewriter for modern kernel metadata and preserves the original KASLR relocation sites. The build job uses the same rewriter that converted the trace, requires the rewrite to succeed, and boots the packaged image in QEMU before publication.

GitHub-hosted ARM64 runners generate AutoFDO profiles. To enable the second pass for both ARM64 targets, set `KERNEL_AARCH64_BOLT_RUNNER` to an Ubuntu 26.04 ARM64 runner label with a compatible `llvm-bolt` on PATH. The supplied rewriter must support pre-aggregated traces and in-place kernel optimization. Upstream AArch64 kernel support remains in development. [LLVM AArch64 kernel BOLT proposal](https://github.com/llvm/llvm-project/pull/118022)

## Publish and install

Set the repository Actions secret `UFLINUX_APK_SIGNING_KEY` to the PEM-encoded ECDSA P-384 (secp384r1) private key used for UFlinux APK signing. The default-branch publishing job uses it to replace the temporary build signatures and verify every APK. The published release includes the public key, Zstandard-compressed APKv3 packages, SHA-256 checksums, resolved inputs, build manifests, and configured kernel sources.

Installation requires apk-tools 3 with Zstandard support. The release includes a statically linked `apk-zstd-<arch>` for Alpine installations whose system `apk` lacks Zstandard. After verifying its checksum against `SHA256SUMS`, make the binary executable and use it in place of `apk` for installing or upgrading these kernel APKs. The system package manager can stay installed. Verify the release public key against the UFlinux key you trust. Install that key in `/etc/apk/keys/uflinux-kernel.ec.pem.pub`, then install the APK with `apk add ./linux-uflinux-<target>-<version>-r<revision>.apk`. Alpine's mkinitfs trigger generates `/boot/initramfs-uflinux-<target>`. Point the bootloader at that initramfs and `/boot/vmlinuz-uflinux-<target>`. Generic and VM variants have distinct paths. Keep the current boot entry available while testing a new kernel. Install the GPU and board firmware required by your hardware from your Alpine or UFlinux firmware packages.

- **Orange Pi RV2.** Use the vendor U-Boot and its existing boot script. Update a test boot entry to load the raw `vmlinuz-uflinux-orangepi-rv2`, matching initramfs, and `dtbs-uflinux-orangepi-rv2/ky/x1_orangepi-rv2.dtb`. Preserve the board’s existing console, root-device, and boot arguments.
- **Dragon Q6A.** Use Radxa's UEFI firmware and a GRUB or extlinux test entry pointing to the matching kernel and initramfs. For device-tree boot, use `dtbs-uflinux-dragon-q6a/qcom/qcs6490-radxa-dragon-q6a.dtb`.
- **Raspberry Pi 3, 4, and 5.** Put the packaged `kernel8.img`, Broadcom DTBs, and `overlays/` on the firmware partition. In `config.txt` use `arm_64bit=1` and `kernel=kernel8.img` so Pi 5 selects it even when `kernel_2712.img` is present. Copy the generated initramfs onto that partition if it is separate and select it with `initramfs initramfs-uflinux-rpi followkernel`.

For physical validation, boot each board with its own DTB and confirm the running ABI with `uname -r`. Verify root-disk and network access, the default TCP congestion control with `sysctl net.ipv4.tcp_congestion_control`, and MGLRU's main enable bit in `/sys/kernel/mm/lru_gen/enabled`. On Pi, confirm `getconf PAGESIZE` returns `4096` on each model. Real board boot coverage requires hardware. The automated VM checks cover VirtIO storage, networking, and serial console.
