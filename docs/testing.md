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
| Repository lint | `tests/repo-lint.sh` (M0-I) |
| shellcheck | Every tracked shell script, via `tests/run-static.sh` (M0-I) |
| Static gate runner | `tests/run-static.sh`: repository lint, ShellCheck, live-build configuration (M0-I); hardware contracts added in M1-A |
| GitHub CI | `.github/workflows/m0-static.yml` runs `tests/run-static.sh` on GitHub Actions (green since M0-J) |
| `live-build` configuration validation | `tests/live-build-config.sh` (M0-E) |
| ISO build | Manual, from the committed configuration (M0-F) |
| Checksum/manifest | Produced with the first build (M0-F) |
| UEFI VM boot test | UEFI optical boot test, operator-run, unattended since M0-H |
| Boot smoke test | `tests/iso-boot-smoke.sh` (M0-G) |
| Hybrid ISO structure | `tests/iso-hybrid-structure.sh` (M0-H) |
| UEFI removable-media boot | Virtual USB mass-storage boot test, operator-run (M0-H) |
| Physical USB boot | Not qualified (real-hardware qualification) |
| Clean rebuild comparison | Operator-run A/B rebuild with repository-input snapshots (M0-I): bit-for-bit reproducible under identical Debian repository inputs |

## M1 hardware contract gate

`tests/hardware-contracts.py` (gate 4 of `tests/run-static.sh`, since M1-A)
checks the hardware contracts in
[hardware-enablement.md](hardware-enablement.md) with the test-only reference
resolver `tests/hwe_reference.py`. It needs `python3` and
`python3-jsonschema` from Debian `main` (no pip, no PyPI), runs without root,
network, libvirt or hardware access, and is deterministic (seeded shuffles
only). It checks:

- every JSON file under `hardware/` and `tests/fixtures/hardware/` parses
  strictly (UTF-8, no duplicate keys, integers only);
- the three schemas validate against the Draft 2020-12 meta-schema, carry
  their fixed `$id` and identifier, and reject undeclared fields everywhere;
- the inventory schema declares no privacy-sensitive, policy or time field;
- valid fixtures pass; malformed inventories (non-normalized hex, missing or
  out-of-range fields, id/locator mismatch, duplicate ids, dangling parents,
  non-strict JSON) fail;
- 26 privacy-sensitive identifiers (serials, UUIDs, WWN, MAC/Bluetooth
  addresses, machine-id, hostname, username, IP addresses, SSID, secrets)
  and 8 generic property-dump containers are rejected at every object level
  of an inventory;
- selector semantics: AND within a selector, OR within a field's list, ANY
  selector per rule, absent fields never match, USB interface conditions on
  one interface, shell-style globs (anchored, case-sensitive, no regex),
  class-code prefixes, no selection on snapshot locators;
- rules with commands, scripts, unknown sources, regex fields or malformed
  selectors are rejected, as are duplicate rule ids and misnamed rule files;
  test rules are `test.*` with `lubarchy-test-*` packages and
  `hardware/rules/` holds no rule;
- unknown hardware is reported as unresolved and gets nothing;
- priority orders the result but never changes it; required/forbidden
  packages, source mismatches and profile exclusions fail closed at any
  priority;
- a non-`main` source is represented as `policy_review_required` and the
  reference code imports nothing that could touch the system;
- every produced plan validates; plans with commands, timestamps or
  inconsistent status are rejected; an invalid-inventory plan never echoes
  the rejected value;
- canonical JSON matches a known-answer SHA-256; inventory digests are stable
  under 25 seeded shuffles per fixture; plans for every fixture are
  byte-identical to the expected files and unchanged by reordering the
  inventory or the rule set.

| Fixture | Coverage |
| --- | --- |
| 01 | Intel CPU + Intel integrated GPU |
| 02 | AMD CPU + AMD GPU |
| 03 | NVIDIA discrete GPU |
| 04 | Intel + NVIDIA hybrid-GPU topology |
| 05 | Ethernet + Wi-Fi |
| 06 | USB Bluetooth controller |
| 07 | NVMe storage controller |
| 08 | Unknown PCI device (and unknown CPU vendor), no rule |
| 09 | QEMU/KVM-like guest |
| 10 | Invalid inventories: forbidden field, non-normalized hex |
| 11 | Conflicting rules |
| 12 | Determinism: fixture 06 with shuffled ordering |

Details are in
[`tests/fixtures/hardware/README.md`](../tests/fixtures/hardware/README.md).

## GitHub CI

The static gates run as the GitHub Actions workflow
`.github/workflows/m0-static.yml` (shown as `M0 static gates` through M0-J and
renamed `Static gates` in M1-A; the file name is unchanged): pinned
`actions/checkout` commit, pinned `debian:trixie` image digest,
`contents: read`, no secrets. It installs `git`, `ca-certificates`,
`shellcheck`, `live-build`, `python3` and `python3-jsonschema` (the last two
since M1-A) from Debian `main` only and runs `tests/run-static.sh`. **It is
static/configuration CI only: it never builds or boots an ISO and never
probes hardware.**

| Run | Commit | Event | Conclusion |
| --- | --- | --- | --- |
| [#1 (37933581297)](https://github.com/lubarchy/lubarchy/actions/runs/37933581297) | `f79c7324b534a37f6ec6c6566cb6615425ad6bce` | push | **failure**: Git rejected the root-run container workspace owned by the runner user ("dubious ownership"), so repository lint stopped |
| [#2 (37972612802)](https://github.com/lubarchy/lubarchy/actions/runs/37972612802) | `ce2462e4e3a6b1016e5fe5d23e8dfd4d7d1361ba` | push | **success**: repository lint, ShellCheck (9 scripts, 0 findings) and the live-build configuration gate passed |
| [#3 (38206501748)](https://github.com/lubarchy/lubarchy/actions/runs/38206501748) | `41263ec99655e78d114373fecbe14adddcdf1af1` | push | **success**: same three gates passed (M0 acceptance commit) |
| [#4 (38275430654)](https://github.com/lubarchy/lubarchy/actions/runs/38275430654) | `91c54072cffe8ca84855904cc85ad7ffd7abac73` | push | **success**: first run as `Static gates`; repository lint, ShellCheck (9 scripts, 0 findings), live-build configuration and hardware contracts (29 checks; `python3 3.13.5-1`, `python3-jsonschema 4.19.2-6` from trixie `main`) passed |

The fix in `ce2462e` trusts exactly `$GITHUB_WORKSPACE` for the gate step
through `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_0`/`GIT_CONFIG_VALUE_0`; no Git
configuration file is written. Later commits are checked by the same workflow
(push to `main` and pull requests touching the listed paths).

The M1-A workflow changes (the `Static gates` name, `python3` and
`python3-jsonschema`, the `hardware/**` path filter and gate 4) first ran on
GitHub in run #4.

## Clean rebuild comparison

Proves that the committed source produces the same ISO bytes when the Debian
repository inputs are the same:

1. Snapshot the signed `InRelease` files of `trixie`, `trixie-updates` and
   `trixie-security`, verified against `debian-archive-keyring`.
2. Build A from a fresh clone of the exact commit (`auto/config`, then
   `auto/build`); `SOURCE_DATE_EPOCH` comes from the commit time.
3. Snapshot the repository inputs again, run `auto/clean` and delete the
   workspace.
4. Snapshot again, then build B from a new fresh clone, then take a final
   snapshot.
5. All four snapshots must be identical; otherwise the result is classified
   as repository input drift, not as non-reproducibility.
6. Compare size, SHA-256, SHA-512, `cmp`, package manifests, ISO and SquashFS
   path listings, payload hashes and the xorriso command line; classify every
   remaining difference.

Both ISOs must also pass the hybrid structure check and the serial smoke test.
This does not prove historical reproducibility: the Debian mirrors move, and a
later rebuild is expected to differ. See [build.md](build.md) for the M0-I
result.

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
