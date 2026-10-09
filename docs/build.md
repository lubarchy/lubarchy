# Build Environment

This document defines the LUBARCHY M0 builder environment.

**M0-D status: PASS.** The builder contract below was implemented and
qualified on 2026-10-08; see [Measured baseline](#measured-baseline-m0-d).

## Purpose

The builder is a dedicated, disposable Debian Stable build appliance. LUBARCHY
images will be built inside it from version-controlled configuration. It is
not a LUBARCHY product installation.

## Host roles

- **Orchestration host:** the Fedora workstation. It runs libvirt/QEMU/KVM,
  holds the Git working tree, and drives the builder over SSH. Fedora tooling
  on this host is managed with Fedora's own package manager; this does not
  affect the LUBARCHY APT/dpkg-only rule, which governs LUBARCHY/Debian OS
  content.
- **Build environment:** Debian Stable inside the builder VM. All Debian
  build tooling runs there, never on the orchestration host.

## Builder VM specification

| Item | Value |
| --- | --- |
| libvirt connection | `qemu:///system` |
| VM name | `lubarchy-builder` |
| Architecture | x86_64 / amd64 |
| Machine type | Q35 |
| Firmware | UEFI (OVMF) |
| Secure Boot | Disabled (builder only) |
| vCPU | 4 |
| RAM | 6144 MiB |
| Disk | 80 GiB sparse qcow2, VirtIO, `default` storage pool |
| Network | libvirt `default` NAT network, VirtIO NIC |
| Autostart | Disabled |
| Desktop | None |
| Console | Serial console |
| Guest agent | QEMU guest-agent channel enabled |

## Guest configuration

| Item | Value |
| --- | --- |
| OS | Debian 13 "Trixie", amd64 |
| Locale | `C.UTF-8` |
| Timezone | `UTC` |
| Hostname | `lubarchy-builder` |
| Account | `builder` (password locked, SSH key only) |
| Root | Locked; no root login |

## Installer source

- ISO: `debian-13.7.0-amd64-netinst.iso`
- SHA-512:
  `ef04d0276850d70e4aaec0b55003628660e9631b26e35215a70ab3c8c707294a4b12731bc73c75a932902f703a7c278310ca99df28d6b36fb06d5618d8d0f678`
- Authenticated through Debian's signed `SHA512SUMS`, Debian CD signing key
  `DF9B 9C49 EAA9 2984 3258 9D76 DA87 E80D 6294 BE9B`.
- The SHA-512 is re-verified before every use. On mismatch, provisioning
  stops.
- Installation is unattended through Debian Installer preseeding. Preseed and
  helper material are kept outside the repository and removed afterwards;
  they contain no passwords or private keys.
- Mechanism: `virt-install --location <verified ISO>` with `--initrd-inject`
  of the preseed and helper files into the installer initrd only. The ISO is
  never remastered.
- The official netinst image ships firmware and will otherwise install CPU
  microcode from `non-free-firmware` and enable that component. The preseed
  therefore sets `hw-detect/firmware-lookup string never` (documented in
  Debian's trixie example preseed) together with
  `apt-setup/non-free-firmware false` and
  `apt-setup/enable-source-repositories false`.

## Builder-only implementation details

The builder uses:

- GPT with an EFI System Partition;
- ext4 root filesystem, with installer-managed swap if created;
- Debian's standard GRUB UEFI installation;
- Secure Boot disabled;
- unencrypted virtual disk.

**These are builder-only choices. They do not resolve any product decision:**
bootloader (P-001), filesystem layout (P-002), encryption (P-003), and Secure
Boot (P-006) remain TBD by ADR. See [decisions.md](decisions.md).

## Network model

- The builder is attached to the libvirt `default` NAT network and obtains
  its address by DHCP.
- It is reachable from the orchestration host only through that NAT network.
- SSH is the only intended listening network service.

## Access model

- SSH public-key authentication only, using a dedicated Ed25519 key created
  for this Debian-generation builder. No earlier key is reused.
- Host keys are pinned in a dedicated known-hosts file on the orchestration
  host; host-key checking is never weakened globally.
- sshd policy (drop-in): `PermitRootLogin no`, `PasswordAuthentication no`,
  `KbdInteractiveAuthentication no`, `PubkeyAuthentication yes`,
  `AllowUsers builder`, `X11Forwarding no`, TCP forwarding disabled.
- `builder` has `NOPASSWD: ALL` sudo through one validated file under
  `/etc/sudoers.d/` (mode 0440). **This is an M0 builder automation exception,
  not a LUBARCHY product security default.**
- Private keys never enter the repository.

## APT sources

Only official Debian sources, component `main`:

- `trixie`
- `trixie-updates`
- `trixie-security`

Not enabled: testing, unstable/sid, experimental, backports, contrib,
non-free, non-free-firmware, third-party repositories, or LUBARCHY
repositories. Signature verification is never disabled.

## Package baseline

Intentionally installed:

- Debian "standard system utilities" task
- SSH server
- `sudo`
- `qemu-guest-agent`
- `git`
- `ca-certificates`

Intentionally **not** installed:

- `live-build` (not part of the M0-D baseline; installed in M0-E, see
  [live-build tooling and configuration](#live-build-tooling-and-configuration-m0-e))
- desktop environments, Xorg, Wayland compositors, display managers
- Flatpak, Podman, Docker, development toolchains, LUBARCHY packages

## Clean-rebuild principle

The builder is disposable. It must be reproducible from this document, the
verified Debian ISO, and official Debian repositories. Nothing that matters
may exist only inside the builder; build configuration lives in Git.

## Qualification evidence

The builder is PASS only with recorded evidence of:

- ISO SHA-512 match before installation;
- successful unattended installation from that ISO;
- UEFI boot with Secure Boot disabled;
- ext4 root and mounted EFI System Partition;
- locked root and `builder` passwords; key-only SSH; effective sshd policy;
- validated sudoers file;
- active `ssh` and `qemu-guest-agent`;
- only official Debian `main` sources for `trixie`, `trixie-updates`,
  `trixie-security`;
- successful `apt-get update` and `full-upgrade`;
- no desktop environment or display manager;
- `live-build` absent;
- no unexpected network listeners;
- persistent boot from disk after a controlled reboot;
- clean shutdown.

## Measured baseline (M0-D)

Established and qualified on 2026-10-08.

### Orchestration host

| Item | Value |
| --- | --- |
| OS | Fedora Linux 44 (Workstation Edition), kernel `7.2.9-200.fc44` |
| libvirt / QEMU / OVMF | `12.0.0-3.fc44` / `10.2.2-1.fc44` / `edk2-ovmf-20260812-8.fc44` |
| virt-install | `5.1.0-4.fc44` (official Fedora repository) |

### Virtual machine

| Item | Value |
| --- | --- |
| Name / connection | `lubarchy-builder` / `qemu:///system` |
| Machine | `pc-q35-10.2`, x86_64 |
| Firmware | `OVMF_CODE_4M.qcow2` (non-Secure-Boot build) |
| NVRAM | `/var/lib/libvirt/qemu/nvram/lubarchy-builder_VARS.qcow2` (no enrolled keys) |
| Secure Boot | Disabled (`secureboot: Secure boot disabled`) |
| vCPU / RAM | 4 / 6144 MiB |
| Disk | `/var/lib/libvirt/images/lubarchy-builder.qcow2`, qcow2, 80 GiB capacity, sparse (about 2 GiB allocated), VirtIO |
| Network | libvirt `default` NAT, VirtIO NIC, DHCP |
| Console / agent | pty serial console; `org.qemu.guest_agent.0` channel |
| Persistent boot | Virtual disk; installer media ejected |
| Autostart | Disabled |

### Guest

| Item | Value |
| --- | --- |
| OS | Debian GNU/Linux 13 (trixie), `debian_version` 13.7, amd64 |
| Kernel | `6.12.111+deb13-amd64` (`linux-image-amd64 6.12.111-1`) |
| Hostname / locale / timezone | `lubarchy-builder` / `C.UTF-8` / `UTC`, NTP synchronised |
| Partitions | GPT: 976 MiB ESP (vfat, `/boot/efi`), 74.9 GiB ext4 `/`, 4.1 GiB swap |
| Encryption / LVM / RAID | None |
| Boot loader (builder only) | `grub-efi-amd64 2.12-9+deb13u2`, `shim-signed 1.51~1+deb13u1+16.1-2~deb13u1` |
| Packages | 330 installed; 0 pending upgrades |

Package highlights: `base-files 13.8+deb13u7`, `openssh-server 1:10.0p1-7+deb13u4`,
`qemu-guest-agent 1:10.0.13+ds-0+deb13u1`, `sudo 1.9.16p2-3+deb13u2`,
`git 1:2.47.3-0+deb13u1`, `ca-certificates 20250419`.

### APT policy (measured)

```text
deb http://deb.debian.org/debian trixie main
deb http://security.debian.org/debian-security trixie-security main
deb http://deb.debian.org/debian trixie-updates main
```

No `deb-src`, no other components, no files in `sources.list.d`, no
non-free, contrib or microcode packages installed. `apt-get update` and
`full-upgrade` succeeded with signature verification intact.

### Access

- Client key: Ed25519, comment `lubarchy-m0-builder`, fingerprint
  `SHA256:997WQLfp0SFWOqBSTSn+qVxKgsON+YIY9xXRp+FIJuY`. The private key stays
  on the orchestration host only.
- Builder host key (pinned in a dedicated known-hosts file): Ed25519
  `SHA256:H5+5deKNLnz+qW9xJgZJyXIHH9qa0N9KrGhLH9tAPBM`.
- `root` and `builder` passwords locked; effective sshd policy matches the
  access model above; sudoers file validated; `sudo -n true` succeeds.

### Services and listeners

- `ssh` enabled and active; `qemu-guest-agent` active (device-activated
  static unit) and answering libvirt.
- Listeners: `sshd` on TCP 22 (IPv4 and IPv6), the only intended service.
  `dhcpcd` holds DHCP client sockets (UDP 68, and UDP 546 on link-local IPv6)
  as part of Debian's network configuration.
- No failed units; no display manager.

### Qualification summary

All items under [Qualification evidence](#qualification-evidence) passed,
including persistent boot from disk after a controlled post-upgrade reboot
and a clean shutdown. **`live-build` is not installed at the M0-D
baseline**; the `lb` command does not exist. No desktop task, display
manager, Flatpak, Podman, Docker or `build-essential` is installed. No
legacy artifact was reused.

### Known limitations

- Read-write access to `qemu:///system` from the orchestration host depends
  on interactive polkit administrator authorisation and can time out.
- libvirt changed the owner (`qemu:qemu`) and SELinux label
  (`virt_content_t`) of the installer ISO while it was attached. Its content
  and SHA-512 are unchanged.
- The installer ran in `en_US.UTF-8`; the installed system locale is set to
  `C.UTF-8` during late configuration.
- The guest IP address is assigned by DHCP and may change.
- No snapshot of the baseline exists yet.

## live-build tooling and configuration (M0-E)

### Tooling

| Item | Value |
| --- | --- |
| Package | `live-build 1:20250505+deb13u1` (`lb --version`: `20250505+deb13u1`) |
| Origin | `http://deb.debian.org/debian trixie/main` |
| Installed with | `apt-get install live-build`, normal Recommends policy |
| Transaction | 14 new packages, 0 upgraded, 0 removed, no authentication warnings |

New packages: `live-build`, `debootstrap`, `arch-test`, `distro-info`,
`cryptsetup`, `cryptsetup-bin`, `libcryptsetup12`, `libcurl4t64`,
`systemd-container`, `libnss-mymachines`, `live-boot-doc`, `live-config-doc`,
`live-manual-html` (all `trixie/main`) and `rsync` (`trixie-security/main`).
The builder's APT sources were unchanged and no non-free, contrib or foreign
package was installed. `live-boot` and `live-config` are not installed on the
builder; they are only needed inside the image.

### Source-controlled configuration

The image is described in Git under `build/live`, using live-build's
auto-script model:

| File | Role |
| --- | --- |
| `build/live/auto/config` | The single authoritative `lb config noauto` invocation |
| `build/live/auto/clean` | `lb clean noauto --purge`, then removes the generated `config/{binary,bootstrap,chroot,common,source}` and `build.log` |
| `build/live/auto/build` | `lb build noauto … \| tee build.log` under Bash `pipefail` |
| `build/live/auto/build-environment` | Sourced by `auto/config` and `auto/build`: exports a deterministic `SOURCE_DATE_EPOCH` (since M0-I) |
| `build/live/config/rootfs/excludes` | Paths left out of the live SquashFS: the volatile APT binary caches (since M0-I) |
| `tests/live-build-config.sh` | Configuration gate (see below) |

Auto scripts make every `lb config` run reproduce the same configuration from
Git, instead of relying on state left in a working directory. The
configuration uses `--ignore-system-defaults`, so `/etc/live/build.conf` and
`/etc/live/build/*` cannot influence it.

`lb config` also generates working files in `build/live` (the variable files
above, `.build/`, `local/bin`, empty `config/*` directories, default hook
symlinks under `config/hooks/` and `config/package-lists/live.list.chroot`).
These are regenerated on every run and are not committed.

### M0 configuration

| Setting | Value |
| --- | --- |
| Mode / distribution / architecture | `debian` / `trixie` / `amd64` |
| System / image type | `live` / `iso-hybrid` |
| Archive areas | `main` only |
| Security / updates / backports / proposed-updates | enabled / enabled / disabled / disabled |
| APT verification / source entries in image | enabled / disabled |
| Debian Installer | `none` |
| Firmware in binary / chroot | disabled / disabled |
| Source image | disabled |
| Boot loaders | `grub-pc grub-efi` since M0-H (M0-E/F: `grub-efi` only); UEFI via `grub-efi` is the boot path, `grub-pc` provides the hybrid system area |
| GRUB menu | `config/bootloaders/grub-pc/config.cfg`: installed template plus `timeout_style=menu`, `timeout=5` (since M0-H) |
| UEFI Secure Boot | `disable` |
| Memtest / zsync / interactive | none / disabled / disabled |
| Kernel flavour / UTC time | `amd64` / enabled |
| Mirrors | `https://deb.debian.org/debian/`, security `https://security.debian.org/debian-security/` (bootstrap, chroot, binary and parent mirrors) |
| Image identity | name `lubarchy-m0`, application `LUBARCHY M0`, publisher `LUBARCHY; https://lubarchy.com`, volume `LUBARCHY_M0` |

**GRUB EFI with Secure Boot disabled and the absence of an installer are M0
pipeline-qualification choices only.** They do not resolve P-001 (boot
architecture), P-004 (installer technology) or P-006 (Secure Boot); see
[decisions.md](decisions.md).

### Validation

Run inside the builder from a checkout:

```sh
tests/live-build-config.sh
```

The gate checks auto-script syntax, generates the configuration through
`auto/config`, runs `lb config noauto --validate`, and asserts values from
`lb config noauto --dump` (42 in M0-E, 44 since M0-I) plus the absence of
contrib/non-free areas. Since M0-H it also checks that the project GRUB
`config.cfg` equals the installed live-build template except for the menu
timeout lines. Since M0-I it also checks the `SOURCE_DATE_EPOCH` derivation
and the exact `config/rootfs/excludes` policy. It
refuses any `lb build`, needs no root, does not touch libvirt, and removes
only the files it generated, including on failure.

**No LUBARCHY ISO has been built in M0-E.**

## First M0 ISO build (M0-F)

The first image was built on 2026-10-08 from the exact committed
configuration, transferred to the builder as a verified Git bundle.

| Item | Value |
| --- | --- |
| Source commit | `a7d1e9c10df6fbe3d2a9a30e6bb3378dcabf1073` (tree `57c8cc51d47df0a001775a0765bde043a06ef754`) |
| Builder tooling | `live-build 1:20250505+deb13u1`, `debootstrap 1.0.141` |
| Entry point | `sudo ./auto/build` in `build/live`, after `tests/live-build-config.sh` passed |
| Build time (UTC) | 2026-10-08T11:19:14Z to 11:27:07Z (473 s), exit status 0 |
| Build log | No `E:` lines; no APT signature or authentication errors |
| ISO | `lubarchy-m0-amd64.hybrid.iso`, 330346496 bytes |
| SHA-256 | `5aad5602cf6276f1303d4a69199ac3c9fdf39297f603127d5f87d188bb289f60` |
| SHA-512 | `7d7d098a3808cd0251a4b3c0ac878fc3e758488b27edd42a58ae64600d99470be987d82dda2812ac7676d1da8618720e61ed52f6e15dcff8de351463510d43e1` |
| Packages | 180 (kernel `linux-image-6.12.111+deb13-amd64 6.12.111-1`) |

### Static inspection

The ISO was inspected without booting it:

- ISO 9660 with Rock Ridge and Joliet; volume `LUBARCHY_M0`, application
  `LUBARCHY M0`, publisher `LUBARCHY; https://lubarchy.com`.
- Live payload: `/live/filesystem.squashfs`, `/live/vmlinuz-6.12.111+deb13-amd64`,
  `/live/initrd.img-6.12.111+deb13-amd64`, `/live/filesystem.packages`.
- EFI material: El Torito boot catalog with an EFI (platform `0xEF`) entry,
  `/EFI/boot/bootx64.efi`, `/boot/grub/efi.img`, `/boot/grub/grub.cfg`,
  `/boot/grub/x86_64-efi/`.
- No Debian Installer payload.
- The image has no MBR boot signature and no GPT header: with `grub-efi` as
  the only boot loader, it carries El Torito EFI boot only. USB-stick boot is
  not established.
- The package manifest embedded in the ISO is identical to live-build's
  manifest.

### Package-source policy

The live system's APT sources are `trixie`, `trixie-security` and
`trixie-updates`, component `main`, from `https://deb.debian.org/debian/` and
`https://security.debian.org/debian-security/`, with `deb-src` entries
commented out. The build fetched only from those two hosts. No contrib,
non-free, non-free-firmware, firmware, microcode, desktop, display-manager or
installer package is present.

### Generated live-build defaults

`lb config` generated 26 default hook symlinks (all into the installed
live-build package) and `config/package-lists/live.list.chroot` (`live-boot`,
`live-config`, `live-config-systemd`, `systemd-sysv`), identical to M0-E.
They were used as transient build state only and were not committed.

### Retained artifacts

Kept outside Git and outside the libvirt storage pools, with `SHA256SUMS` and
`SHA512SUMS` verified on the builder and again after transfer to the
orchestration host:

- the ISO;
- `build.log`, the pre-build gate log and the `auto/config` output;
- `packages.txt` and live-build's raw manifests (`*.packages`, `*.contents`,
  `*.files`, `chroot.packages.*`);
- `iso-contents.txt` and `iso-meta.txt` (static inspection);
- `config-dump.txt` and `generated-defaults.txt`;
- `build-info.txt` (provenance).

The temporary build workspace on the builder was removed after verification.

### Not established in M0-F

- At the end of M0-F the ISO had not been booted; boot qualification
  followed in M0-G (below).
- **A reproducible clean rebuild has NOT been proven.**
- GRUB EFI with Secure Boot disabled remains an M0 pipeline choice, not the
  product boot architecture (P-001, P-006 open).

## Boot qualification of the first ISO (M0-G)

On 2026-10-08 the M0-F ISO (`lubarchy-m0-amd64.hybrid.iso`, SHA-256
`5aad5602cf6276f1303d4a69199ac3c9fdf39297f603127d5f87d188bb289f60`) was
re-verified against its retained SHA-256 and SHA-512 manifests and tested
without modification. Test methods are described in
[testing.md](testing.md#m0-boot-testing).

| Test | Result |
| --- | --- |
| UEFI optical boot (Q35, OVMF without Secure Boot, 2 vCPU, 2048 MiB, no disk, no network, read-only CD-ROM) | **PASS** |
| Automated serial live-payload smoke (`tests/iso-boot-smoke.sh`) | **PASS** in 8 s (limit 180 s) |

UEFI optical boot: OVMF booted the El Torito EFI entry, the ISO's GRUB EFI
menu appeared, the default "Live system (amd64)" entry started kernel
`6.12.111+deb13-amd64`, live-boot mounted the ISO (`/dev/sr0`) as the live
medium and the SquashFS root, and the console reached the autologin shell
about 15 seconds after the entry was selected, well within 240 seconds. systemd
reported `running`; the only block devices were the CD-ROM and the SquashFS
loop device.

Automated smoke: with the kernel and initrd extracted from the same ISO and
the ISO attached read-only, the serial console showed the Debian 13 systemd
banner, `multi-user.target` and a ttyS0 login prompt. Negative checks confirmed
the test fails on a wrong checksum and on a missing live filesystem.

Findings:

- The GRUB menu has **no timeout**: the default entry is highlighted but GRUB
  waits for a key press, so unattended UEFI boot stops at the menu. The test
  selected the default entry with a single Enter.
- The image has no serial console configuration (GRUB uses `gfxterm`; no
  `console=ttyS0`); the serial smoke test adds it on the command line only.

**USB/removable-media boot of the M0-F ISO: NOT QUALIFIED.** That ISO has no
MBR boot signature, no GPT header and an empty system area; with `grub-efi` as
the only boot loader it provides El Torito EFI boot for optical media only. It
must not be described as USB-bootable. Both findings were addressed by a new
artifact in M0-H (below); the M0-F ISO itself is unchanged.

## Hybrid removable-media and unattended UEFI qualification (M0-H)

### Why `grub-pc` is configured

The installed live-build (`1:20250505+deb13u1`) splits `--bootloaders` into a
BIOS role (`LB_BOOTLOADER_BIOS`) and an EFI role (`LB_BOOTLOADER_EFI`);
`grub-pc grub-efi` is a documented combination. Its `binary_iso` stage writes
the hybrid system area only when the image type is `iso-hybrid` **and** a BIOS
boot loader is set: for `grub-pc` it adds `--grub2-mbr boot_hybrid.img
-efi-boot-part --efi-boot-image`, which together with the `grub-efi` options
(`-e boot/grub/efi.img -isohybrid-gpt-basdat`) produces an MBR, a GPT and an
EFI system partition mapped to the EFI boot image. With `grub-efi` alone (M0-F)
no system area is written.

`grub-pc` is therefore present **only as live-build's hybrid system-area
mechanism for the M0 pipeline artifact.** UEFI via `grub-efi` remains the M0
boot path; BIOS boot is not an M0 requirement and was not tested. This does not
select the LUBARCHY product boot architecture: P-001 (boot architecture) and
P-006 (Secure Boot) remain open. No installed live-build code was changed and
the ISO was not post-processed. The GRUB PC build tools (`grub-pc-bin`) come
from Debian trixie `main` and are installed by live-build into its build chroot
only; they are not part of the live filesystem.

### GRUB menu timeout

`build/live/config/bootloaders/grub-pc/config.cfg` is the installed live-build
template plus two lines, so the default entry boots after 5 seconds unless a
key is pressed:

```text
set default=0
set timeout_style=menu
set timeout=5
```

Menu entries, kernel parameters, theme and console settings are unchanged; no
serial console was added to the image.

### Artifact

| Item | Value |
| --- | --- |
| Source commit | `fc4c03470fbca0376d52cd87dec95a51da3e97cf` (tree `38cf7547544f603735047e77558b674ec6efc7ae`) |
| Build (UTC) | 2026-10-08T21:15:37Z to 21:23:31Z, exit 0; no `E:` lines or APT authentication errors |
| ISO | `lubarchy-m0-amd64.hybrid.iso`, 335235072 bytes |
| SHA-256 | `0a643e2e440885d2f2c17c4b71e2d4574dfb3f12cd379e9bccbdcbbf7548bf58` |
| SHA-512 | `8caec01edf811a10238710fb5c6eae00643c3059f1a3013727c4ea3e3355361b5aa0a87031878a5cfe14e40e19019ddbb2c0973e859df056696d89cc1f553ed7` |
| Packages | 180; live filesystem manifest identical to M0-F |
| Package sources | `trixie`, `trixie-security`, `trixie-updates` `main` only; fetched only from `deb.debian.org` and `security.debian.org` |

Build evidence and qualification evidence are retained outside Git with
SHA-256 and SHA-512 manifests.

### Static structure

`tests/iso-hybrid-structure.sh`: **PASS**.

- El Torito: BIOS entry (`/boot/grub/grub_eltorito`) and UEFI entry
  (`/boot/grub/efi.img`).
- System area: `MBR protective-msdos-label grub2-mbr GPT`; MBR boot signature
  `0x55AA`; protective MBR partition (type `0xEE`).
- GPT: EFI System Partition (type `C12A7328-F81F-11D2-BA4B-00A0C93EC93B`)
  mapped exactly to `/boot/grub/efi.img`, plus two gap partitions.
- ISO 9660 remains readable; no installer payload.
- BIOS boot material (`grub_eltorito`, `/boot/grub/i386-pc/`) is present but
  was not tested.

### Boot qualification

| Test | Result |
| --- | --- |
| UEFI optical boot, unattended (Q35, OVMF without Secure Boot, 2 vCPU, 2048 MiB, no disk, no network, read-only CD-ROM) | **PASS**: GRUB counted down from 5 s with no input and the default entry reached the console shell about 14 s after power-on |
| UEFI removable-media boot (same VM class, the ISO bytes as a read-only USB mass-storage disk on `qemu-xhci`, no CD-ROM, no disk, no network) | **PASS**: same unattended countdown; console shell about 16 s after power-on; live medium `/dev/sda` (`TRAN usb`, `RM 1`, `RO 1`), no `sr0`; systemd `running` |
| Automated serial live-payload smoke (`tests/iso-boot-smoke.sh`) | **PASS** in 8 s |

Both VM tests used byte-verified temporary copies of the ISO; the retained
artifacts were never handed to libvirt.

- **UEFI optical boot: QUALIFIED**
- **UEFI removable-media boot (OVMF USB mass-storage): QUALIFIED**
- **Physical USB real-hardware boot: NOT YET QUALIFIED.** It belongs to later
  real-hardware qualification.

Still not established: a reproducible clean rebuild.

## Deterministic build and reproducibility baseline (M0-I)

### Build time

`build/live/auto/build-environment` is sourced by `auto/config` and
`auto/build`. It requires a Git checkout whose tracked files equal `HEAD` and
exports `SOURCE_DATE_EPOCH`, by default the commit timestamp of `HEAD`
(`git show -s --format=%ct HEAD`). A caller-supplied value must be a
non-negative integer. The wall-clock time is never used. live-build passes it
to every stage, including xorriso's `--modification-date`.

### Initial failure: APT binary caches

The first A/B rebuild of `47fac2aafd6b8e4d117335b571fdc6f1a42d2046`
(SOURCE_DATE_EPOCH `1791536313`) was **NO-GO**. The two ISOs differed
(`d583178a868eb221960231b6012561f4519e025039b6cbd84d829fa1dac32c50` and
`e1d162876a023d8e0a250a70701f75806ea9f7269f97194cbd149f41775e9174`) under
identical Debian repository inputs. The only differing file in the live
SquashFS was `/var/cache/apt/pkgcache.bin`, APT's binary package cache, which
embeds the time at which it was generated.

A chroot-hook remedy (deleting the caches in a hook) was investigated and
rejected without being deployed. The installed live-build runs `apt-get update`
when it removes its build-time archive configuration, after the chroot hooks
have run, so APT recreates the caches after any hook. A hook cannot guarantee
that the caches are absent from the image.

### Remedy: `config/rootfs/excludes`

`build/live/config/rootfs/excludes` lists exactly:

```text
var/cache/apt/pkgcache.bin
var/cache/apt/srcpkgcache.bin
```

In the installed live-build (`binary_rootfs`, with
`LB_BUILD_WITH_CHROOT=true`, the default) this file is copied into the build
chroot and passed to `mksquashfs` as `-wildcards -ef /excludes`. The paths are
relative to the SquashFS source directory and are excluded at image-creation
time, after every chroot stage. The caches still exist in the mutable build
chroot, but they are left out of the image. Both files are regenerated by APT
on first use in the live system. `/var/lib/apt/lists` and the rest of
`/var/cache/apt` are kept.

`tests/live-build-config.sh` asserts that the file contains exactly these two
lines, with no wildcards and no other paths, and that
`LB_CHROOT_FILESYSTEM=squashfs` and `LB_BUILD_WITH_CHROOT=true`. Committed as
`2a2eda4b1c209be7dd5eb668e46402a181f308f6`.

### Reproducibility result

Two independent builds of `2a2eda4b1c209be7dd5eb668e46402a181f308f6` (tree
`4be5b43109e02d737bb4e83aab8d67b3bd7686fa`, SOURCE_DATE_EPOCH `1791538744`,
xorriso `--modification-date=2026100909390400`) on 2026-10-09, each from a
fresh clone of a verified Git bundle. The first build's workspace was cleaned
(`auto/clean`) and deleted before the second build started.

| Item | Build A | Build B |
| --- | --- | --- |
| Build (UTC) | 09:39:34Z to 09:46:13Z, exit 0 | 09:48:49Z to 09:56:14Z, exit 0 |
| Build log | no `E:` lines, no APT authentication errors | no `E:` lines, no APT authentication errors |
| ISO size | 312694784 bytes | 312694784 bytes |
| SHA-256 | `d697b42871477c5ac5e5055df3472986a55ca9f6216b5110d1cc2b11a2c33db3` | identical |
| SHA-512 | `61c8c68b3a45233493e60bce0cd65b610688e6457e4607cd10ae6b4d91291ea0bb45e0ef1f88eb07ee292ce1e90d137eb275caec4eec25ebb16d4c1e3063bc53` | identical |
| Packages | 180 | identical manifest |

`cmp` reports the two ISOs byte-identical. Package manifests, ISO file
listings, SquashFS path listings, payload hashes, the xorriso command line and
`.disk/info` are also identical. The only differences are in build-side
metadata that is not part of the image: wall-clock build times, `ls -l` mtimes
of the build chroot and of the fresh checkouts, and evidence paths.

Repository inputs: the signed `InRelease` files of `trixie`, `trixie-updates`
and `trixie-security` were fetched and verified with `sqv` against
`debian-archive-keyring` before A, after A, before B and after B, and were
identical each time:

| Suite | `InRelease` SHA-256 | Date |
| --- | --- | --- |
| `trixie` | `0584fba32e13e0ab8285fb16c27adea1ec03a73669c18702821094fd6ca86675` | 2026-09-12 07:55:41 UTC |
| `trixie-updates` | `344c347c303cd2c621d6458e42f7acc789d478bb0b4fd79e4dcd2ee601b7d6ac` | 2026-10-09 08:09:09 UTC |
| `trixie-security` | `4e3d5b0b8cee991cb99a0a49d423cfc67c218aa0f8afa4589d6a1c88e1bf5bf7` | 2026-10-08 23:02:22 UTC |

The `InRelease` files used inside both build chroots match these hashes.

Exclusion proof: the path listing of the final `/live/filesystem.squashfs`
in each ISO contains neither `/var/cache/apt/pkgcache.bin` nor
`/var/cache/apt/srcpkgcache.bin`. It still contains `/var/cache/apt/`,
`/var/cache/apt/archives/`, `/var/lib/apt/lists/` (11 files) and
`/etc/apt/sources.list`. Compared with the failed M0-I image, the only path
change is the removal of those two files.

Functional gates on both ISOs: `tests/iso-hybrid-structure.sh` **PASS**;
`tests/iso-boot-smoke.sh` **PASS** (8 s). Static gates on the committed source
(`tests/run-static.sh`): repository lint PASS, ShellCheck 0 findings (9
scripts), live-build configuration gate PASS.

**Result: BIT-FOR-BIT REPRODUCIBLE UNDER IDENTICAL DEBIAN REPOSITORY INPUTS.**

Limitations:

- The Debian mirrors are moving external inputs. Rebuilding later, after the
  suites change, is expected to produce a different image. Historical
  reproducibility (from archived snapshots) is not established.
- Both builds ran on the same builder VM with the same toolchain.
- The pinned static CI workflow (`.github/workflows/m0-static.yml`) is defined
  but **has not yet been executed on GitHub**.
- Physical USB real-hardware boot remains **NOT YET QUALIFIED**.

Evidence (both ISOs, build logs, input snapshots, manifests, the comparison
report, the exclusion proof and the static-gate log) is retained outside Git
with verified `SHA256SUMS` and `SHA512SUMS`.
