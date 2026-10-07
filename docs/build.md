# Build Environment

This document defines the LUBARCHY M0 builder environment.

**M0-D status: IN PROGRESS.** The builder contract below is defined; the
builder has not yet been qualified.

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

- `live-build` (installed and qualified in a later, separate M0 step)
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

## Current state

M0-D — clean Debian builder: **IN PROGRESS.**
