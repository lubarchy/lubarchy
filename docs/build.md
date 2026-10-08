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
