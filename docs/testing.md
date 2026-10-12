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
| Static gate runner | `tests/run-static.sh`: repository lint, ShellCheck, live-build configuration (M0-I); hardware contracts added in M1-A; hardware probe added in M1-B; hardware resolver added in M1-C |
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

## M1 hardware probe gate

`tests/hardware-probe.py` (gate 5 of `tests/run-static.sh`, since M1-B) tests
the production probe `hardware/probe.py` against **synthetic** sysfs/procfs
trees built in a private temporary directory, with a fake command runner. It
never reads the host's `/sys` or `/proc`, never runs `dpkg`,
`systemd-detect-virt` or `udevadm`, and never inspects CI runner hardware.
It needs `python3` and `python3-jsonschema` from Debian `main` and no root.

It checks architecture mapping and fail-closed unsupported architectures;
missing tools; CPU parsing, omission of values that differ between logical
CPUs, and microcode normalization; UEFI/BIOS detection and every Secure Boot
variable case (enabled, disabled, absent, short, long, other value,
unreadable, and that no other variable is consulted); virtualization order;
PCI enumeration, ID normalization and malformed values; byte-for-byte
modalias; driver/module links; bridge, xHCI and USB-hub parent topology;
root-hub exclusion; active-configuration USB interfaces; locators beyond
inventory v1; udev MODALIAS agreement, absence and mismatch; that the command
checker rejects 20 non-approved command shapes (udev export, attribute walk,
full queries, other properties, trigger/test/control, shells, relative or
escaping paths); the read allowlists; that planted serial, product,
manufacturer, descriptor, config and resource values and real sysfs paths
never reach the output; the inventory-v1 contract and canonical bytes; that
equivalent trees built in different orders and locations give identical
output; the CLI contract; and that every value pattern in the probe equals
the inventory-v1 schema. A source (AST) inspection proves standard-library
imports only, exactly one `subprocess.run(argv, shell=False)` after the
command check, read-only `open(..., "rb")`, no environment access, no
file-write or process APIs other than the CLI's stdout/stderr, and no
forbidden identity paths or udev operations.

Mutation coverage (M1-B evidence): the gate fails for each injected defect —
collecting USB `serial`, including USB root hubs, uppercasing PCI IDs, using
a sysfs path as device identity, unsorted devices, accepting a udev/sysfs
MODALIAS mismatch, `udevadm --export-db`, `shell=True`, a timestamp field,
and a `hostname` field.

### Builder-VM live qualification

Operator-run on `lubarchy-builder`, never in CI:

1. Preflight: Debian 13 amd64, `main`-only APT sources, no failed units,
   and `python3`, `dpkg`, `udevadm`, `systemd-detect-virt` and
   `python3-jsonschema` present. Nothing is installed.
2. From a fresh clone of the exact commit, as the unprivileged `builder`
   user (`id -u` ≠ 0, no sudo), run `hardware/probe.py` at least twice.
3. Each run must exit 0 with empty stderr; the outputs must be
   byte-identical canonical JSON with the same SHA-256 and pass inventory-v1
   schema and semantic validation; no key may be privacy-sensitive.
4. Sanity for the VM: `amd64`/`x86_64`, at least one PCI device,
   virtualization `vm`, firmware `uefi`, Secure Boot not `enabled`.
5. A Python audit hook records every file the probe opens and every command
   it starts, proving read-only access, the allowlisted sources and that the
   only udev queries were device-specific `MODALIAS` queries (counts and
   agreement only; no udev database is captured).

This proves only that the probe produces a valid, deterministic inventory
for the builder VM. **It is not real-hardware qualification**: it says
nothing about physical Intel, AMD or NVIDIA hardware, laptops, Wi-Fi,
Bluetooth, physical USB devices, firmware availability or drivers.

## M1 hardware resolver gate

`tests/hardware-resolver.py` (gate 6 of `tests/run-static.sh`, since M1-C)
tests the production resolver (`hardware/resolver.py`, `hardware/resolve.py`)
and the production rules (`hardware/rules/`) with synthetic inputs only: the
M1-A fixtures, generated inventories, temporary rule directories and the
M1-B synthetic sysfs trees. It never reads host hardware. It checks:

- **parity:** every fixture inventory (valid and invalid) against the test,
  conflict and production rule sets, plus 60 seeded random rule subsets with
  random priorities, gives byte-identical canonical plans from the
  production resolver and the independent M1-A reference resolver; the
  inventory and rule-set digests agree;
- **inputs:** valid and invalid inventories (status `invalid`, no rejected
  values echoed); 10 malformed JSON inputs (truncated, trailing data,
  non-UTF-8, floats, NaN, Infinity, duplicate keys, deep nesting);
- **rule trust boundary:** 19 directory states each reject the whole set —
  file name ≠ id, symlinked rule, symlink to a non-rule, symlinked
  documentation, hidden rule, hidden file, subdirectory, directory named like
  a rule, other file type, upper-case extension, duplicate key, float,
  non-UTF-8, truncated JSON, schema-invalid rule, package/profile/warning
  effect, duplicate rule id;
- **policy:** exactly the 12 production rules, each one classification and
  nothing else; production plans carry no package, profile, warning or source
  and record the `main`-only source policy;
- **semantics:** AND within a selector, OR within a field, ANY selector,
  absent fields never match, priority orders only, all three conflict kinds
  fail closed, unknown hardware gets nothing;
- **classifications:** Intel/AMD CPU (other or mis-cased vendors not
  classified); display class `03` (VGA, XGA, 3D, other) from `8086`, `1002`,
  `10de`; Intel xHCI/audio, NVIDIA/AMD HDMI audio and QEMU, virtio, AMD-`1022`
  and Matrox display devices are not `gpu.*`; `0200` Ethernet; `0280` is
  `network.other` and never Wi-Fi; `010802` NVMe; `0106xx` SATA (but not
  `010803`, `0201`, `0104`); VM, container and explicit bare metal; no
  platform class without a virtualization observation; no `gpu.hybrid`;
- **determinism:** plan bytes, inventory digest and rule-set digest under
  seeded shuffles of devices and rules, and deterministic rule loading;
- **CLI:** canonical stdout plus newline, `-` for stdin, exit 0 (also for
  `conflict`), 2 (invalid plan), 3 (bad input), 4 (bad rules), 64 (usage, for
  example any `--rules` option); an end-to-end run of the executable;
- **source safety (AST):** only standard-library modules and `jsonschema`; no
  process, network, shell, eval, file-write or write-mode open; no APT, dpkg,
  modprobe, udevadm, sysfs, procfs, D-Bus or URL strings;
- **integration:** synthetic sysfs → `hardware/probe.py` library → canonical
  inventory → production plan, with the expected classifications, unresolved
  devices visible and no package, profile or source.

Mutation coverage (M1-C evidence): the gate fails for last-rule-wins,
AND→OR selector confusion, matching absent fields, loading symlinked rules,
skipping a malformed rule, allowing package effects in production, a package
effect added to a production rule, labelling `0280` as Wi-Fi, a changed
NVIDIA vendor ID, classifying non-display Intel functions as GPUs, unsorted
plan output, a `--rules` CLI option, shell execution and an APT invocation.

### Builder-VM probe → resolve qualification

Operator-run on `lubarchy-builder`, never in CI, from a fresh clone of the
exact commit, as the unprivileged `builder` user, with nothing installed:

1. Run `hardware/probe.py` and `hardware/resolve.py --inventory` on its
   output, twice, without changing the VM.
2. Require byte-identical inventories, inventory digests, plans and rule-set
   digests; valid inventory v1 and plan v1; no conflict; no package, profile
   or source. The plan may be `partial`.
3. Require at least `cpu.intel` (while the builder exposes an Intel CPU) and
   `platform.vm`, plus whatever else the observed facts support.
4. Compare package database, APT sources, loaded modules, driver bindings
   and failed units before and after.

This proves only that the resolver produces a valid, deterministic plan for
the builder VM; it is not real-hardware qualification.

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
| [#5 (38275836036)](https://github.com/lubarchy/lubarchy/actions/runs/38275836036) | `8cc2f6d816d3c3f4298a54489c09d7f61f69e11b` | push | **success**: all four gates passed (documentation commit) |
| [#6 (38276073529)](https://github.com/lubarchy/lubarchy/actions/runs/38276073529) | `1bb31567b4b54e551026a244de900ad35840a324` | push | **success**: all four gates passed (documentation commit) |
| [#7 (38276813287)](https://github.com/lubarchy/lubarchy/actions/runs/38276813287) | `cf1318dafa5afb9e60b697e8220c2bab18041755` | push | **success**: all four gates passed (documentation commit) |
| [#8 (38289415670)](https://github.com/lubarchy/lubarchy/actions/runs/38289415670) | `4afa53fd14e983552dea1f8553366ddd13a59e6a` | push | **success**: repository lint, ShellCheck (9 scripts, 0 findings), live-build configuration, hardware contracts (29 checks) and hardware probe (35 synthetic checks) passed |
| [#9 (38289634704)](https://github.com/lubarchy/lubarchy/actions/runs/38289634704) | `7561ac107fd26d77de0275a91be726cb358b2b28` | push | **success**: all five gates passed (documentation commit) |
| [#10 (38294397684)](https://github.com/lubarchy/lubarchy/actions/runs/38294397684) | `67a659d02a845886be43ef71e32193a53786180e` | push | **success**: repository lint, ShellCheck (9 scripts, 0 findings), live-build configuration, hardware contracts (29 checks), hardware probe (35 checks) and hardware resolver (23 checks; 96 production/reference parity cases) passed |

The fix in `ce2462e` trusts exactly `$GITHUB_WORKSPACE` for the gate step
through `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_0`/`GIT_CONFIG_VALUE_0`; no Git
configuration file is written. Later commits are checked by the same workflow
(push to `main` and pull requests touching the listed paths).

**M1-A CI qualification:** the M1-A workflow changes (the `Static gates`
name, `python3` and `python3-jsonschema`, the `hardware/**` path filter and
gate 4) were executed on GitHub by workflow `Static gates`, run ID
[38275430654](https://github.com/lubarchy/lubarchy/actions/runs/38275430654)
(run #4), on commit `91c54072cffe8ca84855904cc85ad7ffd7abac73`. All four
gates ran and passed: repository lint, ShellCheck, live-build configuration
and hardware contracts.

**M1-B CI qualification:** the M1-B change (gate 5, hardware probe,
synthetic only; no new dependency) was executed on GitHub by workflow
`Static gates`, run ID
[38289415670](https://github.com/lubarchy/lubarchy/actions/runs/38289415670)
(run #8), on commit `4afa53fd14e983552dea1f8553366ddd13a59e6a`. All five
gates ran and passed: repository lint, ShellCheck, live-build configuration,
hardware contracts and hardware probe. CI never probes the runner's hardware.

**M1-C CI qualification:** the M1-C change (gate 6, hardware resolver,
synthetic only; no new dependency) was executed on GitHub by workflow
`Static gates`, run ID
[38294397684](https://github.com/lubarchy/lubarchy/actions/runs/38294397684)
(run #10), on commit `67a659d02a845886be43ef71e32193a53786180e`. All six
gates ran and passed: repository lint, ShellCheck, live-build configuration,
hardware contracts, hardware probe and hardware resolver.

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
