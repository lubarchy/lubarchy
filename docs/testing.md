# Testing

Testing and qualification baseline for LUBARCHY.

## Principles

- "Works on my VM" is not qualification.
- **Expected to work is not PASS.**
- A result is PASS only with recorded evidence: exact commands, exit statuses,
  relevant output, and the tested configuration.

## Test layers

| Layer | Purpose |
| --- | --- |
| Static validation | Lint, shellcheck, configuration validation |
| Unit testing | Individual tools and scripts |
| Integration testing | Components working together |
| ISO verification | Build outputs, manifests, checksums, signatures |
| Installation testing | Installer flows and storage safety |
| Desktop testing | Hyprland and Plasma sessions |
| Real-hardware testing | Qualification on physical machines |
| Update testing | Update paths between versions |
| Recovery testing | Snapshots, rollback, recovery environment |
| Security testing | Authenticity, exposure, privilege |

## Installer safety matrix

Installer tests must cover each disk mode, existing operating systems,
existing EFI data, multiple disks, insufficient space, and interruption. See
[installer.md](installer.md).

## Security tests

- Signature/authenticity failure handling fails closed.
- No unnecessary listening services by default.
- No secrets or private keys in repository or build outputs.

## Update tests

- Updates from authenticated sources only.
- Upgrade paths between qualified versions.
- Failure during update leaves a recoverable system.

## Recovery tests

Rollback, bootable recovery, and desktop-independent recovery tested with
injected failures. See [recovery.md](recovery.md).

## Reproducibility evidence

Build evidence must include:

- the Git commit built;
- builder environment and tool versions;
- package manifest;
- output checksums;
- build logs.

A clean rebuild must succeed from the same commit.

## M0 gates

| Gate | Status |
| --- | --- |
| Repository lint | Not implemented |
| shellcheck | Not implemented |
| `live-build` configuration validation | `tests/live-build-config.sh` (M0-E) |
| ISO build | Manual, from the committed configuration (M0-F) |
| Checksum/manifest | Produced with the first build (M0-F) |
| UEFI VM boot test | UEFI optical boot test, operator-run, unattended since M0-H |
| Boot smoke test | `tests/iso-boot-smoke.sh` (M0-G) |
| Hybrid ISO structure | `tests/iso-hybrid-structure.sh` (M0-H) |
| UEFI removable-media boot | Virtual USB mass-storage boot test, operator-run (M0-H) |
| Physical USB boot | Not qualified (real-hardware qualification) |
| Clean rebuild comparison | Not implemented |

No gate runs in CI yet.

## M0 boot testing

M0 boot qualification uses four separate tests. Each proves something the
others do not, and none replaces another.

### UEFI optical boot test

Proves the ISO's own boot chain:

OVMF → UEFI optical (El Torito EFI) boot → GRUB EFI → kernel/initrd →
live-boot → live filesystem → userspace

It runs in a fresh, disposable libvirt VM: Q35, OVMF without Secure Boot and
with a fresh NVRAM, 2 vCPU, 2048 MiB RAM, no disk, no network, and the ISO
attached as a read-only CD-ROM (a byte-verified temporary copy, so the
retained artifact is never handed to libvirt). The default Live entry must
reach a console shell without any keyboard input: since M0-H the GRUB menu
boots the default entry after 5 seconds, and the test requires the console
within 120 seconds with zero input (M0-G, before the timeout existed, allowed
one Enter and 240 seconds). Evidence is captured as screenshots and an
in-guest check of the live medium mount, kernel command line, systemd state
and block devices.

It does not prove removable-media boot, BIOS boot or boot on real hardware.

### Hybrid ISO structure check

```sh
tests/iso-hybrid-structure.sh --iso ISO --sha256 SHA256 [--report FILE]
```

Read-only, no root. After verifying the SHA-256 it uses `xorriso` to check
that the ISO is readable as ISO 9660, keeps a UEFI El Torito boot image, and
has an MBR system area with the `0x55AA` signature, a GPT (header at LBA 1)
and an MBR or GPT partition that maps the EFI boot image. It fails on an
empty system area (as in the M0-F ISO).

It proves structure only, not that any firmware or USB device boots the image.

### UEFI removable-media (virtual USB) boot test

Proves:

OVMF → USB mass-storage → EFI removable loader → GRUB (5-second default) →
kernel/initrd → live-boot → live filesystem → userspace

Same VM class as the optical test, but the ISO bytes are exposed only as a
read-only, removable USB mass-storage disk on a `qemu-xhci` controller, with
no CD-ROM, no other disk and no network. The image is a byte-verified
temporary copy and is never partitioned or modified. The test boots with zero
input and checks in the guest that the live medium is the USB disk (`lsblk`
transport `usb`, removable, read-only) and that no optical device exists.

It proves UEFI removable-media boot under OVMF only. It does not prove boot
from a physical USB device on real hardware, which belongs to real-hardware
qualification.

In both VM tests the VM, its NVRAM and the temporary ISO copy are removed
afterwards.

### Automated serial live-payload smoke test

Proves the ISO's live payload automatically:

kernel/initrd from the exact ISO → live-boot → the same ISO's SquashFS →
userspace → serial login prompt

```sh
tests/iso-boot-smoke.sh --iso ISO --sha256 SHA256 \
    --kernel VMLINUZ --initrd INITRD --grub-cfg GRUB_CFG [--log FILE] [--timeout SECONDS]
```

The kernel, initrd and `grub.cfg` are extracted read-only from the same ISO
(for example with `osirrox -indev ISO -extract ...`). The script:

- verifies the ISO's SHA-256 and requires it to be a read-only regular file;
- boots under QEMU/KVM with 2 vCPU, 2048 MiB RAM, no disk and no network,
  with the ISO attached read-only as a CD-ROM;
- uses the default Live entry's kernel parameters from `grub.cfg` and appends
  only `console=ttyS0,115200n8 live-config.noautologin systemd.show_status=yes`;
- passes when the serial console shows the systemd banner of the live root,
  `multi-user.target` and a ttyS0 login prompt within the timeout (default
  180 seconds);
- fails on kernel panic, a missing live medium, an `(initramfs)` shell,
  SquashFS errors or emergency mode, or on timeout.

**Direct kernel loading is used only to make serial output deterministic. It
is not evidence for UEFI or GRUB;** that proof comes from the UEFI optical
boot test above.

## Failure evidence

Failures are recorded with the same detail as successes: commands, exit
statuses, logs, and configuration. A failed security check is never
downgraded to a warning.

## Release qualification

A release is qualified only when every required gate passes with recorded
evidence tied to the release commit and build manifest.
