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

Use a fresh output directory when repeating a build. Run `python3 scripts/resolve.py` to resolve the entire matrix. For source and patch validation without compilation, run `python3 scripts/build.py virt-x86_64 work/resolved.json work/patch-check --prepare-only`. A GitHub token in `GH_TOKEN` can raise the source resolver's API rate limit. A local build that ships signed modules exports `UFLINUX_MODULE_SIGNING_KEY` (base64-encoded combined key-and-certificate PEM) or `UFLINUX_MODULE_SIGNING_KEY_FILE`; without one, a flavor that requests signing warns and builds unsigned.

Place target patches under `patches/<target>/` using numbered `.patch` filenames. Required local and vendor patches apply in order before BBRv3, BORE, and le9uo. BBRv3 is required and is built in as the default TCP congestion control. MGLRU is enabled at boot where the final Kconfig supports it. BORE and le9uo are used when the kernel series has a patch that applies cleanly. Inspect `build-<target>.json` in the release for patch hashes, feature status, and omitted optimizations.

The generic kernels keep Alpine's CPU baselines. Optimizations use Clang, ThinLTO in distributed mode where supported, and `-O3` when the kernel compiles with it. On generic `aarch64`, GUD uses Kbuild’s optimization level because its module link failed Clang’s fortified-read check at `-O3`. The BOLT integration uses the exact linked binary and runs before the boot image is generated.

### UFlinux profiles

Scheduled releases generate PGO profiles and apply BOLT wherever the release workflow has a compatible rewriter. Scheduled runs skip inputs that already have a published release. To run the same pipeline manually, dispatch the workflow on the default branch with `profile_target=none` and `generate_release_profiles=true`. A manual run that skips profile generation publishes anyway when `force_publish=true`: it ships the default-optimization kernels built without PGO or BOLT, under the same tag as an otherwise identical profile-less build. Force publish needs a full release build, so it cannot be combined with `profile_target`. The workflow independently profiles `x86_64`, `virt-x86_64`, `aarch64`, and `virt-aarch64` using mixed block I/O and loopback TCP in QEMU. Clang instrumentation covers selected built-in block, memory, filesystem, and networking code. The optimized kernel consumes the resulting instrumentation profile without including the counter exporter.

To collect a profile locally for a generic or virtual target, use matching Alpine build dependencies in both builds:

```sh
target=virt-x86_64
output=work/collection
python3 scripts/resolve.py --target "$target"
UFLINUX_PROFILE_GUEST=1 scripts/container.sh "$target" work/resolved.json "$output" --profile-build
scripts/collect-release-profiles.sh work/resolved.json "$output/profiles" "$target" pgo "$output"
UFLINUX_KERNEL_PHASE=merge-pgo scripts/container.sh "$target" work/resolved.json "$output"
mkdir -p "work/$target/profiles"
cp "$output/profiles/"* "work/$target/profiles/"
scripts/container.sh "$target" work/resolved.json "work/$target"
```

The profile pair is `kernel.profdata` and `pgo.json`. Use the pair together. Its metadata binds it to the target, source, patches, configuration, compiler and optimization flags. Single-target workflow dispatches using `profile_target` produce an instrumented APK and `vmlinux` as workflow artifacts without publishing a release.

For BOLT, use the exact linked ELF from the PGO-optimized build. CI separates the collection compile, PGO training, release compile, BOLT rewrite, and APK packaging and boot tests. The x86 jobs share a single patched rewriter. The rewrite job verifies the input ELF and rewriter, preserves x86 KASLR relocations, and regenerates the boot image before packaging. The release manifests report PGO and BOLT separately.

To build the x86 rewriter locally, resolve with `--generate-profiles`, install the x86_64 LLVM versions listed in `work/resolved.json`, and run `scripts/build-kernel-bolt.sh <tools> work/resolved.json`. Bundle that binary using `UFLINUX_BOLT_BINARY` with `scripts/prepare-bolt.sh <target> <output> --bundle`. For a local training run, place the PGO profile pair in a fresh output directory under `profiles/`, then run `container.sh` with `--bolt-baseline` and `UFLINUX_PROFILE_GUEST=1`. Run `collect-release-profiles.sh <resolved> <output>/profiles <target> bolt <output> <output>/bolt/llvm-bolt`, then run `container.sh` with `--apply-collected-bolt` against that same output. Boot-test the rewritten package with `scripts/boot-test.sh`.

Profiled ARM64 releases use PGO. Set `KERNEL_AARCH64_BOLT_RUNNER` to an Ubuntu 26.04 ARM64 runner with a compatible `llvm-bolt` on PATH to add BOLT. Its rewriter must support pre-aggregated traces and in-place kernel optimization. [LLVM AArch64 kernel BOLT proposal](https://github.com/llvm/llvm-project/pull/118022)

## Memory footprint policy

The measured baseline for `virt-x86_64` was about 25.7 MiB resident at `Freeing unused kernel image`. The policy below is per-flavor data in `targets.json`, applied by `scripts/build.py` and re-checked after Kconfig normalization, so a flavor can opt back in without a code change.

- `KALLSYMS_ALL` and `LATENCYTOP` are off. `LATENCYTOP` selects both `KALLSYMS_ALL` and `SCHEDSTATS`, so all three are cleared in one pass. `KALLSYMS_ALL` alone is 1.4 MiB of `.rodata` on the guest flavor. `KALLSYMS` stays on, so `perf`, `ftrace`, and lockdep still resolve symbols.
- `LOG_BUF_SHIFT` is 16 and `DYNAMIC_DEBUG` is off, which removes 288 KiB of static log buffers and 100 KiB of `_ddebug` tables.
- `NR_CPUS` and `HZ` are per-flavor: 512 for the ARM64 flavors, 256 for the x86_64 flavors, 8 for Orange Pi, 4 for Raspberry Pi; 1000 Hz on generic flavors, 300 Hz in the guests, and the vendor value on the boards. Per-CPU allocations and the scheduler tick both scale with these.
- `INIT_ON_ALLOC_DEFAULT_ON` and `IKCONFIG` are per-flavor. `SCHEDSTATS` is opt-in.
- `MODULE_UNLOAD` stays on everywhere: it is the only way a rarely used driver returns its memory at runtime.
- `TRIM_UNUSED_KSYMS` is deliberately never enabled. It shrinks the export table to what in-tree modules use, which breaks out-of-tree DKMS builds against the full table.
- No compression codec is removed. The audit found only about 37 KiB of resident decompressor code outside Zstd and LZ4, so every codec that a flavor's defconfig selects still ships.

The expected result across the matrix is roughly 2.0-2.3 MiB (8-9 %) less resident memory, plus a smaller on-disk boot image and module tree from Zstd.

## Module signing, compression, and packaging

Set the repository Actions secret `UFLINUX_MODULE_SIGNING_KEY` to the base64-encoded module-signing key in the combined format Kbuild expects: a PKCS#8 private key followed by its self-signed X.509 certificate in one PEM file, the same layout as the `certs/signing_key.pem` the kernel generates for itself. Kbuild reads the certificate out of that file to embed in the kernel and the private key out of it to sign modules, so a key-only PEM is rejected. Every flavor signs with an EC NIST P-384 key and sha384 (`MODULE_SIG_KEY_TYPE_ECDSA`, `MODULE_SIG_SHA384`), which cuts about 380 bytes of signature trailer out of every compressed module compared to RSA-4096/sha256 and makes load-time verification cheaper. sha384 is the only hash every supported source allows next to an EC key: from 6.50 on, `MODULE_SIG_KEY_TYPE_ECDSA` depends on `!(MODULE_SIG_SHA256 || MODULE_SIG_SHA3_256)` and Kconfig would silently build an RSA key instead. Generate one with `openssl req -new -x509 -newkey ec -pkeyopt ec_paramgen_curve:secp384r1 -noenc -sha384 -days 36500 -keyout key.pem -out cert.pem -subj "/CN=UFlinux kernel module signing key" && cat key.pem cert.pem | base64 -w0 && rm key.pem cert.pem`, then set it as the secret. The commonName becomes the `signer` field in every module, so keep it stable across regenerations. A flavor can go back to RSA with `"module_sig_key_type": "RSA"` and `"module_sig_hash": "sha256"` in `targets.json`; the key must then be an RSA one, so keys and modules are always regenerated together. Every flavor builds with `MODULE_SIG_ALL` and that key, so modules carry a real signer instead of tainting the kernel with `TAINT_UNSIGNED_MODULE`. `MODULE_SIG_FORCE` stays off because locally built DKMS modules are unsigned. `scripts/build.py` publishes the matching certificate once per release, as `uflinux-module-cert.x509`, and stages a per-flavor copy that each kernel package ships at `/boot/module-cert-uflinux-<flavor>.x509`, so two flavors installed side by side do not fight over one path; it deletes the private key from the object tree so it never reaches an artifact. A build without the secret warns and ships unsigned modules, which is fine for a fork pull request; the job that publishes a release refuses to run without it.

Modules install Zstd-compressed (`.ko.zst`) and the ARM64 EFI flavors compress the boot image with Zstd. Alpine's `kmod` and `mkinitfs` are built against `zstd-dev`, and the in-kernel Zstd decompressor is already resident, so neither the initramfs generator nor early userspace needs anything extra.

Each flavor now publishes two APKs: `linux-uflinux-<target>` (the boot files: kernel image, initramfs inputs, `System.map`, and build config) and `linux-uflinux-<target>-modules` (the whole `/lib/modules/<abi>` tree, including `modules.dep` and the built-in module lists). The kernel package depends on the exactly matching modules package, so `apk add ./linux-uflinux-<target>-<version>-r<revision>.apk` still installs both and the mkinitfs trigger still sees a complete module tree. A driverless guest can keep the kernel package and drop the modules package later if the package manager allows it.

## Publish and install

Set the repository Actions secret `UFLINUX_APK_SIGNING_KEY` to the PEM-encoded ECDSA P-384 (secp384r1) private key used for UFlinux APK signing. The default-branch publishing job uses it to replace the temporary build signatures and verify every APK. The published release includes the public key, Zstandard-compressed APKv3 packages, SHA-256 checksums, resolved inputs, build manifests, and configured kernel sources.

Installation requires apk-tools 3 with Zstandard support. The release includes a statically linked `apk-zstd-<arch>` for Alpine installations whose system `apk` lacks Zstandard. After verifying its checksum against `SHA256SUMS`, make the binary executable and use it in place of `apk` for installing or upgrading these kernel APKs. The system package manager can stay installed. Verify the release public key against the UFlinux key you trust. Install that key in `/etc/apk/keys/uflinux-kernel.ec.pem.pub`, then install the APK with `apk add ./linux-uflinux-<target>-<version>-r<revision>.apk`. Alpine's mkinitfs trigger generates `/boot/initramfs-uflinux-<target>`. Point the bootloader at that initramfs and `/boot/vmlinuz-uflinux-<target>`. Generic and VM variants have distinct paths. Each APK also installs its kernel’s symbol map at `/boot/System.map-uflinux-<target>`. Keep the current boot entry available while testing a new kernel. Install the GPU and board firmware required by your hardware from your Alpine or UFlinux firmware packages.

- **Orange Pi RV2.** Use the vendor U-Boot and its existing boot script. Update a test boot entry to load the raw `vmlinuz-uflinux-orangepi-rv2`, matching initramfs, and `dtbs-uflinux-orangepi-rv2/ky/x1_orangepi-rv2.dtb`. Preserve the board’s existing console, root-device, and boot arguments.
- **Dragon Q6A.** Use Radxa's UEFI firmware and a GRUB or extlinux test entry pointing to the matching kernel and initramfs. For device-tree boot, use `dtbs-uflinux-dragon-q6a/qcom/qcs6490-radxa-dragon-q6a.dtb`.
- **Raspberry Pi 3, 4, and 5.** Put the packaged `kernel8.img`, Broadcom DTBs, and `overlays/` on the firmware partition. In `config.txt` use `arm_64bit=1` and `kernel=kernel8.img` so Pi 5 selects it even when `kernel_2712.img` is present. Copy the generated initramfs onto that partition if it is separate and select it with `initramfs initramfs-uflinux-rpi followkernel`.

For physical validation, boot each board with its own DTB and confirm the running ABI with `uname -r`. Verify root-disk and network access, the default TCP congestion control with `sysctl net.ipv4.tcp_congestion_control`, and MGLRU's main enable bit in `/sys/kernel/mm/lru_gen/enabled`. On Pi, confirm `getconf PAGESIZE` returns `4096` on each model. Real board boot coverage requires hardware. The automated VM checks cover VirtIO storage, networking, and serial console.
