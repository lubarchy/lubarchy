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
| Static gate runner | `tests/run-static.sh`: repository lint, ShellCheck, live-build configuration (M0-I); hardware contracts added in M1-A; hardware probe added in M1-B; hardware resolver added in M1-C; hardware firmware added in M1-E; hardware NVIDIA strategy added in M1-G; hardware package transaction added in M1-I; hardware package staging (M1-J), package apply (M1-K), package recovery (M1-L), post-install validation (M1-M), package state garbage collection (M1-N) and hardware enablement orchestration (M1-O) added since |
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
- **policy:** exactly the 12 classification rules (one classification each)
  and, since M1-D, the 2 CPU microcode package rules; production effects are
  only classifications and `require` packages from `non-free-firmware`; the
  production package set is exactly `{intel-microcode, amd64-microcode}`;
  plans carry no profile or warning and record the source policy
  `main` + `non-free-firmware` (M1-C: classification only, `main` only);
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

### Package and source-policy tests (M1-D)

Part of gate 6 (`tests/hardware-resolver.py`); a separate gate would add no
isolation, because the policy is enforced by the same resolver and rule
loader. The tests require:

- the resolver source policy to be exactly `main`, `non-free-firmware`, and
  `contrib`, `non-free`, `backports`, `lubarchy` not to be authorised;
- `GenuineIntel` → `intel-microcode` and `AuthenticAMD` → `amd64-microcode`,
  each `require` from `non-free-firmware` with source status `authorised`,
  while the `cpu.intel` / `cpu.amd` classifications stay unchanged;
- no microcode for Hygon, Centaur, mis-cased, truncated or unknown vendor
  strings, and no package for any device;
- the exact production package set, no package from `main`, no `forbid`,
  profile or warning; a production rule requesting `contrib`, `non-free`,
  `backports` or `lubarchy`, or a `forbid` effect, rejects the rule set;
- a non-production rule requiring `contrib`, `non-free` or `backports` is
  still reported `policy_review_required` (representation is not
  authorisation);
- parity with the M1-A reference resolver under the same, explicitly
  injected source policy (the reference still accepts any policy);
- CLI exit 5 (internal contract failure) for four non-plan-v1 results,
  reached by substituting the resolver function inside the test process
  only (closes the M1-C test gap; production code has no test hook).

M1-D mutation coverage: the gate fails for Intel mapped to `amd64-microcode`,
AMD mapped to `intel-microcode`, a mapping source of `main`, `non-free` or
`backports`, a third package, Hygon treated as AMD, a package for unknown
CPUs, a `forbid` effect, a profile, an install subprocess, and a source
policy widened to `contrib`, `non-free` or `backports`.

### Authenticated Debian metadata qualification (M1-D)

Operator-run, never in CI, as an unprivileged user with an isolated APT
configuration (its own state, cache and deb822 sources under a temporary
directory; the system's `/etc/apt` is not read for sources and not changed):

1. Sources: `https://deb.debian.org/debian` `trixie trixie-updates` and
   `https://security.debian.org/debian-security` `trixie-security`,
   components `main non-free-firmware`,
   `Signed-By: /usr/share/keyrings/debian-archive-keyring.pgp`;
   `Acquire::AllowInsecureRepositories` and
   `AllowDowngradeToInsecureRepositories` false; no `trusted=yes`.
2. `apt-get update` must succeed with no `W:`, `E:` or `Err:` line.
3. Each `InRelease` is verified again with `sqv --cleartext` against the
   archive keyring, its authenticated text compared with the file, and a
   tampered copy must be rejected.
4. `apt-cache policy` / `show` record component, section, architecture,
   description and version of every mapped and candidate package. Nothing is
   downloaded or installed.

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
   digests; valid inventory v1 and plan v1; no conflict; no profile. The
   plan may be `partial`.
3. Require at least `cpu.intel` (while the builder exposes an Intel CPU) and
   `platform.vm`, plus whatever else the observed facts support. Since M1-D
   the only package expected is `intel-microcode` from `non-free-firmware`
   with source status `authorised`, and no `amd64-microcode` (M1-C: no
   package at all).
4. Compare package database, APT sources, loaded modules, driver bindings
   and failed units before and after; since M1-D also require that
   `intel-microcode` is not installed and that the builder's own APT sources
   are still `main` only (the plan is a proposal, not an installation).

This proves only that the resolver produces a valid, deterministic plan for
the builder VM; it is not real-hardware qualification.

## M1 hardware firmware gate

`tests/hardware-firmware.py` (gate 7 of `tests/run-static.sh`, since M1-E) is
separate from the resolver gate because firmware-evidence integrity,
regeneration drift and overlap analysis fail independently of resolver
semantics. It uses only the committed, normalized kernel alias evidence
(`tools/hwe/module-alias-evidence.json`) and synthetic devices; it never
reads the CI runner's `/lib/modules`. It checks:

- the evidence is normalized (sorted, unique, PCI/USB only, uppercase hex as
  the kernel writes it), self-consistent (counts and SHA-256 digests) and
  tied to `linux-image-6.12.111+deb13-amd64` 6.12.111-1, with the exact
  per-module alias counts; SDIO, BCMA (`brcmsmac`) and non-bus aliases are
  recorded as excluded;
- the four committed `package.firmware.*` rules regenerate byte-for-byte
  from that evidence and `tools/hwe/firmware-mappings.json`; ten kinds of
  drift are detected (dropped alias, alias from another module, stale
  count/digest, changed case, duplicate, SDIO alias, alias for excluded
  `brcmsmac`, vendor-wide wildcard, hand-edited rule, extra rule file);
- the `modules.alias` parser rejects malformed lines, and SDIO, BCMA,
  non-bus and bracket aliases are unrepresentable;
- no cross-package overlap (field-wise analysis);
- exact firmware rules (modalias selectors only), the exact production
  package set of six, all `require` from `non-free-firmware`, no version, no
  deferred package (Realtek, Atheros, Bluetooth, NVIDIA, …);
- every one of the 1,722 committed aliases, instantiated as a concrete
  synthetic device, resolves to exactly its own family's package;
- synthetic fixtures `tests/fixtures/hardware/firmware/` (`iwlwifi`, `i915`,
  `xe`, `amdgpu`, `radeon`, `brcmfmac` PCI and USB) map as expected, while
  near misses (Intel network/display/audio devices without a qualified
  alias, AMD HDMI audio, a Broadcom BCMA chip, Broadcom Ethernet, Realtek and
  Atheros Wi-Fi, NVIDIA display) get no package;
- `network.wifi` only for qualified Wi-Fi aliases, `0280` alone stays
  `network.other`, firmware rules never create `gpu.*`;
- unknown hardware gets nothing; CPU microcode is unchanged;
- production/reference parity and plan determinism on the firmware fixtures;
- the resolver and probe never read kernel alias metadata (no runtime rule
  generation) and the qualification tool starts no process.

M1-E mutation coverage (gates 6 and 7): broadening any family to a whole
vendor, an alias from the wrong module, a dropped alias, an SDIO alias,
normalized case, a duplicate alias, a Realtek, Atheros or NVIDIA firmware
rule, a seventh package, a firmware source of `main` or `non-free`, a version
pin, runtime rule generation from `modules.alias`, and an install subprocess.

### M1-F strengthening of the firmware gate

Since M1-F gate 7 has 13 checks and also verifies:

- the evidence is `lubarchy.hardware.firmware-evidence/v2`, its M1-E alias
  entries equal the committed M1-E v1 evidence, and the four M1-E rules
  regenerate byte-for-byte from that v1 evidence (migration safety);
- **firmware-file attribution**: for every M1-F module, every declared
  firmware file of the binding module and its dependency closure is shipped
  by the family package or by no package, and at least one by the family
  package; M1-E families may only have explicitly reviewed foreign requests;
- **collisions**: no relevant firmware path is shipped by two packages, no
  alias overlap between rules whose package or classification differs, and
  every device-specific claim by another module is same-package, reviewed
  or excluded;
- the production package set is the six of M1-D/M1-E plus only the M1-F
  allowlist (`firmware-realtek`, `firmware-atheros`, `firmware-mediatek`,
  `bluez-firmware`), with no deferred module and no vendor-wide alias;
- all **2,517** active aliases (1,722 M1-E + 795 M1-F), instantiated as
  concrete synthetic devices, map to exactly their own package and only their
  own Wi-Fi/Bluetooth classification;
- second-wave fixtures (`fw-09`…`fw-15`), a near-miss set (`fw-16`: Realtek
  Ethernet, card reader, USB audio and USB Ethernet, `ath11k`, both excluded
  ambiguous IDs, an unqualified Realtek network ID) and a combo-card
  negative (`fw-17`: MediaTek PCI Wi-Fi gets `firmware-mediatek` and
  `network.wifi`, its `btusb` Bluetooth half gets nothing);
- 16 kinds of evidence/rule drift are detected, and the `modules.alias`,
  Contents and ELF module parsers reject malformed input.

Two M1-E near-miss entries in `fw-08` were replaced in M1-F because the
devices they used (`168c:003e`, `10ec:c821`) are now correctly qualified by
`ath10k_pci` and `rtw88_8821ce`.

M1-F mutation coverage (gates 6 and 7): vendor-wide Realtek, Qualcomm and
MediaTek rules; a wrong package for a qualified module; committed SDIO and
BCMA aliases; Bluetooth firmware on a Wi-Fi PCI target; a Realtek audio alias
mapped as network firmware; a dropped alias; a foreign alias; firmware-file
ownership changed to the wrong package; an unexplained firmware-path
collision; an unexplained cross-package alias overlap; a package outside the
allowlist; a `non-free` source; runtime rule generation; an install
subprocess.

### Builder firmware requalification (M1-E)

Operator-run on `lubarchy-builder`, never in CI, unprivileged, nothing
installed and no module loaded:

1. Record `uname -r`, the owning kernel package and version, the
   `modules.alias` SHA-256, and confirm with `modinfo -F alias` (reading the
   package-owned `.ko` files) that each mapped module's aliases equal its
   `modules.alias` entries.
2. `tools/hwe/qualify-firmware-mappings.py check-evidence --modules-alias
   /lib/modules/$(uname -r)/modules.alias …` must report that the committed
   evidence regenerates byte-for-byte; `check-rules` must pass.
3. Verify each firmware package's component, version and long description
   with the authenticated metadata procedure above; optionally download each
   `.deb` with that metadata, check its hash against the signed index, list
   its contents with `dpkg-deb -c`, and delete it.
4. Run probe → resolve twice; on the QEMU builder no device firmware
   package may appear (only `intel-microcode`), with the usual side-effect
   checks.

Since M1-F the extraction also needs authenticated package data: run the
isolated APT procedure with Debian **Contents** index targets enabled (APT
verifies them against the signed `InRelease`), build the firmware-path →
package table with `/usr/lib/apt/apt-helper cat-file`, download each
relevant `.deb` only to hash and list its firmware files (then delete it),
and pass `--module-dir`, `--contents` and `--packages` to
`check-evidence`. Probe → resolve on the QEMU builder must still propose
only `intel-microcode`, with no Wi-Fi or Bluetooth classification.

This qualifies alias coverage and package provenance, not real hardware: it
does not show that firmware loads, Wi-Fi associates or graphics
acceleration works.

## M1 hardware NVIDIA strategy gate

`tests/nvidia-strategy.py` (gate 8 of `tests/run-static.sh`, since M1-G,
extended in M1-H) has 19 checks and uses committed files only (no network, no
new dependency):

- `hardware/nvidia/evidence-v1.json` is canonical and complete; strategy
  scores are in range; tiers use only recommended strategies; no
  strategy is an automatic default, and no tier claims production
  qualification. Strategy C is an opt-in proposal never offered to legacy
  GPU classes;
- Nouveau is a baseline/fallback candidate, never a qualified default:
  per-generation status is `limited` (legacy classes), `fallback-candidate`
  (Turing to Hopper) or `not-yet-qualified` (Blackwell); no row is
  production-qualified. The ADR states the Mesa 25.0.7 baseline, and
  Nouveau-default overclaims in the ADR and docs fail the gate;
- the signing-key record includes the 404 fallback URL and the
  index-exposed `8793F200.pub` (same full fingerprint), marks neither as a
  trust anchor and requires full-fingerprint verification through an
  explicitly qualified mechanism. Claims that the keyring package is the
  key's sole channel fail the gate;
- with open Debian security issues recorded, strategies A/B (Debian 550) are
  never recommended or automatic; no DKMS path is automatic while P-006 is
  undecided;
- NVIDIA repository facts (fingerprint and hash formats, branches vs.
  versions vs. pinning packages, missing `Valid-Until` recorded) are
  consistent;
- `hardware/nvidia/supported-gpus-v1.json` is canonical and self-consistent
  (`tools/hwe/normalize-nvidia-gpus.py check`), matches the evidence's
  provenance and generation matrix, and classifies well-known PCI IDs
  correctly. Four snapshot mutations must be rejected;
- the simulations record the key risks (`dkms` takeover, flavor switch,
  Nouveau blacklist, missing headers);
- ADR-0002 is Proposed and the register has no accepted NVIDIA decision;
- the production package set is exactly the 10 packages and none is NVIDIA,
  `class.gpu.nvidia` stays classification-only, there is no `gpu.hybrid`, the
  source policy is `main` + `non-free-firmware`, no tracked file outside the
  evidence names NVIDIA's repository or key, no key file is tracked, and the
  live-build configuration enables no `contrib`/`non-free` or NVIDIA package;
- the resolver, `resolve.py` and the probe never read the NVIDIA evidence.

- M1-H containment (`tools/hwe/validate-nvidia-apt-policy.py check`):
  - The policy is NOT ACTIVE, references ADR-0002 as Proposed, and has the
    exact Origin/Label, URI and fingerprint. It commits no key material and
    sets no `trusted=yes`.
  - Default deny is the last record. The allowlist is finite, exact and
    disjoint from Rule D. Every positive record is name plus version, and no
    allowlisted name matches a denied class.
  - Branch lock `615.*`; `dkms` is denied.
  - The rendered `apt-preferences.example` equals the rendering of the
    policy.
  - The fixtures are canonical, with the Release identity (absent
    `Suite`/`Codename`/`Valid-Until`, `Acquire-By-Hash: no`).
  - An `apt_preferences(5)` model must reproduce APT's recorded candidate
    for all 558 NVIDIA names, in both the product and the all-components
    context.
  - The contained transaction pulls exactly the closure, with Debian's
    `dkms`. Every closure dependency stays inside the closure or Debian, and
    every closure member is reachable from the two roots.
  - Classification covers every NVIDIA name. No non-closure NVIDIA name is
    eligible, and no unrelated overlap or infrastructure package resolves to
    NVIDIA.
  - Non-615 and future-branch versions are ineligible.
  - Recorded negative, branch-escape, precedence, explicit-override and
    vendor-pinning outcomes are present.
  - The Release-Date history is monotonic.
  - Nothing in `build/`, `.github/` or `hardware/rules/` references the
    policy, NVIDIA's URI or its key;
- 20 in-memory policy mutations (deny removed or permissive, wildcard,
  `dkms`/CUDA/fabric/keyring allowed, branch lock removed, 610, any branch,
  overlap wins, identity or fingerprint altered, active, `trusted=yes`, no
  rollback rule, ADR accepted, vendor pinning relied on, invented max age)
  must be rejected. The model must show the uncontained `dkms` takeover and
  none under the policy, and Rule D precedence must hold.
- The containment assessment is recorded as YES, scoped to LUBARCHY-managed
  automatic transactions, and does not authorise the repository. Its
  activation prerequisites (key bootstrap, freshness, privileged executor,
  P-006, ADR-0002) and the seven executor requirements must be present.
- Boundary wording: the gate fails on un-negated wording that presents
  pinning as protection from root administrators. It also fails on wording
  that presents an explicit exact-version request as nullifying the
  automatic containment. The empirical explicit-version evidence must stay
  in the fixtures.
- Release-Date rollback protection rejects a Date regression and a same-Date
  content change, and claims no maximum age.
- No tracked file contains an OpenPGP key block or `trusted=yes`.

### Isolated NVIDIA APT simulations (M1-G, builder only)

The strategy evidence comes from `apt-get -s` runs on the builder, each with
its own `Dir::State`, `Dir::Etc` and status copy (the system `/etc/apt` and
dpkg database are never written), Debian sources `Signed-By` the Debian
archive keyring and the NVIDIA repository `Signed-By` a dedicated copy of the
key extracted from `cuda-keyring` (full fingerprint
`02182E60104FCDC26EAE1B8597A5D4CB8793F200` checked; this is test-only and
not a qualified bootstrap mechanism). Upgrade, downgrade and removal paths use
synthetic installed states built from authenticated index metadata, with
auto-install marks. Downloaded `.deb` files are only hashed and listed, then
deleted. This qualifies package resolution and provenance, not drivers: no
module is built or loaded and no GPU is involved.

### NVIDIA repository containment simulations (M1-H, builder only)

Live requalification runs on the builder only; normal CI never contacts
NVIDIA.
- **Metadata:** each profile has its own `Dir::State`, `Dir::Etc` and cache.
  NVIDIA metadata is authenticated with an evidence-only copy of the key,
  whose full fingerprint is checked first by a standalone calculator
  (fail-closed). The verified Release is extracted with `sqv`, and the
  Packages hash is matched against it.
- **Matrix:** S0 Debian only, S1 uncontained, S2 default deny, S3 contained,
  S3X/S1X all Debian components (takeover audit), S4 contained upgrades and
  the in-branch update, S5 source disabled, S6 pinned rollbacks.
- **Other runs:**
  - negative and branch-escape installs;
  - apt_preferences precedence probes;
  - the vendor pinning package inspected (downloaded, hashed, extracted,
    deleted) and applied alone (SV);
  - explicit exact-version probes (`pkg=version`). These show that APT 3.0.3
    lets an explicit administrator request override candidate selection for
    the named package. The result is kept as evidence of a privileged
    override outside the containment boundary.
- **dkms criterion:** NVIDIA's `dkms` is not an APT candidate and cannot be
  selected by normal LUBARCHY-managed dependency resolution. No claim is
  made that APT stops an arbitrary root administrator from requesting that
  exact version.
- **Fixtures:** `tools/hwe/validate-nvidia-apt-policy.py extract` turns the
  capture into the committed fixtures. Exact-commit validation re-captures,
  re-extracts and compares byte-for-byte.
- **Repository mutation suite:** each mutation is committed in a throwaway
  clone and `tests/run-static.sh` must fail on it, after a passing baseline.

This qualifies package-source containment only. Nothing is installed, no
module is built and no GPU is involved.

## M1 hardware package transaction gate

`tests/hardware-transaction.py` (gate 9 of `tests/run-static.sh`, since
M1-I) has 17 checks and is synthetic only. It never contacts a mirror or
NVIDIA, never probes the runner, never runs apt or dpkg and never mutates
packages. Its inputs:
- the existing synthetic inventories (`tests/fixtures/hardware/firmware`,
  `inventory`) and the production rules;
- a deterministic package universe
  (`tests/fixtures/hardware/transaction/universe.json`), rendered by a fake
  read-only backend in APT's own text formats;
- real APT/dpkg output samples captured on the builder (`apt-samples/`),
  which check the production parsers.

It proves:
- **Contract:** schema meta-validity with every object closed and no
  time/identity fields; inventory/rule/plan v1 unchanged.
- **Determinism:** canonical bytes and digest are deterministic, and 10
  fixtures covering all 10 production packages compile to the reviewed
  transactions in `transaction/expected/`.
- **Authority:** package names equal the fresh plan's names; the compiler
  has no package, version, selector or option parameter; a caller-added
  package is rejected; CLI package, version, selector, option, rules,
  binary and `apply` arguments are usage errors; the CLI uses the fixed
  probe, production rules and system backend.
- **Staleness:** changes to the inventory, rule set, plan, inventory or
  rule digests, installed state, holds, repository metadata, package
  version, hash, size or hook configuration all make the transaction stale.
- **Plan acceptance:** `contrib` → unauthorised; forbid/conflict, invalid
  inventory and warnings → rejected; `partial` is accepted and keeps its
  unresolved targets.
- **Provenance:** wrong component (`non-free`, `contrib`), origin, label,
  codename (sid, Backports), an untrusted index or a foreign keyring rejects
  the transaction, including through a dependency; the plan component, the
  SHA-256 and the candidate are all bound.
- **Install-only:** `already_satisfied` works; upgrade, downgrade, removal,
  purge, reinstall, architecture change, held-package change, a hold on a
  new package and an unresolvable set are each rejected for the right
  reason; recommends are never selected.
- **Exact closure with direct-only targets:** only direct requirements are
  explicit targets (`name:arch=version`; the dependency `iucode-tool` never
  is). Their simulation must reproduce the full closure. An extra, missing,
  re-versioned or re-architected dependency is rejected.
- **Package marks:**
  - New direct packages end manual and new dependencies automatic.
  - An existing manually installed dependency stays manual and is never
    targeted.
  - Automatic↔manual flips (of a dependency or an unrelated package) make
    the transaction stale.
  - An already-installed automatic direct requirement fails closed.
  - Inconsistent marks fail the contract.
  - A wrong or foreign target architecture is rejected, and so is a
    dependency used as a target.
  - A dependency architecture change makes the transaction stale.
- **Future apply argv and environment:** an array starting with
  `/usr/bin/apt-get install` with `--no-install-recommends` and
  `--no-remove`, no forbidden option, plain `name:arch=version` direct
  targets only;
  caller `APT_CONFIG`/`DEBIAN_FRONTEND` ignored; executables and the
  environment are literal constants.
- **No mutation path:** `subprocess.run(shell=False)` only; no lock, kill
  or unlink; no apply implementation; an exact read-only operation
  allowlist (any install, download, update, `dpkg -i`, `-o` or shell
  request is refused before execution).
- **Real samples and unchanged policy:** the builder output samples parse;
  the source policy and the 10-package set are unchanged, and the NVIDIA
  policy stays inactive.

### Builder transaction dry-run qualification (M1-I, builder only)

`tools/hwe/qualify-transaction.py WORKDIR` builds an isolated APT environment:
- Debian `trixie`, `trixie-updates` and `trixie-security`, components
  `main` + `non-free-firmware`, Signed-By the Debian archive keyring;
- the system dpkg database used read-only;
- the system APT configuration and sources untouched.

It then:
- runs fresh probe → production resolve → compile twice and compares the
  bytes;
- verifies the transaction;
- records the install simulation, the direct-target simulation and its
  closure comparison with the intended package marks, the index targets, the
  candidate policy and the future argv (not executed);
- probes the APT target syntax in simulation only: native, `:all` and
  foreign architecture qualifiers for an arch-specific and an
  `Architecture: all` package;
- downloads each selected `.deb` into WORKDIR only, checks its SHA-256 and
  size against the transaction and deletes it;
- runs the production CLI against the system configuration, where it must
  fail closed.

A before/after audit compares:
- the dpkg status and package, manual, automatic and hold counts, and the
  APT `extended_states` hash;
- the APT sources and preferences;
- the system archive cache;
- loaded modules and failed units;
- APT history.

## M1 hardware package staging gate

`tests/hardware-staging.py` (gate 10 of `tests/run-static.sh`, since M1-J)
has 19 checks and is synthetic and offline only. Its inputs:
- APT source trees built in temporary directories;
- the real builder `sources.list` sample (`tests/fixtures/hardware/staging/`);
- synthetic Debian packages built deterministically in memory (`ar` with
  `tar.gz` members and fixed timestamps);
- the M1-I synthetic universe rendered by the gate-9 fake backend.

The only program it runs is `dpkg-deb --field` on those payloads. It never
contacts Debian or NVIDIA, never downloads a `.deb`, never runs `apt-get`,
never reads the runner's `/etc/apt` or hardware and writes only into its own
temporary directory.

It proves:
- **Contracts:** both new schemas are meta-valid and closed with no
  time/host/user/command fields. The protected contract schemas
  (inventory, rule, plan, package-transaction, source-activation,
  staged-bundle v1) match a committed SHA-256 baseline, so an accidental
  change is caught even in a shallow CI checkout.
- **Source policy:**
  - The rendering is deterministic, with `main` + `non-free-firmware` only,
    a fixed field order and mandatory `Signed-By`.
  - The real builder topology yields `create_owned_source` with exactly the
    foreign URIs.
  - These are rejected: a `trusted` option or field, a foreign keyring,
    `contrib`, `non-free`, Backports, testing, NVIDIA, third-party mirrors,
    a LUBARCHY repository and `ftp`.
  - These are rejected too: malformed one-line or deb822 input, `deb-src`,
    cdrom, options, non-UTF-8 files, symlinks, FIFOs and directories.
- **Ownership:** partial topologies, duplicate definitions, mixed schemes,
  unrecognised files, conflicting `Signed-By` values and redundant owned
  files fail closed. A stale owned file is replaced; foreign files never are.
  Rendering and isolated configuration write only into workspaces.
- **Drift:** the pre-state digest ignores comments and whitespace but not
  semantics. Source, keyring or owned-file drift makes the contract stale,
  and tampered contracts are rejected.
- **Metadata evidence:** index targets must equal the post-state and be
  Debian, `Trusted` and keyring-signed. An absent target is accepted only as
  an index the signed `InRelease` lists as empty. The metadata digest binds
  the index contents.
- **Staging:**
  - The bundle binds the transaction digest. Its payload list is exactly
    the install closure: the direct packages end manual, the dependency
    automatic, and the dependency is never an explicit target.
  - The manifest, bundle digest and payload bytes are deterministic, and
    modes are `0755`/`0644`.
  - These payloads are rejected with no residue: missing, extra, wrong
    SHA-256, wrong size, symlink, FIFO, directory or renamed.
  - A `dpkg-deb` package, version or architecture mismatch is rejected even
    when hash and size match.
- **Finalized bundle:** replacement, truncation, addition, removal, filename
  swap, symlink, mode change and manifest edits are all detected.
- **Freshness:** a changed transaction, source-activation or metadata
  digest is stale, and so is a newer Debian security candidate; any of
  them requires restaging.
- **Authority:**
  - Library signatures are fixed. The CLIs reject package, version, URI,
    component, option, root and apply input with exit 64, and system
    workspaces with exit 5. Staging-root escape and bundle-name traversal
    are refused.
  - The command allowlists refuse install, `dpkg -i`, extraction and the
    shell. The modules contain no shell and no `rmtree`.
  - The offline argv is constructed only: direct targets, `--no-download`,
    no forbidden option.
- **Cleanup:** removal is exact-digest and entry by entry, never follows a
  link, aborts on an unknown layout and refuses system roots.
- **Invariants:** the policy is `main` + `non-free-firmware` from Debian
  releases only, the production package set is 10, and the NVIDIA policy is
  inactive.

### M1-J mutation suite (builder only)

Each mutation is committed in a throwaway clone, and the full
`tests/run-static.sh` must fail through a functional assertion. The 36
mutations:
- enable `contrib`, `non-free`, Backports or NVIDIA;
- remove `Signed-By`; accept a `trusted` option; accept an arbitrary mirror;
- ignore source drift; allow a staging-path escape; allow symlink payloads;
- skip the SHA-256, size or `dpkg-deb` identity check;
- accept a missing or an extra package;
- drop the transaction or repository binding;
- emit a dependency as an explicit target;
- allow network retrieval or `dpkg -i` in the offline model;
- allow an unsigned repository configuration;
- stop verifying payloads after finalization;
- allow cleanup outside the bundle; allow a workspace under `/etc`;
- accept unauthenticated metadata or an unchecked empty index;
- accept duplicate definitions or mixed schemes;
- skip the bundle-digest or canonical-manifest check;
- accept a stale security candidate;
- add a caller package or source parameter;
- allow shell execution; make `Signed-By` optional; accept a foreign keyring.

### Builder source activation and staging qualification (M1-J, builder only)

`tools/hwe/qualify-staging.py WORKDIR` runs unprivileged (it refuses root)
and writes only inside WORKDIR. It:
- compiles the source-activation contract from the real `/etc/apt` (read
  only) twice and renders the owned file into WORKDIR;
- runs two independent chains. Each does an isolated authenticated update
  (empty `trusted.gpg.d`), a fresh probe and production resolve, a
  transaction against the isolated state, and staging of the payloads. The
  tool then compares the transaction, metadata digest, manifest and payload
  bytes of the two chains;
- re-verifies the bundle against all its bindings;
- proves the offline apply model on payload copies:
  - complete cache with `--no-download` and a black-holed proxy;
  - `--print-uris` with nothing to fetch;
  - a missing dependency that fails;
  - resolver mark decisions;
  - a non-root no-op-dpkg sandbox with private status and
    `extended_states` copies;
  - APT accepting a same-size corrupted cached payload, against LUBARCHY
    rejecting it;
- checks tamper detection, final revalidation and the production CLIs;
- deletes every `.deb`.

A before/after audit covers:
- dpkg status, package/manual/automatic/hold counts and `extended_states`;
- every file under `/etc/apt`, including the absence of the owned file;
- APT history and logs, and the lists and archive caches;
- loaded modules, running services and failed units;
- `/var/lib/lubarchy`.

## M1 hardware package apply gate

`tests/hardware-apply.py` (gate 11 of `tests/run-static.sh`, since M1-K)
has 14 checks.

It drives `hardware/package_apply.py` through a **fake host** rooted in a
temporary directory:
- a Debian-installer-style `sources.list`;
- the M1-I universe, whose `non-free-firmware` packages become visible only
  after a simulated `apt-get update`;
- synthetic `.deb` payloads, read only with `dpkg-deb --field`;
- a simulated APT install that changes only the fake dpkg state;
- hooks that inject drift and tampering at exact points.

It never writes `/etc`, `/run` or `/var/lib/lubarchy`, never runs an
installing APT or a mutating dpkg, needs no root (CI runs it as root
safely), and never contacts a network or inspects the runner. CLI usage
probes run in-process with the real host replaced by a sentinel, so even a
regression cannot reach the real executor in CI.

It proves:
- **End to end:**
  - The 11 phases are reached in order, and the journal shows `applying` on
    disk while APT runs.
  - Exactly one APT install runs, with the exact argv: direct targets only,
    `--no-download`, `--no-install-recommends`, `--no-remove`, the internal
    cache option and nothing forbidden.
  - Direct packages end manual and the dependency automatic.
  - The root-owned bundle is created, all digests are journaled, and the
    owned source has the exact rendered bytes and mode `0644`. The foreign
    tree is byte-identical.
  - A satisfied plan completes as `nothing_to_apply` without APT.
- **Privilege and API:**
  - Root is required.
  - The CLI rejects package, option, URI, component, staging-root and
    extra arguments.
  - A held lock gives `busy`.
  - A symlinked, wrongly-moded or world-writable lock path is refused, and
    the lock file is never deleted.
- **Source activation:**
  - `contrib`, NVIDIA and partial topologies fail before any command.
  - An injected write failure leaves no residue and restores a pre-existing
    owned file byte for byte.
  - A stale owned file is replaced.
  - A symlinked `sources.list.d` is refused.
  - The executor's only durable writes are the owned file and the journal.
- **Refresh and freshness:**
  - A failed `apt-get update` stops before staging.
  - No transaction is compiled before the refresh.
  - An unsafe upgrade is rejected.
  - A newer candidate appearing after staging is stale.
- **Staging and re-hash:**
  - A symlinked or writable staging root is refused.
  - Same-size payload corruption and an identity swap introduced after
    staging are caught by the immediate re-hash before APT.
  - An identity mismatch or an acquisition failure at staging is rejected.
  - The cache option outside the attempt directory is not allowlisted.
- **Stale state:** seven kinds of drift between staging and apply stop the
  transaction before APT: inventory, installed state, marks, holds, source
  state, metadata and manifest.
- **Journal:**
  - Each write fsyncs the file and the directory, mode `0600`, with no
    temporary residue.
  - A non-terminal journal or a post-APT failure gives `recovery_required`
    and nothing runs.
  - A failed `applying` write means APT never runs.
  - A failure before APT does not block the next attempt.
  - A malformed journal fails closed.
  - The journal holds no command output or environment, and phases never go
    backwards.
  - The schema matches its baseline hash. Gate 10's protected table also
    holds it.
- **APT failure:** a non-zero exit is journaled `failed` with
  `apply_started`, nothing else runs, and the next invocation is
  `recovery_required`. The executor source contains no remove, purge, `-f`,
  `--fix-broken`, `--configure` or `dpkg -i`.
- **Post-apply validation** fails on each of these: an extra package, a
  removal, a downgrade, a hold change, a dependency marked manual, a direct
  package marked automatic, a half-configured package, `dpkg --audit`
  output, an `apt-get check` failure, a missing package file, or a changed
  owned source.
- **Commands:**
  - The literal environment and `shell=False` hold even with `APT_CONFIG`,
    proxy, locale and `PYTHONPATH` set by the caller.
  - The allowlist refuses install without the cache option, `update -o`,
    `dpkg -i`, `-f`, remove, the shell, relative binaries, `--fix-broken`,
    `apt-mark`, foreign `-o` options and forbidden flags.
  - The modules contain no shell, `rmtree`, setuid, sudo or polkit.
- **Trusted deployment:** a real content-addressed deployment is built in
  a temporary directory (git repository, bare clone, `ls-tree` manifest,
  `git archive` with `tar.umask=0022`) and verified.
  - These are rejected: wrong owner, a group-writable file, an
    other-writable directory, a writable ancestor, content mismatch,
    untracked code, executable-bit drift, a symlinked file or deployment
    component, a wrong tree or commit, a tampered manifest, and the
    developer checkout itself.
  - The privileged argv is exactly `env -i python3 -I -B <deployment>/tree/
    hardware/apply-packages.py apply`.
  - Imports cannot be redirected by `PYTHONPATH`, `PYTHONHOME`, the current
    directory or decoy modules, while an unisolated control does load the
    decoy.
  - The import probe writes no bytecode.
  - The launcher execs only a verified deployment and refuses a tampered
    one.
- **Policy:** no NVIDIA, `contrib` or Backports path. The policy is `main`
  + `non-free-firmware`, the production package set is 10, and the NVIDIA
  policy is inactive.

### M1-K mutation suite

Each mutation is committed in a throwaway clone, and the full
`tests/run-static.sh` must fail through a functional assertion. The 46
mutations:
- **Privilege and boundary:** no root check; CLI accepting extra
  arguments; no lock; a lock that follows symlinks; lock-file deletion; a
  caller staging root.
- **Source:** no source-state recheck; modifying a foreign source; no
  owned-source restore; allowing an NVIDIA archive.
- **Freshness:** skipping the metadata refresh; reusing a pre-activation
  transaction; allowing unsafe transactions; skipping the installed-state,
  marks or metadata revalidation.
- **Payload integrity:** skipping the immediate re-hash; size-only
  verification; skipping `dpkg-deb` verification.
- **Journal:** no journal fsync; no `applying` phase; permitting a second
  active journal; marking `complete` before validation; skipping
  validation.
- **Command and environment:** enabling the shell; inheriting `APT_CONFIG`
  or proxies.
- **APT argv:** an explicit dependency target; removing `--no-download` or
  `--no-remove`; enabling recommends; adding `--allow-unauthenticated`,
  `--allow-downgrades` or `--force-yes`; using `dpkg -i`.
- **Recovery:** automatic `apt-get -f install` after a failure.
- **Trusted deployment:** skipping the owner, writability, symlink,
  ancestor, content, untracked-file or Git-identity check; launching from
  the developer clone; dropping `-I`; launching without verification.

### Builder apply qualification (M1-K, builder only)

The only authorised real package-manager mutation runs on the
`lubarchy-builder` VM, under this procedure:
1. The builder is shut off. A host-level libvirt checkpoint of all writable
   VM state (system disk and UEFI NVRAM) is created, and its restoration is
   proven before any mutation.
2. Boot. Capture the pre-state:
   - the source tree;
   - dpkg state, marks, holds and package counts;
   - the APT cache and history;
   - services and modules.
3. Root creates the trusted deployment `/root/lubarchy-m1k/<commit>/` from
   a bundle of the exact commit, using system `git`/`tar` only. Root then
   runs `tools/hwe/trusted-deployment.py verify` and `imports` from that
   deployment, and launches `run-apply` once. The user-owned clone is never
   executed by root; it only runs the unprivileged gates and read-only
   captures.
4. Validate the post-apply state:
   - packages, versions, architectures and marks;
   - the source post-state and the foreign files;
   - `dpkg --audit`, `apt-get check`;
   - the journal transitions;
   - no network fetch during apply.
5. Run one controlled reboot, then re-validate.
6. Shut down, restore the checkpoint, boot and run a read-only audit that
   must match the original baseline. Then shut down with autostart
   disabled.

Results are recorded in the evidence bundle of the exact commit. VM package
success is not physical CPU-microcode or real-hardware qualification.

**Result (exact commit `bee741485ce2fb6db35d433b8c0102bc59824e9f`):** one corrected real privileged package transaction on the rollback-protected
builder VM, executed only from the root-owned exact-commit deployment
`/root/lubarchy-m1k/bee741485ce2fb6db35d433b8c0102bc59824e9f/`.
It performed:
- source activation of the LUBARCHY-owned file;
- an authenticated metadata refresh;
- a fresh transaction and root-owned staging;
- an immediate payload re-hash and final stale-state revalidation;
- one journaled offline APT apply (`intel-microcode` direct, `iucode-tool`
  dependency);
- package and mark validation, `dpkg --audit` and `apt-get check`;
- a reboot validation.

The VM checkpoint was then restored, and a complete audit matched the
pre-M1-K baseline. CI never performs a real mutation: gate 11 uses a fake
host and synthetic trees.

Every LUBARCHY Python file executed as root came from that deployment's
`tree/`. The developer clone ran only the unprivileged gates and read-only
captures. An earlier qualification of the superseded unpublished commit
`5d4fee5` had executed the entrypoint from the user-owned clone; it was
reverted and repeated as above.

## M1 hardware package recovery gate

`tests/hardware-recovery.py` (gate 12 of `tests/run-static.sh`, since M1-L)
has 15 checks.

It drives `hardware/package_recovery.py` through the gate-11 fake host. Its
interrupted attempts are modelled on the states measured on the builder:
- an interrupted initramfs-tools trigger (closure installed, initramfs-tools
  half-configured);
- an interruption at the start of `dpkg --unpack` (no package changed);
- interruptions before `applying` and after APT completed;
- unpacked, half-installed and partially absent closure packages, and
  unrelated damage.

The fake host writes `/var/log/dpkg.log` in the sequence measured on the
builder (unpack with man-db's file trigger activated, configure with
intel-microcode activating `update-initramfs`, then trigger processing),
cuts it where each simulated SIGKILL lands, and derives the fake package
state by replaying exactly the logged prefix, so log and package database
agree as they do on a real system. Process death is simulated by an
exception the executor cannot catch, so journals stay exactly where a real
SIGKILL leaves them. Fake exact-package dpkg commands change only fake
state and log their own lines. The fake dpkg follows the measured
semantics: a configure runs the postinst (intel-microcode's and
initramfs-tools' activate `update-initramfs`) and, unless `--no-triggers`
is given, then processes every pending trigger; `--triggers-only <names>`
processes only the named packages; every executed handler is recorded. The gate never runs APT or a mutating
dpkg and needs no root.

It proves:
- **Contract:** the recovery-journal schema is meta-valid, closed and
  matches its protected baseline (also in gate 10's table).
- **Stopped before applying:** classified `no_mutation` and abandoned.
  - The attempt's apply cache and `.staging-*` are removed; the journal and
    the owned source are kept.
  - A resolved attempt no longer blocks apply. A stale retained bundle is
    restaged and a valid one reused.
  - A stray entry in the apply cache aborts cleanup without touching the
    bundle.
  - A changed package state despite no mutation is manual.
- **APT started without a package change:** `no_package_change`, proven by
  the pre-state digest.
- **Complete post-state:** `applied_but_unvalidated`. It is validated and
  closed with no APT and no dpkg configure.
- **Bounded completions:**
  - an interrupted trigger → `dpkg --configure --no-triggers
    initramfs-tools`, then `dpkg --triggers-only initramfs-tools`;
  - killed right after the `update-initramfs` activation →
    `--triggers-only initramfs-tools man-db` (both triggers attributed).

  Each re-verifies the original bundle against the journal's digests, runs
  only that argv and no APT, leaves native dpkg lock files untouched and
  unblocks apply.
- **Recovery impact set (attributed):** the apply journal binds the dpkg log
  cursor and the trigger-interest records in `bundle_verified`, before
  `applying`. Recovery records the closure (three packages) and, separately,
  the impact set: initramfs-tools (activated during
  `configure intel-microcode:amd64`, events triggers-pending → trigproc →
  half-configured, interest `update-initramfs`) and man-db (activated
  during `install iucode-tool:amd64`, completed). The authorised targets and
  the argv are `initramfs-tools` only; the post-state includes both sets. A
  recovery that changes a closure package fails validation.
- **29 unattributable, tampered or missing-evidence cases** →
  `manual_recovery_required`, zero mutation, apply blocked:
  - an unrelated unpacked package (state only, and with its own dpkg log
    lines); an unrelated half-configured package; an unrelated package
    declaring trigger interest but never activated;
  - an impact package unhealthy before apply; impact version or
    architecture drift; an impact state that is not its last logged state;
  - a tampered log prefix, a truncated or rotated log, a tampered cursor,
    a tampered pre-apply record, a changed trigger declaration, a forged
    hitchhiker line, a non-trigger operation on an impact package, a
    closure line with the wrong version, a closure state disagreeing with
    the log, an incomplete or unknown line, an impact line before the
    first closure operation, an impact package with no activation, a
    symlinked log;
  - missing evidence (field absent or null) and a deleted log;
  - a second trigger package trying to hitchhike (an attributed but
    completed package set back to half-configured, and an unbound trigger
    package left pending);
  - a failed recovery whose own dpkg run already logged activity.
- **Trigger execution containment (15 cases):** the real case runs exactly
  two contained phases (handlers `configure initramfs-tools`, then
  `trigproc initramfs-tools`; per-step cursors, delta digests and pending
  sets journaled; every other package unchanged; repeated recover is
  `nothing_to_recover`). Manual recovery, with the unauthorised handler
  never executed and the authorisation never expanded, when: configuring
  the target activates an unrelated trigger; trigger processing activates
  one; configuring an unpacked closure activates `update-initramfs` of an
  initramfs-tools that the original transaction never touched; the
  command's log delta shows an unauthorised `trigproc` or `configure`, or a
  `trigproc` in the configure phase; an authorised package is still pending
  after phase B; an authorised package's mark or an unrelated package
  changes during a command.
- **Bounded argv:** an argv widened beyond the authorised set (an unrelated
  name, `-a`, `--pending`, an empty list, a complete closure package, a
  non-target trigger package, `apt-get -f install`, `--remove`) is a
  `contract_failure` before any mutation, and apply stays blocked.
- **17 fail-closed states** → `manual_recovery_required`, with no command
  run and apply kept blocked:
  - an unrelated pending package, a half-installed closure, a partially
    absent closure;
  - a change outside the closure, a hold, a mark of an unrelated package;
  - a closure version, architecture or mark deviation;
  - a non-trigger package left half-configured;
  - a corrupt or missing payload;
  - a journal transaction-digest, bundle-digest or closure mismatch;
  - owned or foreign source drift.
- **Journals:**
  - a garbled apply journal is rejected;
  - two interrupted attempts are manual;
  - an interrupted recovery is closed as failed and re-inspected;
  - a failing dpkg command gives a durable failed recovery with no retry or
    repair, and apply stays blocked;
  - a deviating post-recovery state fails.
- **Boundaries:**
  - the shared lock (recovery and apply mutually exclusive), root only, a
    CLI with no input;
  - the allowlist rejects `-a`, `--pending`, options, `-i`, `--remove`,
    `--purge`, `apt-get -f install`, `apt-mark` and duplicates;
  - the recovery module has no subprocess, shell, `rmtree`, install,
    remove, purge, update, download, NVIDIA, `contrib` or Backports;
  - the environment is literal;
  - recovery is launched only as `env -i python3 -I -B <verified
    deployment>/tree/hardware/apply-packages.py recover`.

### M1-L mutation suite

Each mutation is committed in a throwaway clone, and the full
`tests/run-static.sh` must fail. The 51 mutations:
- **Unrelated damage and provenance:** ignore unrelated broken packages;
  skip the bundle re-hash; allow a missing payload; ignore a bundle-digest
  or closure mismatch; allow foreign source drift.
- **Unwanted actions:** re-run on a complete state; refresh over the
  network; allow a version change; allow a removal command; run a generic
  `apt-get -f install`; run unrestricted `dpkg --configure -a`.
- **Locks and inputs:** skip the recovery lock; remove native dpkg locks;
  accept arbitrary CLI input; recover from the user-writable tree.
- **Journals:** erase the recovery journal after a failure; let cleanup
  erase the apply journal; mark `complete` before health checks.
- **Attribution:** treat every non-final package as impact without evidence;
  skip the pre-state reconstruction; abandon despite changes; ignore a mark
  deviation; let apply ignore an unresolved recovery.
- **Recovery impact set (amendment):** ignore unrelated non-final packages;
  ignore the log prefix digest or truncation; accept an impact line before
  the first closure operation; accept an impact package unhealthy before
  apply; accept an impact package without activation; allow non-trigger
  operations on impact packages; ignore impact version drift; ignore the
  last logged impact state; ignore a changed trigger declaration; drop the
  argv authorisation; apply without binding the evidence; enter `applying`
  without durable evidence; accept unbound log packages; ignore unexpected
  closure log operations; ignore closure/log disagreement; accept an
  incomplete log line; accept missing evidence; drop impact membership and
  normalise every non-final package to installed.
- **Trigger execution boundary:** configure without `--no-triggers`; let
  the allowlist accept a plain configure; ignore step containment; accept
  an unauthorised trigger activation; expand the authorisation
  dynamically; ignore an unrelated state change; ignore an unauthorised
  handler; ignore a package still pending after recovery; ignore an
  authorised package's identity change.

The 46 M1-K mutations are re-run on the same commit.

### Builder recovery qualification (M1-L, builder only)

Run from a fresh M1-L checkpoint of the shut-off builder (disk and UEFI
NVRAM). The fault injector (`interrupt.sh`, qualification only):
- launches the trusted-deployment `run-apply`;
- records the launcher's descendant tree;
- SIGKILLs only that tree at the trigger point.

APT puts dpkg in its own session, so matching is by parent-PID ancestry,
never by name across the system.

Every recovery runs as root from the root-owned exact-commit deployment
(`run-recover`). The scenarios:
1. **Before applying.** Killed while staging, the journal before
   `applying`. Expected: `no_mutation` / `abandoned`, with bounded cleanup.
2. **Applied but unvalidated.** Killed right after `apt-get` exits.
   Expected: `applied_but_unvalidated` / `complete`, with nothing re-run.
3. **Real partial interruption.** Killed during the initramfs-tools trigger.
   Expected: `bounded_incomplete_transaction`, with the closure
   (`intel-microcode`, `iucode-tool`) and the recovery impact set
   (initramfs-tools half-configured, man-db complete) recorded separately
   with their dpkg-log evidence, recovered with
   `dpkg --configure --no-triggers initramfs-tools`, re-inspected, then
   `dpkg --triggers-only initramfs-tools`, each contained by its own log
   cursor, then a reboot.
4. **Unrelated damage.** Like 3, plus an unrelated unpacked package.
   Expected: `manual_recovery_required` and no mutation.

The VM is restored from the checkpoint and audited against the original
baseline. Results are in the evidence bundle of the exact commit. VM package
success is not physical CPU-microcode or real-hardware qualification.

**Result (exact commit `3b0ec8869a3034c7df38c25c004387bf99750d7b`):**
scenario 3 was re-run on the builder from the `pre-m1l` checkpoint with
the final recovery architecture. Recovery ran only from the root-owned
exact-commit deployment `/root/lubarchy-m1l/<commit>/`.
- **Interruption:** SIGKILL of only the attempt's process tree during the
  initramfs-tools trigger; apply then refused with `recovery_required`.
- **Attribution:** the transaction closure stayed `intel-microcode` and
  `iucode-tool`. The recovery impact set, proven from the bound dpkg log
  cursor, was initramfs-tools (activated during
  `configure intel-microcode:amd64`, half-configured) and man-db
  (completed, not a target). The authorised targets were initramfs-tools
  only.
- **Execution:** `dpkg --configure --no-triggers initramfs-tools`, then a
  re-inspection showing initramfs-tools trigger-pending on its own
  `update-initramfs` trigger, then `dpkg --triggers-only initramfs-tools`.
  Each command's own log delta named only initramfs-tools, and its only
  handlers were `configure` and then `trigproc` of that package.
- **Result:** journal `complete`; `dpkg --audit` and `apt-get check`
  clean; marks unchanged; no holds; the initrd regenerated with the Intel
  microcode. After a reboot: system running, no failed units, and a
  repeated `recover` reported `nothing_to_recover`.
- **Restore:** the VM was reverted to `pre-m1l`, and a complete audit
  matched the original baseline.

Before that, the same state was measured with manual commands:
`--configure --no-triggers` alone leaves initramfs-tools trigger-pending,
so the separately authorised trigger phase is required
([package-recovery.md](package-recovery.md#trigger-execution-boundary)).
CI never performs a real mutation: gate 12 uses a fake host.

## M1 hardware post-install validation gate

`tests/hardware-validation.py` (gate 13 of `tests/run-static.sh`, since M1-M)
has 13 checks. It drives `hardware/hardware_validation.py` and
`hardware/validate-hardware.py` with synthetic inputs only:
- inventories from the resolver fixtures, plus devices instantiated from
  the committed firmware alias evidence (the firmware gate's helpers):
  Intel, Realtek, MediaTek and Atheros Wi-Fi, Intel and AMD GPUs, an
  Atheros USB Bluetooth controller, Intel and AMD CPUs;
- synthetic normalized runtime evidence: an all-healthy document, then one
  perturbation per case;
- a synthetic sysfs/procfs tree and a fake command runner for the
  collector.

It never reads the runner's hardware, kernel log, packages or services,
runs no command and needs no root.

It proves:
- **Contract:** the result schema is meta-valid, closed, protected (also in
  gate 10's table) and its reason codes equal the module's.
- **Profile:**
  - it covers exactly the production classifications and package rules;
  - the embedded firmware-family modules semantically equal the canonical
    `tools/hwe/firmware-mappings.json`, and six kinds of drift (module or
    family added or removed, policy for an unknown family, an entry without
    evidence or with a bad requirement) are detected;
  - the microcode vendors equal the rules; NVIDIA stays unqualified;
  - the production firmware requirement policy is reported.
- **Healthy machines PASS** (Intel/AMD CPU with each Wi-Fi family, GPU and
  Bluetooth; Ethernet, USB Bluetooth, NVMe and AMD fixtures), attributed to
  the exact inventory identity, canonical and independent of input order.
- **Device failures:**
  - wrong driver (`driver_mismatch`), no driver (`expected_driver_not_bound`,
    dependents `NOT_TESTED`), missing device, changed identity;
  - a firmware request failure of the target is `LIMITED`
    (`firmware_requirement_unclassified`, the production policy has no
    entries), while failures of another device or unattributed ones are
    ignored for it;
  - missing wireless interface, DRM node or HCI controller; a Bluetooth
    device bound to the wrong driver;
  - an unrelated, unvalidated device that disappears does not affect the
    result.
- **Firmware requirement semantics:** a TEST-ONLY policy is installed
  temporarily, and the cases are:
  - required → FAIL; optional → LIMITED; unknown or unnamed → LIMITED,
    never PASS or FAIL;
  - required is never downgraded, and unknown outranks optional;
  - an optional failure does not mask a missing wireless interface; an
    unknown failure does not mask a wrong or missing driver;
  - unrelated or foreign failures are ignored, and another device's failure
    or another family's policy cannot affect the target;
  - an ambiguous policy fails closed;
  - a caller cannot supply a classification (an evidence field is rejected;
    there is no parameter);
  - a policy change changes the profile digest and makes an existing result
    stale.
- **LIMITED and NOT_TESTED are never PASS:**
  - an unavailable kernel log, a VM, a container and the same boot give
    `LIMITED`; a microcode loader error gives `FAIL`; a silent loader is
    `LIMITED`;
  - NVIDIA with `nouveau` or `nvidia` bound is `NOT_TESTED` and never
    PASS, with no DRM check;
  - the aggregation table (FAIL outranks LIMITED; advisory failures cap at
    LIMITED); a result with an inconsistent status is rejected.
- **Packages and system:** a missing or half-configured package, a version
  mismatch against the apply journal, `dpkg --audit` and `apt-get check`
  failures, an unavailable `apt-get check` (LIMITED), a critical unit
  failure (FAIL) versus unrelated failed units (advisory), maintenance and
  starting states, a read-only root, a missing root block device, and ext4
  error counters.
- **Freshness:** the inventory, rule set, plan, installed state (version
  and mark), boot and profile are bound; each change makes a result stale,
  and a tampered binding is not current.
- **Continuity:** a revalidation after a reboot passes when devices remain;
  a device gone after the reboot is `device_missing_after_reboot`; a
  previous result from another profile is rejected.
- **Evidence integrity:** 17 malformed, duplicated, ambiguous or smuggling
  evidence documents are rejected, including two inventory devices on one
  sysfs device and a firmware failure carrying an extra raw-text field.
- **Collector:**
  - it normalizes the driver, module, wireless interface, root block device
    attribution, units, marks and install times;
  - it attributes kernel firmware failures to the device;
  - it treats a permission-denied kernel log as unavailable and a
    permission-denied `apt-get check` as unavailable;
  - it leaks no MAC address, serial number, hostname, interface name, uid,
    boot ID or path;
  - it leaves the tree byte-for-byte unchanged;
  - it runs only seven fixed argv lists with `shell=False` and a literal
    environment, refusing any other command.
- **No caller input:** the CLI refuses device IDs, paths, drivers,
  packages, checks and commands (exit 64) before anything runs.
- **Read-only module:** no write, unlink, mkdir, chmod, exec or shell; the
  imports are limited; no install, configure, source activation or
  modprobe.

### M1-M mutation suite

Each mutation is committed in a throwaway clone, and the full
`tests/run-static.sh` must fail. The 43 mutations:
- **Status semantics:** always PASS; LIMITED counts as PASS; NOT_TESTED
  counts as PASS.
- **Devices:** ignore a wrong driver, a missing driver, a missing device or
  an identity change; ignore a firmware failure; accept unattributed or
  other devices' firmware evidence; drop device continuity; drop the
  wireless check from the profile.
- **Firmware requirements:** ignore a known-required failure; downgrade it
  to LIMITED; let unknown outrank required; treat an unknown failure as
  PASS or as FAIL; treat an optional failure as FAIL; accept a
  caller-supplied requirement; omit the policy from the profile digest;
  accept drift of the embedded module mapping.
- **Freshness:** skip the installed-state or boot binding; accept a stale
  result.
- **Privacy:** keep unit instance names; store raw kernel log lines.
- **Boundaries:** accept a caller path or a caller command; invoke a shell;
  inherit the caller's environment; install a package during validation;
  write a file from the collector; accept smuggled extra evidence fields.
- **Health:** ignore a dpkg, APT, critical-unit, system-state or read-only
  root failure; skip the package presence or version check.
- **Limitations:** VM microcode PASS; same-boot microcode PASS; assume an
  available kernel log; NVIDIA PASS without qualification.

The M1-L (51) and M1-K (46) mutations are re-run on the same commit.

### Builder validation qualification (M1-M, builder only, read-only)

The builder VM qualifies only the validator's runtime: no snapshot is
created, because nothing is mutated. From the exact commit (a fresh
clone), boot the shut-off builder and run `validate`, `evidence` and
`check-current` as the unprivileged builder user, and `validate` as root.
Results are measured, not predicted:
- **Expected on a VM:** physical microcode `LIMITED`; physical radio and
  GPU function not claimed.
- **Restored baseline:** it has no hardware package installed, so the plan
  packages are expected to fail.

A reboot then shows the boot binding: the earlier result becomes stale,
and a revalidation reports continuity. The builder is shut down again. VM
results are not real-hardware qualification.

**Result (exact commit `59793b6dcdaddf73946dcdd9a6681ac58b2fc644`):** read-only, no snapshot, one
Polkit `start`, reboot and shutdown over SSH. The unprivileged run used a
fresh clone in tmpfs `/tmp`; the root run used only the verified root-owned
exact-commit deployment.
- **Current builder validation result: `FAIL`, as measured and correct.**
  The resolved plan requires `intel-microcode`, which is deliberately absent
  from the restored clean baseline (`package_missing`). This is the validator
  detecting a real expected-state mismatch. The package was not installed to
  make the result green; the milestone qualification does not depend on the
  builder's own validation result being `PASS`.
- **CPU microcode:** `LIMITED` (`virtualized_cpu`): a VM never proves
  physical microcode.
- **Package manager:** `apt-get check` is `LIMITED` without root (it needs
  the dpkg frontend lock), and clean as root. The kernel log is unavailable
  without root (`kernel.dmesg_restrict=1`) and readable as root, with no
  firmware failures.
- **Devices and system:** virtio-net (present, driver, interface), SATA
  AHCI (present, driver), root storage (ext4, read-write, attributed, no
  errors), system `running` and `dpkg --audit` all `PASS`.
- **Not claimed:** no physical Wi-Fi, Bluetooth or GPU function (the VM has
  no such targets; every result carries `physical_function_not_exercised`).
- **Boot binding:** after the reboot the boot-A result was stale with
  exactly `boot_id_digest` changed (exit 3), revalidation showed continuity
  `PASS` for both devices, and boot A and boot B results were identical
  apart from boot identity.
- **Clean-up:** everything placed on the VM was removed, the home listing and
  the baseline audit matched exactly, and the builder ended shut off with
  autostart disabled and 0 snapshots.

## M1 hardware package state GC gate

`tests/hardware-gc.py` (gate 14 of `tests/run-static.sh`, since M1-N) has 10
checks (8 since M1-N, plus 2 from the M1-N APT sandbox correction).

It drives `hardware/package_gc.py`, `apply-packages.py gc-plan`/`gc` and the
trusted-deployment launcher against package state created by the **real**
apply executor on the gate-11 fake host (a temporary root): a complete
attempt with its bundle and hard-linked apply cache. It then adds variants:
- journal phases;
- cloned and corrupted bundles;
- recovery journals written through the real recovery journal class;
- temporary staging directories;
- hostile objects.

It never touches the runner's `/var/lib`, APT or dpkg state and needs no
root.

It proves:
- **Contracts:** both schemas are meta-valid, closed, protected (gate 10's
  table) and their codes equal the module's; the four fixed roots are
  unchanged.
- **Retention graph:**
  - a complete bundle is `eligible_complete`;
  - nonterminal, applied-but-unvalidated and bundle-verified attempts are
    `protected_active`;
  - failed-after-APT is `protected_recovery`;
  - failed-before-APT is `eligible_abandoned`;
  - recoveries ending `manual` or `failed` are protected; `complete` and
    `abandoned` make the bundle eligible;
  - a nonterminal recovery protects everything; the strongest reference
    wins;
  - unreferenced bundles, and corrupt, missing or unexpected journals, are
    `protected_unknown`;
  - the plan is deterministic.
- **Integrity cases retained** (`invalid_or_ambiguous`):
  - a corrupt, missing or extra payload; an extra entry;
  - a manifest, digest or name mismatch;
  - a symlink or FIFO payload; a group-writable directory;
  - a hard link outside the attempt caches; unexpected cache entries;
  - a copied (not linked) cache file;
  - a symlinked bundle and a mount crossing.

  A wrong owner or a symlinked root is refused.
- **Temporary and unrecognized entries:**
  - a safe temporary directory is eligible; one a staging attempt may own is
    protected;
  - temporaries with a symlink, a nested directory, mode 0777, a symlinked
    `archives`, or a foreign owner are retained;
  - look-alike names, foreign files and directories and symlinks are
    preserved, untouched.
- **Execution:**
  - it removes exactly the eligible bundles (with the payload links of their
    caches) and the safe temporary, keeping APT's `lock`;
  - the manual-recovery bundle stays verifiable and apply stays blocked;
  - the corrupt bundle, a sentinel, every journal and the APT cache, dpkg
    and source state are untouched;
  - the durable receipt equals the result;
  - a second run is `nothing_to_collect` and writes no receipt.
- **Races and failures** (injected after the plan, under the lock):
  - a changed journal, a replaced bundle, a replaced or symlinked payload, an
    extra file, a vanished candidate, a changed cache and a changed temporary
    all fail closed before any unlink;
  - a partial failure gives a durable `partial` receipt with exact counts;
  - a crash leaves a `deleting` receipt, never a success;
  - a receipt that cannot be written (disk full) prevents all deletion.
- **Lock:** GC and plan are refused while the shared lock is held; apply,
  recovery and GC are refused while GC holds it; both operations are root
  only.
- **No caller control:**
  - the CLI refuses a path, age, attempt ID, force, recursive option, plan
    file, size or delete list (exit 64);
  - the launcher operations are fixed;
  - there is no rmtree, shell, subprocess, time, mtime, environment or argv
    in the module, and no APT cache, dpkg or source path;
  - deletion is descriptor-relative; journals are never deleted.

**APT sandbox model (M1-N correction).** Real APT changes the attempt
cache's `partial/` to `_apt:root`, mode `0700`, as measured on the builder.
An unprivileged gate cannot `chown`, so the gate does two things:
- it overlays exactly that observed ownership on the inodes of the
  `partial/` directories of attempts whose APT ran, keyed by
  (device, inode), on GC's single metadata read (`package_gc._lstat`); a
  replaced directory loses it;
- it writes a root-owned `etc/passwd` naming `_apt` into the fake root.

The two added checks prove:
- **Accepted:** the exact shape is accepted, a real-shaped complete bundle
  is eligible and reclaimed together with its `partial/`, and a second run
  is idempotent.
- **Retained** (43 cases):
  - a foreign UID; `_apt` with the wrong group; seven wrong modes;
  - a non-empty or nested `partial/`;
  - a symlink, even one with the sandbox owner; a FIFO or regular-file
    `partial/`; a replaced `partial/`;
  - a mount crossing;
  - the sandbox UID, or a foreign UID, on the lock file, cache directory,
    cache payload, bundle directory, manifest or archives, and on a
    temporary directory;
  - a root-owned `partial/` in a cache APT used;
  - `_apt` where APT never ran;
  - a missing, duplicate, aliased, root, non-system or other `_apt` entry;
    a malformed `/etc/passwd`; a group- or other-writable `/etc/passwd`; a
    symlinked or missing `/etc/passwd`.
- **Root-owned `partial/`** is accepted only for an attempt that never ran
  APT.
- **Fixed inputs:** the environment cannot change the sandbox identity, and
  the expectations are fixed source constants.
- **Rechecked before deletion:** an owner, group or mode change, contents
  appearing, or a replacement of `partial/` after classification gives a
  `failed` receipt before any unlink, with the bundle intact.

### M1-N mutation suite

Each mutation is committed in a throwaway clone, and the full
`tests/run-static.sh` must fail. The 25 mutations:
- **Retention:** delete an unresolved bundle; delete manual-recovery
  evidence; delete a corrupt bundle; delete an unknown directory; treat an
  ambiguous graph as eligible; delete a temporary a staging attempt may own;
  ignore hard-link ambiguity; skip the bundle verification.
- **Time and caller input:** trust mtimes; accept a caller plan; accept a
  caller-selected transaction; permit an environment-supplied root.
- **Lock and races:** skip the lock; skip the post-lock identity
  revalidation; skip the journal recheck; ignore an extra file; follow a
  symlink; ignore ownership; ignore mount crossing.
- **Deletion:** use recursive deletion; delete journals; touch the APT cache;
  delete APT's cache lock file; write the receipt only after deletion;
  report success after a partial failure.

The M1-M (43), M1-L (51) and M1-K (46) mutations are re-run on the same
commit.

The M1-N correction adds 17 mutations:
- **Ownership:** accept any UID; accept any non-root owner; ignore the
  group; ignore the mode; permit `_apt` ownership anywhere; disable the APT
  sandbox expectation; accept the sandbox owner where APT never ran.
- **Contents and type:** accept a non-empty `partial/`; accept a symlink
  `partial/`.
- **Recheck before deletion:** skip the owner recheck; skip the listing
  recheck; skip the identity recheck.
- **Sandbox identity:** hard-code a fallback UID; read the sandbox user from
  the environment; ignore duplicate or aliased UIDs; trust an unsafe
  `/etc/passwd`; follow a symlinked `/etc/passwd`.

They are run together with the existing 25 M1-N mutations (two of whose
targets moved to the new `_lstat` read) and the M1-M, M1-L and M1-K suites.

**Inode-reuse harness defect.** `Static gates` run #36
([38888081993](https://github.com/lubarchy/lubarchy/actions/runs/38888081993)) failed gate 14 on the product correction
`dfd82cb`, in the two "replaced `partial/`" cases only.
- **Cause:** the overlay was keyed by (device, inode) and outlived its
  object. The runner's filesystem gave the re-created directory the freed
  inode number, so the new root-owned replacement inherited the removed
  object's `_apt` overlay.
- **Not a product failure:** a real replacement is root-owned and is
  rejected.

The test-only fix `9e7ad19` changes only `tests/hardware-gc.py`:
- **Object lifetime:** the overlay now lives exactly as long as its object.
  `os.rmdir`, `os.unlink`/`os.remove` and `os.rename`/`os.replace` are
  wrapped during the gate, so an overlay ends when the object's last link
  is destroyed, whoever destroys it.
- **One helper:** `replace_partial()` replaces `partial/` and asserts the
  replacement is observed root-owned `0700`.
- **Forced reuse:** a deterministic same-inode-identity model adds variants
  of both replacement cases.
- **GC's own removal:** the accepted-shape case also asserts that GC's
  removal of `partial/` ends its overlay.

Under a forced inode-reuse simulator the old harness reproduces the run-#36
failures exactly and the fixed harness passes. Run #37
([38896919894](https://github.com/lubarchy/lubarchy/actions/runs/38896919894)) passed 14/14 gates with gate 14 at 10
checks. The real-builder qualification of `dfd82cb` remains authoritative,
because `hardware/package_gc.py` is byte-identical.

### Builder GC qualification (M1-N, builder only)

Real deletion is qualified only on the builder under a fresh rollback
checkpoint `pre-m1n`: disk and UEFI NVRAM, the builder shut off, created
with an individual Polkit approval.

1. **Fixture.** From the root-owned exact-commit deployment, a
   qualification-only fixture is created by root under the production roots
   (not part of the product or the CLI):
   - a complete attempt with its verified bundle and apply cache;
   - a failed-after-APT attempt with a manual-recovery journal and its
     bundle;
   - a complete attempt whose bundle is corrupt;
   - a temporary staging directory;
   - a foreign sentinel.

   No real hardware package is installed.
2. **Plan and GC.** `run-gc-plan`, then `run-gc`, then `run-gc` again
   (idempotence) and `run-gc-plan`.
3. **Checks.** The eligible bundle, its cache links and the temporary
   directory are removed. The recovery bundle (still verifiable, `status`
   and `apply` still report recovery required), the corrupt bundle, the
   sentinel and every journal remain. APT, dpkg and source state are
   unchanged.
4. **Restore.** The VM is reverted to `pre-m1n`, booted and audited against
   the original baseline, then shut down. The checkpoint is kept until
   publication closure.

**Result (exact commit `1eec088956d73cdc53bf450a76d860cbe4ec7e41`):** `pre-m1n` was created with the builder
shut off (atomic, internal disk plus UEFI NVRAM). The root-owned deployment
verified (189 files, no non-root, writable or symlinked entries).
- **Plan:** the complete bundle was `eligible_complete`, the temporary
  directory `eligible_abandoned`, the manual-recovery bundle
  `protected_recovery`, the corrupt bundle `invalid_or_ambiguous`
  (`bundle_verification_failed`), and the sentinel unrecognized. The graph
  was complete and settled.
- **First `gc`:** `complete`. It removed exactly the eligible bundle, its
  attempt cache's payload links and the temporary directory (3814 bytes); the
  durable receipt equals the printed result.
- **Retained:**
  - the protected recovery bundle, which still re-verified exactly while
    `apply` was still refused (`recovery_required`) and `status` still
    showed the manual recovery;
  - its attempt cache, including APT's `lock`;
  - the corrupt bundle and the sentinel;
  - all journals, byte-identical.
- **Unchanged:** the APT cache, dpkg status, `extended_states`, `/etc/apt`,
  the dpkg lock files, and the installed, automatic and held package lists.
- **Second `gc`:** `nothing_to_collect`, with the tree byte-identical.
- **Restore:** after the revert to `pre-m1n` the audit equalled the original
  baseline exactly, all qualification state was gone, and the domain XML
  (disk and NVRAM) was unchanged. The builder ended shut off with autostart
  disabled; `pre-m1n` is kept until CTO closure.

## M1 hardware enablement orchestration gate

`tests/hardware-orchestration.py` (gate 15 of `tests/run-static.sh`, since
M1-O) has 21 checks.

It drives `hardware/hardware_orchestration.py`, `manage-hardware.py` and the
trusted-deployment launcher on the gate-12 fake host (a temporary root). The
**real** apply executor, bounded recovery, validator and GC run against:
- a fake package manager;
- a synthetic runtime collector whose boot identity the gate controls (a
  test-harness "reboot" only changes that identity).

Process death is simulated by an exception nothing can catch, so journals
stay where a SIGKILL leaves them. The gate never touches the runner's
`/var/lib`, `/run`, APT, dpkg, hardware or boot state, needs no root and never
reboots.

It proves:
- **Contract:** the journal schema is Draft 2020-12, meta-valid, closed,
  bounded, free of time and identity fields, and hash-protected (here and in
  gate 10's table).
- **Convergence:** a satisfied plan reaches validation with no APT, dpkg or
  apply attempt (`complete`, exit 0). A second run is identical,
  digest-chained and rewrites nothing. Artifacts are canonical, bound by
  digest and 0700/0600.
- **Lifecycle:**
  1. A missing package goes through the existing apply executor (one APT
     run, attempt and transaction bound).
  2. Same-boot validation ends `reboot_required` with no GC and no reboot.
     The same boot stays `reboot_required`.
  3. After the harness reboot: revalidation against the pre-reboot result
     (continuity checks present), post-reboot PASS, `complete`, then M1-N
     GC (receipt bound).
  4. A third run is idempotent (`nothing_to_collect`).
- **VM:** microcode stays `LIMITED/virtualized_cpu`; the outcome is
  `validation_limited` (exit 2), never `complete`. The contract rejects
  LIMITED-as-complete.
- **FAIL:** a critical driver failure gives `validation_failed` (exit 1), no
  GC and the bundle retained. PASS stays PASS.
- **Precedence:**
  - a same-boot critical FAIL is `validation_failed`, not `reboot_required`:
    - no `awaiting_reboot`, no GC, no reboot;
    - the validator's artifact is byte-exact, with its same-boot evidence
      kept;
    - `status` reports the failure as primary and no pending reboot;
  - once the failure is corrected on the same boot, a fresh run is
    `reboot_required`;
  - a post-reboot FAIL is `validation_failed`;
  - five contradictory journals (`reboot_required` over FAIL,
    `validation_failed` without FAIL, an actionable reboot flag on a failed
    run, `complete` with FAIL, GC after `validation_failed`) and an
    awaiting-reboot continuation derived from a failed run all fail closed.
- **Corrected M1-N integration:** gate 15 uses gate 14's APT ownership
  model. The fake APT run leaves the attempt's `partial/` as `_apt:root`,
  mode `0700`, empty, through the object-lifetime overlay and a root-owned
  fake `/etc/passwd`.
  - M1-N classifies the bundle `eligible_complete`, and would retain it
    without APT's ownership.
  - No GC runs before reboot validation.
  - The post-reboot M1-N GC reclaims the bundle, cache links and
    `partial/`, keeping the journal; the next run is `nothing_to_collect`.
  - A candidate M1-N refuses is retained without affecting the outcome.
- **Fresh planning:** a removed device gives new inventory and plan digests;
  the old plan stays historical. A change between planning and validation
  gives `stale`.
- **Recovery first:**
  - a kill inside APT is recovered (exact dpkg configure) before anything
    else, with no second apply;
  - a no-mutation interruption is abandoned and then freshly replanned and
    applied once;
  - a recovery claim contradicted by its journal gives
    `failed/recovery_unverified`;
  - manual recovery gives exit 9, no apply or GC, and is not re-attempted;
  - a failed recovery is never retried automatically, and continues after
    an operator recovery;
  - unreadable package journals block.
- **Crash/resume:**
  - a crash before apply leads to exactly one apply;
  - a crash after a complete apply is discovered from the apply journal,
    with no second APT run;
  - an external interrupted low-level apply after a finished run is still
    recovered first;
  - an apply "success" without, or contradicted by, its durable journal
    gives `failed/apply_unverified`;
  - an APT failure gives `failed/apply_failed`, with recovery first next
    time;
  - a held package lock gives `package_lock_busy`;
  - drift gives `stale` without APT.
- **Locks:** a second reconcile is busy (exit 8, nothing written). During
  apply both locks are held, in the order orchestration then package; the
  coordinator never takes the package lock itself.
- **Persistent state:** each of the following fails closed with exit 10,
  nothing mutated or rewritten, and `status` reporting the reason:
  - a malformed, schema-invalid or non-canonical journal;
  - an altered or missing artifact;
  - a group- or other-writable run, root or journal;
  - a symlinked root or journal;
  - unexpected entries;
  - an ambiguous sequence;
  - a rewritten predecessor.

  A foreign owner is rejected. Crash debris is preserved. On ENOSPC at
  `applying` the run stops before the apply executor runs.
- **Caller authority:**
  - 17 caller argv forms are rejected (exit 64); non-root exits 77;
  - the operations take only the host;
  - a hostile environment has no effect;
  - the coordinator uses no environ, subprocess, shell, argparse or
    package-manager argv;
  - the launcher runs only the verified entrypoint, with
    `env -i python3 -I -B`.
- **Status:** no lock, no directory, no write (byte-identical root). It
  reports the outcome, apply identity, pending reboot, boot change,
  currentness and recovery need.
- **Privacy:** no raw boot ID, MAC, IP, home path, timestamp, hostname,
  serial or user name in any persisted file.
- **GC:**
  - a GC exception or failed deletion is recorded separately; the hardware
    outcome is kept; exit 6;
  - unresolved package state at GC time skips GC;
  - only `gc.collect_garbage` deletes.
- **Scope:** no reboot, shutdown, systemd unit, timer, socket, D-Bus or
  daemon; NVIDIA stays inactive; no MOK or Secure Boot code.

### M1-O mutation suite

Each mutation is committed in a throwaway clone, and the full
`tests/run-static.sh` must fail. The 34 mutations:
- **Recovery:** skip recovery before apply; continue after manual recovery;
  skip the failed-recovery block; trust an unverified recovery result;
  trust the orchestration phase instead of the package journals.
- **Apply:** trust apply's return without its journal; duplicate the apply
  on resume; omit the apply identity binding.
- **Planning and validation:**
  - reuse a stale plan; validate the planning inventory;
  - reuse the stale validation; omit the validation-result binding;
  - treat LIMITED as PASS; mark a same-boot validation as post-reboot;
  - automatically reboot.
- **GC:** GC before reboot validation; GC after a critical FAIL; GC with
  unresolved package state; report complete after a GC failure; record a GC
  failure as success.
- **Boundaries:**
  - accept a caller package; accept a caller path;
  - inherit a hostile environment; shell execution;
  - skip the orchestration lock;
  - overwrite a corrupt journal; ignore a wrong owner or mode;
  - persist the raw boot ID;
  - a mutating `status`; non-durable journal writes.
- **Precedence and corrected M1-N:** reboot outranks a critical FAIL; a
  failed validation becomes an awaiting-reboot continuation; the actionable
  reboot flag is taken from the raw validation evidence; M1-N regresses to
  the root-owned `partial/` assumption (gate 15 kills this on its own).

The corrected baseline is re-run on the same commit: M1-N correction (17),
M1-N (25), M1-M (43), M1-L (51) and M1-K (46), 182 in total, giving 216 with
M1-O.

### Builder orchestration qualification (M1-O, builder only)

The real end-to-end lifecycle is qualified only on the builder under a fresh
rollback checkpoint `pre-m1o`:
- disk and UEFI NVRAM;
- the builder shut off;
- created with an individual Polkit approval after read-only checks.

The restored baseline lacks `intel-microcode` while the plan requires it.
From the root-owned exact-commit deployment:
1. `run-hwe-status`: no orchestration state.
2. `run-hwe-reconcile`: recovery check, fresh probe and plan, the existing
   apply executor installs exactly the qualified closure, then validation.
   The coordinator does not reboot (the boot ID is unchanged).
3. The harness reboots the VM externally. That reboot is test behaviour, not
   product behaviour.
4. `run-hwe-reconcile`: the boot change is proven, there is revalidation
   with continuity, no second apply, and VM microcode stays LIMITED.
5. `run-hwe-reconcile` again: idempotence. The post-reboot run (step 4) is
   expected to reclaim the real bundle through the corrected M1-N, despite
   APT's real `_apt:root 0700` `partial/`; the third run is then
   `nothing_to_collect`.
6. Package, source, mark and hold diffs, plus orchestration state, are
   captured as evidence.
7. The VM is reverted to `pre-m1o`, booted and audited against the baseline,
   then shut down. The checkpoint is kept until publication closure.

**M1-O qualification (rebuilt candidate).**
- **Implementation:** `4fc443d0540be51028f24e5569fbaaa5fdf60d97`, on the
  corrected M1-N closure `8b77e24`. The earlier unpublished candidate
  `a27c392` is kept only as local historical evidence and was not reused as
  acceptance evidence.
- **CI:** `Static gates` run #40 ([38980191190](https://github.com/lubarchy/lubarchy/actions/runs/38980191190)) on the
  exact SHA passed 15/15 gates (gate 10: 19, gate 11: 14, gate 12: 15,
  gate 13: 13, gate 14: 10, gate 15: 21 checks).
- **Exact-commit qualification:** aggregate exit 0. 216/216 mutations were
  killed: M1-O 34/34, plus the corrected baseline 182/182 (M1-N correction
  17, M1-N 25, M1-M 43, M1-L 51, M1-K 46). There were 0 survivors, 0 harness
  errors and 0 bad baselines.
- **Validation precedence:** `validation_failed` > `reboot_required` >
  `validation_limited` > `complete`. It is qualified by gate 15 and
  mutations O31, O32 and O34, because the builder was deliberately not
  damaged to produce a real critical FAIL.
- **Builder:**
  1. Run 1: one apply (`intel-microcode` direct, `iucode-tool` dependency)
     ended `reboot_required`. The product did not reboot (same boot-ID
     digest).
  2. The harness rebooted the builder externally.
  3. Run 2: revalidation against the pre-reboot result (continuity checks
     PASS), no duplicate apply or recovery, `validation_limited`. The
     corrected M1-N GC reclaimed the real bundle (12,905,139 bytes; payload
     links and the APT-owned `_apt:root 0700` `partial/` removed; APT's
     `lock` and the journal kept).
  4. Run 3: idempotent, `nothing_to_collect`.
  5. VM microcode was `LIMITED/virtualized_cpu` in every run. Packages,
     marks, holds, sources and APT history did not drift after the apply.
  6. The restore to `pre-m1o` was exact. `pre-m1o` is retained only for
     post-closure cleanup.

The qualification docs `03be7dc` passed `Static gates` run #41
([38980651256](https://github.com/lubarchy/lubarchy/actions/runs/38980651256)), 15/15 gates. M1-O is **PASS (CTO accepted)
/ CLOSED**; M1-P has not started.

## GitHub CI

The static gates run as the GitHub Actions workflow
`.github/workflows/m0-static.yml` (shown as `M0 static gates` through M0-J and
renamed `Static gates` in M1-A; the file name is unchanged): pinned
`actions/checkout` commit, pinned `debian:trixie` image digest,
`contents: read`, no secrets. It installs `git`, `ca-certificates`,
`shellcheck`, `live-build`, `python3` and `python3-jsonschema` (the last two
since M1-A) from Debian `main` only and runs `tests/run-static.sh`. Gate 10
also uses `dpkg-deb` from the image's essential `dpkg` package, read-only, on
synthetic payloads. **It is static/configuration CI only: it never builds
or boots an ISO and never probes hardware.**

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
| [#11 (38294607100)](https://github.com/lubarchy/lubarchy/actions/runs/38294607100) | `c15a0a50f6e0b66d2a4ca163a7e77add3f6107d8` | push | **success**: all six gates passed (documentation commit) |
| [#12 (38321463299)](https://github.com/lubarchy/lubarchy/actions/runs/38321463299) | `4917b47948f5b3ab19a292a510a979f674e3c654` | push | **success**: repository lint, ShellCheck (9 scripts, 0 findings), live-build configuration, hardware contracts (29 checks), hardware probe (35 checks) and hardware resolver (24 checks, including the package/source-policy checks; 96 parity cases) passed; CI container sources `main` only |
| [#13 (38321640718)](https://github.com/lubarchy/lubarchy/actions/runs/38321640718) | `93d18659a6a1591748871d7c4304329ddb37bb23` | push | **success**: all six gates passed (documentation commit) |
| [#14 (38362588631)](https://github.com/lubarchy/lubarchy/actions/runs/38362588631) | `b316042a960683fbf44f2279de0754db1b731abb` | push | **success**: repository lint, ShellCheck (9 scripts, 0 findings), live-build configuration, hardware contracts (29 checks), hardware probe (35 checks), hardware resolver (24 checks) and hardware firmware (12 checks; 1,722 committed aliases mapped, no cross-package overlap) passed; CI container sources `main` only |
| [#15 (38363375181)](https://github.com/lubarchy/lubarchy/actions/runs/38363375181) | `d86458882de6ae44353d4a860c79f31c2bf6cc76` | push | **success**: all seven gates passed (documentation commit) |
| [#16 (38372395051)](https://github.com/lubarchy/lubarchy/actions/runs/38372395051) | `d56d6eb4c5a61ab0e10f37b23635cf3a8fa8485c` | push | **success**: repository lint, ShellCheck (9 scripts, 0 findings), live-build configuration, hardware contracts (29 checks), hardware probe (35 checks), hardware resolver (24 checks) and hardware firmware (13 checks; 2,517 committed aliases mapped, M1-E rules regenerated from v1 evidence, attribution and collision checks clean) passed; CI container sources `main` only |
| [#17 (38372865767)](https://github.com/lubarchy/lubarchy/actions/runs/38372865767) | `6798524aaf0f650710c7408ba7e07e98803c29ad` | push | **success**: all seven gates passed (documentation commit) |
| [#18 (38437605209)](https://github.com/lubarchy/lubarchy/actions/runs/38437605209) | `9beb35d211f5def6b5642d5bb31ef830bf1f6ae4` | push | **success**: repository lint, ShellCheck (9 scripts, 0 findings), live-build configuration, hardware contracts (29 checks), hardware probe (35 checks), hardware resolver (24 checks), hardware firmware (13 checks) and hardware NVIDIA strategy (14 checks; committed evidence only, no NVIDIA repository contacted or package installed) passed; CI container sources `main` only |
| [#19 (38437920398)](https://github.com/lubarchy/lubarchy/actions/runs/38437920398) | `fe75229fb7503083b2bef5dbb82fd42ba847b2c2` | push | **success**: all eight gates passed (documentation commit) |
| [#20 (38474658686)](https://github.com/lubarchy/lubarchy/actions/runs/38474658686) | `cd8b43fc329a8093c43e97d388689f4386dc3c34` | push | **success**: repository lint, ShellCheck (9 scripts, 0 findings), live-build configuration, hardware contracts (29 checks), hardware probe (35 checks), hardware resolver (24 checks), hardware firmware (13 checks) and hardware NVIDIA strategy (19 checks; M1-G evidence and M1-H containment policy from committed fixtures only, no NVIDIA repository contacted or package installed) passed; CI container sources `main` only |
| [#21 (38474917621)](https://github.com/lubarchy/lubarchy/actions/runs/38474917621) | `72ccc8bdb5c7386b282f82b616cf8ed10e7d5b4a` | push | **success**: all eight gates passed (documentation commit) |
| [#22 (38519747096)](https://github.com/lubarchy/lubarchy/actions/runs/38519747096) | `3378afb53bd96644f740e79fb23a2a30f88c874c` | push | **success**: repository lint, ShellCheck (9 scripts, 0 findings), live-build configuration, hardware contracts (29 checks), hardware probe (35 checks), hardware resolver (24 checks), hardware firmware (13 checks), hardware NVIDIA strategy (19 checks) and hardware package transaction (17 checks; committed synthetic fixtures, APT samples and golden transactions only; no runner APT state, no package installed) passed; CI container sources `main` only |
| [#23 (38520046728)](https://github.com/lubarchy/lubarchy/actions/runs/38520046728) | `f778ee031364d0458b413efac643a1e03b4070e7` | push | **success**: all nine gates passed (documentation commit) |
| [#24 (38539169890)](https://github.com/lubarchy/lubarchy/actions/runs/38539169890) | `0fab8f899a356685fc6b33117953e333e30742ac` | push | **success**: repository lint, ShellCheck (9 scripts, 0 findings), live-build configuration, hardware contracts (29 checks), hardware probe (35 checks), hardware resolver (24 checks), hardware firmware (13 checks), hardware NVIDIA strategy (19 checks), hardware package transaction (17 checks) and hardware package staging (19 checks; synthetic and offline: synthetic source trees and payloads, read-only `dpkg-deb --field`, no `apt-get`, no download, no source activation) passed; CI container sources `main` only |
| [#25 (38539426185)](https://github.com/lubarchy/lubarchy/actions/runs/38539426185) | `bf145e68530f291f8076ba9660d7924685494e01` | push | **success**: all ten gates passed (documentation commit) |
| [#26 (38560415459)](https://github.com/lubarchy/lubarchy/actions/runs/38560415459) | `bee741485ce2fb6db35d433b8c0102bc59824e9f` | push | **success**: repository lint, ShellCheck (9 scripts, 0 findings), live-build configuration, hardware contracts (29 checks), hardware probe (35 checks), hardware resolver (24 checks), hardware firmware (13 checks), hardware NVIDIA strategy (19 checks), hardware package transaction (17 checks), hardware package staging (19 checks) and hardware package apply (14 checks; fake host and synthetic trees only: no real install, source activation, download, `/var/lib/lubarchy` or `/root/lubarchy-m1k` use, reboot or libvirt) passed; CI container sources `main` only |
| [#27 (38560784304)](https://github.com/lubarchy/lubarchy/actions/runs/38560784304) | `b0480cc43932ae0e873809aaf6ed362730f4fdd7` | push | **success**: all eleven gates passed (documentation commit) |
| [#28 (38666440151)](https://github.com/lubarchy/lubarchy/actions/runs/38666440151) | `3b0ec8869a3034c7df38c25c004387bf99750d7b` | push | **success**: repository lint, ShellCheck, live-build configuration, hardware contracts (29 checks), hardware probe (35 checks), hardware resolver (24 checks), hardware firmware (13 checks), hardware NVIDIA strategy (19 checks), hardware package transaction (17 checks), hardware package staging (19 checks), hardware package apply (14 checks) and hardware package recovery (15 checks; fake host and synthetic dpkg logs only: no real apply or recovery, no runner APT/dpkg mutation by LUBARCHY code, no `/etc/apt` or `/var/lib/lubarchy` write, no reboot, no libvirt, no NVIDIA contact) passed; CI container sources `main` only |
| [#29 (38666799577)](https://github.com/lubarchy/lubarchy/actions/runs/38666799577) | `2610a1db5f1ec49ffe99e19eedad07bbeea5b592` | push | **success**: all twelve gates passed (documentation commit) |
| [#30 (38732417272)](https://github.com/lubarchy/lubarchy/actions/runs/38732417272) | `59793b6dcdaddf73946dcdd9a6681ac58b2fc644` | push | **success**: repository lint, ShellCheck, live-build configuration, hardware contracts (29 checks), hardware probe (35 checks), hardware resolver (24 checks), hardware firmware (13 checks), hardware NVIDIA strategy (19 checks), hardware package transaction (17 checks), hardware package staging (19 checks), hardware package apply (14 checks), hardware package recovery (15 checks) and hardware post-install validation (13 checks; synthetic inventories, synthetic runtime evidence and a fake sysfs/procfs tree only: the runner's hardware, kernel log, packages and services were never read, nothing installed, activated, written, loaded or bound, no reboot, no libvirt, no NVIDIA contact, no physical-hardware claim) passed; CI container sources `main` only |
| [#31 (38732989639)](https://github.com/lubarchy/lubarchy/actions/runs/38732989639) | `a40f6a9ebeb6506f1acda596a9a6e3074acaecdb` | push | **success**: all thirteen gates passed (documentation commit) |
| [#32 (38734298541)](https://github.com/lubarchy/lubarchy/actions/runs/38734298541) | `66393f5ef2bb87fcaaf7321581b0509159d08816` | push | **success**: all thirteen gates passed (M1-M closure documentation commit) |
| [#33 (38816122990)](https://github.com/lubarchy/lubarchy/actions/runs/38816122990) | `1eec088956d73cdc53bf450a76d860cbe4ec7e41` | push | **success**: repository lint, ShellCheck, live-build configuration, hardware contracts (29 checks), hardware probe (35 checks), hardware resolver (24 checks), hardware firmware (13 checks), hardware NVIDIA strategy (19 checks), hardware package transaction (17 checks), hardware package staging (19 checks), hardware package apply (14 checks), hardware package recovery (15 checks), hardware post-install validation (13 checks) and hardware package state garbage collection (8 checks; fake-host temporary roots only: the runner's `/var/lib/lubarchy`, APT cache, dpkg state and `/etc/apt` were never touched, no apply, recovery or GC against the runner, no deletion of runner files, no libvirt, no reboot, no root needed) passed; CI container sources `main` only |

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

**M1-D CI qualification:** the M1-D change (package/source-policy tests
within gate 6; no new dependency) was executed on GitHub by workflow
`Static gates`, run ID
[38321463299](https://github.com/lubarchy/lubarchy/actions/runs/38321463299)
(run #12), on commit `4917b47948f5b3ab19a292a510a979f674e3c654`. All six
gates ran and passed: repository lint, ShellCheck, live-build configuration,
hardware contracts, hardware probe and hardware resolver. The CI container's
own APT sources stayed Debian `main` only.

**M1-E CI qualification:** the M1-E change (gate 7, hardware firmware,
synthetic and committed evidence only; `tools/**` added to the workflow path
filter; no new dependency) was executed on GitHub by workflow `Static gates`,
run ID
[38362588631](https://github.com/lubarchy/lubarchy/actions/runs/38362588631)
(run #14), on commit `b316042a960683fbf44f2279de0754db1b731abb`. All seven
gates ran and passed: repository lint, ShellCheck, live-build configuration,
hardware contracts, hardware probe, hardware resolver and hardware firmware.
CI read only the committed alias evidence, never the runner's kernel modules.

**M1-F CI qualification:** the M1-F change (strengthened gate 7, v2
evidence, five new firmware rules; no new dependency) was executed on GitHub
by workflow `Static gates`, run ID
[38372395051](https://github.com/lubarchy/lubarchy/actions/runs/38372395051)
(run #16), on commit `d56d6eb4c5a61ab0e10f37b23635cf3a8fa8485c`. All seven
gates ran and passed: repository lint, ShellCheck, live-build configuration,
hardware contracts, hardware probe, hardware resolver and hardware firmware.
CI used only committed evidence and synthetic fixtures; it downloaded no
package and never read the runner's kernel modules or hardware.

**M1-G CI qualification:** the M1-G change (gate 8, hardware NVIDIA
strategy; NVIDIA evidence and normalized supported-GPU snapshot; ADR-0002
Proposed; no new dependency) was executed on GitHub by workflow
`Static gates`, run ID
[38437605209](https://github.com/lubarchy/lubarchy/actions/runs/38437605209)
(run #18), on implementation commit
`9beb35d211f5def6b5642d5bb31ef830bf1f6ae4`. All eight gates ran and passed:
repository lint, ShellCheck, live-build configuration, hardware contracts,
hardware probe, hardware resolver, hardware firmware and hardware NVIDIA
strategy. CI used only committed evidence and synthetic fixtures. It fetched
packages only from Debian `main`, contacted no NVIDIA repository, installed
no NVIDIA package and probed no runner hardware. M1-G was subsequently
accepted by the CTO (PASS).

**M1-H CI qualification:** the M1-H change (inactive NVIDIA repository
containment policy, rendered preferences, authenticated APT fixtures,
`tools/hwe/validate-nvidia-apt-policy.py`; gate 8 extended to 19 checks; no
new dependency) was executed on GitHub by workflow `Static gates`, run ID
[38474658686](https://github.com/lubarchy/lubarchy/actions/runs/38474658686)
(run #20), on implementation commit
`cd8b43fc329a8093c43e97d388689f4386dc3c34`. All eight gates ran and passed.
CI tested the NVIDIA policy only against committed fixtures and evidence. It
never contacted the NVIDIA repository, used no live NVIDIA metadata,
installed no NVIDIA, CUDA or DKMS package, and kept its Debian sources
`main` only. Containment recommendation: YES — NVIDIA's Debian 13 repository
can be technically contained as a candidate D-005 exception. M1-H was
subsequently accepted by the CTO (PASS); ADR-0002 remains Proposed and the
repository remains unauthorised.

**M1-I CI qualification:** the M1-I change (package transaction contract
`lubarchy.hardware.package-transaction/v1`, compiler and read-only CLI,
`tools/hwe/qualify-transaction.py`; new gate 9 with 17 checks; no new
dependency) was executed on GitHub by workflow `Static gates`, run ID
[38519747096](https://github.com/lubarchy/lubarchy/actions/runs/38519747096)
(run #22), on implementation commit
`3378afb53bd96644f740e79fb23a2a30f88c874c`. All nine gates ran and passed.
Gate 9 used only the committed synthetic universe, APT output samples and
golden transactions. CI never compiled a transaction against the runner's
APT state, never probed runner hardware, never contacted NVIDIA, and fetched
Debian packages only in the existing QA dependency step (`main` only). No
package was installed or changed by the transaction tests, and no apt/dpkg
mutation exists. In CI's shallow checkout, the gate-9 check that inventory,
rule and plan v1 are unchanged falls back to the schema identity checks of
gates 4–6; locally they are byte-identical to `72ccc8b`. Documentation run
#23 passed all nine gates on `f778ee0`. M1-I was subsequently accepted by the
CTO (PASS). No privileged package mutation is implemented. Since M1-J, gate
10 also checks those protected schemas against a committed hash baseline,
which needs no Git history.

**M1-J CI qualification:** the M1-J change was executed on GitHub by workflow
`Static gates`, run ID
[38539169890](https://github.com/lubarchy/lubarchy/actions/runs/38539169890)
(run #24), on implementation commit
`0fab8f899a356685fc6b33117953e333e30742ac`. The change comprises:
- source-activation and staged-bundle contracts;
- `hardware/source_activation.py` and `hardware/package_staging.py` with
  their CLIs;
- `tools/hwe/qualify-staging.py`;
- new gate 10 with 19 checks.

All ten gates ran and passed. Gate 10 is synthetic and offline:
- synthetic source trees, the committed builder `sources.list` fixture and
  the gate-9 universe;
- synthetic in-memory payloads;
- `dpkg-deb --field` read-only.

CI never ran `apt-get`, never downloaded a `.deb`, never contacted NVIDIA,
never read the runner's `/etc/apt` or hardware, never activated a source and
never installed a package. Packages were installed only in the existing QA
dependency step. The package names are identical to run #22; the mirror's
`libcurl3t64-gnutls` moved from `deb13u5` to `deb13u7`, and the workflow
itself changed only in its header comment. M1-J remains awaiting CTO review
(not PASS). No system source or package mutation was performed, staging
remains pre-apply only, and M1-K has not started.

**M1-K CI qualification:** the M1-K change was executed on GitHub by workflow
`Static gates`, run ID
[38560415459](https://github.com/lubarchy/lubarchy/actions/runs/38560415459)
(run #26), on implementation commit
`bee741485ce2fb6db35d433b8c0102bc59824e9f`. The change comprises:
- the privileged apply executor and its root-only CLI;
- the apply-journal schema;
- the trusted qualification deployment tool;
- new gate 11 with 14 checks.

All eleven gates ran and passed; gate 10 stayed at 19 checks. CI running as
root does not authorise mutation, and none occurred:
- gate 11 drove the executor through its fake host and synthetic trees in a
  temporary directory;
- CLI usage probes run in-process with the real host replaced by a sentinel;
- no package was installed or downloaded, no source was activated, and
  neither `/var/lib/lubarchy` nor `/root/lubarchy-m1k` was used;
- there was no reboot, no libvirt use and no NVIDIA contact;
- the QA dependency set matches run #24.

The real mutation was qualified only on the rollback-protected builder VM
(see the builder apply qualification above), which was restored to its
original baseline. M1-K remains awaiting CTO review (not PASS), and M1-L
has not started.

**M1-L CI qualification:** the M1-L change was executed on GitHub by workflow
`Static gates`, run ID
[38666440151](https://github.com/lubarchy/lubarchy/actions/runs/38666440151)
(run #28), on implementation commit
`3b0ec8869a3034c7df38c25c004387bf99750d7b`. The change comprises:
- the recovery library and the root-only `recover` operation;
- the recovery-journal schema and the apply journal's pre-apply recovery
  evidence;
- the `run-recover` launcher of the trusted deployment tool;
- new gate 12 with 15 checks.

All twelve gates ran and passed; gate 10 stayed at 19 checks and gate 11 at
14. CI running as root does not authorise mutation, and none occurred:
- gate 12 drove recovery through its fake host, fake dpkg and synthetic
  dpkg logs in a temporary directory;
- no real apply or recovery ran against the runner, no LUBARCHY code
  touched the runner's APT/dpkg state, `/etc/apt` or `/var/lib/lubarchy`;
- no reboot, no libvirt and no NVIDIA contact. The only package-manager
  activity was the workflow's own installation of the QA tools from Debian
  `main` in the disposable container.

The final recovery architecture recorded by this qualification:
- the original transaction closure stays separate from the recovery
  impact set, and trigger impact must be attributed to the exact
  transaction from evidence bound before `applying`;
- the configure phase uses `--no-triggers`, and trigger execution is a
  separately authorised `--triggers-only` phase;
- recovery never broadens its authorisation set; unrelated or newly
  induced trigger work fails closed to manual recovery;
- there is no generic APT repair and no unrestricted
  `dpkg --configure -a`;
- the journals are evidence, not a rollback, and the VM snapshot protects
  only the qualification environment.

The real recovery was qualified only on the rollback-protected builder VM
(see the builder recovery qualification above), which was restored to its
original baseline. M1-L remains awaiting CTO review (not PASS), and M1-M
has not started.

**M1-M CI qualification:** the M1-M change was executed on GitHub by workflow
`Static gates`, run ID
[38732417272](https://github.com/lubarchy/lubarchy/actions/runs/38732417272)
(run #30), on implementation commit
`59793b6dcdaddf73946dcdd9a6681ac58b2fc644`. The change comprises:
- the read-only validator library and CLI;
- the `lubarchy.hardware.validation-result/v1` schema (protected);
- new gate 13 with 13 checks.

All thirteen gates ran and passed; gate 10 stayed at 19, gate 11 at 14 and
gate 12 at 15. CI was synthetic and non-mutating:
- gate 13 used synthetic inventories, synthetic normalized runtime evidence
  and a fake sysfs/procfs tree with a fake command runner;
- it never read the runner's hardware, kernel log, packages or services;
- nothing was installed, activated, written to `/etc` or
  `/var/lib/lubarchy`, loaded, bound or unbound;
- no network device was touched, there was no reboot, no libvirt, no
  `dmesg` permission change and no NVIDIA contact;
- no physical-hardware claim was made.

The workflow change was the header comment only. The only package-manager
activity was the workflow's own installation of the QA tools in the
disposable container.

The local exact-commit qualification of the same commit had passed before
publication:
- the aggregate driver exited 0;
- 13/13 gates;
- mutations: M1-M 43/43, M1-L 51/51 and M1-K 46/46 killed, with zero
  survivors and zero harness errors;
- the read-only builder qualification (no M1-M snapshot; the reboot
  freshness test passed; builder shut off afterwards).

The current builder validation result correctly remains `FAIL` because
`intel-microcode` is absent from the restored baseline. VM microcode stays
`LIMITED`, and no physical Wi-Fi, Bluetooth or GPU function is claimed.

Firmware policy at publication:
- the canonical firmware-family mapping remains
  `tools/hwe/firmware-mappings.json`; the validator's module lists are a
  derived copy that gate 13 enforces semantically equal (drift fails);
- the production firmware requirement policy is empty and no requiredness
  was guessed: attributed unknown and optional firmware failures are
  `LIMITED`, and only evidence-backed known-required failures are `FAIL`;
- independently observable device, driver and interface failures remain
  `FAIL`.

At publication M1-M was awaiting CTO review; it was subsequently accepted
(PASS, CTO accepted). The PASS qualifies the validation subsystem; the
builder's own validation result remains `FAIL` as recorded above. M1-N has
not started.

**M1-N CI qualification:** the M1-N change was executed on GitHub by workflow
`Static gates`, run ID
[38816122990](https://github.com/lubarchy/lubarchy/actions/runs/38816122990)
(run #33), on implementation commit
`1eec088956d73cdc53bf450a76d860cbe4ec7e41`. The change comprises:
- the package-state GC library and the root-only `gc-plan`/`gc`
  operations;
- the trusted-deployment `run-gc-plan`/`run-gc` launchers;
- the protected `gc-plan-v1` and `gc-receipt-v1` schemas;
- new gate 14 with 8 checks.

All fourteen gates ran and passed: gate 10 stayed at 19, gate 11 at 14,
gate 12 at 15 and gate 13 at 13; gate 14 had 8. Gate 14 operated only on
fake-host temporary roots, and never on the runner's `/var/lib/lubarchy`,
APT cache, dpkg state or `/etc/apt`:
- no apply, recovery or GC ran against the runner;
- no runner file was deleted;
- there was no libvirt, no reboot and no root.

The workflow change was the header comment; gate 14 runs through the
existing static runner.

**Exact-commit qualification.** The first local run exited 1 only because
two older mutation targets (L14, K02) still matched the pre-M1-N CLI
argument check. That was a harness defect, not a product failure. After
retargeting them (harness only, commit unchanged), the exact-commit run had:
- 14/14 gates;
- mutations M1-N 25/25, M1-M 43/43, M1-L 51/51 and M1-K 46/46 (165/165)
  killed;
- zero survivors and zero harness errors;
- aggregate exit 0.

Both records are kept in the external evidence. The real builder GC
qualification is recorded in the builder GC qualification above.

**Scope of the qualification.** It qualifies the GC subsystem only; it
does not authorise generic cleanup. Journals, receipts, unknown, corrupt,
ambiguous or manual-recovery state, the APT cache in general, dpkg state,
installed packages and APT sources are never GC targets.

At publication M1-N was awaiting CTO review; it was subsequently accepted
(PASS, CTO accepted). The PASS qualifies the package-state GC subsystem
only. M1-O had not started at that point.

M1-N was later reopened for the APT sandbox `partial/` correction and
re-closed: **PASS (CTO re-accepted) / CLOSED**. The chain is:
- product correction `dfd82cb`; run #36
  ([38888081993](https://github.com/lubarchy/lubarchy/actions/runs/38888081993)) failed gate 14 because of the synthetic
  inode-reuse overlay defect, a harness failure and not a product failure;
- test-only harness fix `9e7ad19`; run #37
  ([38896919894](https://github.com/lubarchy/lubarchy/actions/runs/38896919894)) passed 14/14 gates;
- qualification docs `cfd6d0a`; run #38
  ([38897405669](https://github.com/lubarchy/lubarchy/actions/runs/38897405669)) passed 14/14 gates, gate 14 at 10 checks.

Exact-commit qualification killed 182/182 mutations. The real builder
evidence (for `dfd82cb`, whose `package_gc.py` is byte-identical in later
commits):
- an APT-created `_apt:root` `0700` `partial/`;
- 12,905,139 bytes reclaimed;
- an idempotent second GC;
- an exact restore.

The APT sandbox is not weakened. M1-O remains on publication hold, and M1-P
has not started.

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
