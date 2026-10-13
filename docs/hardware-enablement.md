# Hardware Enablement

Requirements for LUBARCHY hardware enablement. Hardware enablement is a
first-class subsystem (D-008).

**Status (M1-G):** the data and policy contracts — inventory v1, rule v1 and
plan v1 — are defined and tested (M1-A). A read-only live probe,
`hardware/probe.py`, turns a running system into an inventory v1 document
(M1-B, see [Live probe](#live-probe-m1-b)). A production resolver turns that
inventory and a conservative production rule set into a plan v1 (M1-C, see
[Production resolver](#production-resolver-m1-c)). Debian `non-free-firmware`
is an accepted hardware-enablement source and the first package mappings —
Intel and AMD CPU microcode — appear in plans (M1-D, see
[Package source policy](#package-source-policy-m1-d)). Four device firmware
families are mapped from qualified kernel module aliases (M1-E, see
[Device firmware mappings](#device-firmware-mappings-m1-e)); second-wave
network and Bluetooth families follow under a stronger firmware-file
evidence chain (M1-F, see
[Second-wave firmware mappings](#second-wave-firmware-mappings-m1-f)).
The NVIDIA driver strategy is qualified as architecture evidence only, in
the Proposed ADR-0002 (M1-G, see
[NVIDIA driver strategy evidence](#nvidia-driver-strategy-evidence-m1-g)).
**Packages in a plan are proposals only: nothing installs, enables a source
or configures anything yet.** Further firmware families, installation,
configuration and validation are later M1 work.

## Goal

A normal beginner should not need terminal work for ordinary hardware setup.
Wi-Fi, Bluetooth, graphics, audio, firmware, displays, input devices, and
power management should work automatically where practical.

## Pipeline

probe → normalized inventory → compatibility classification →
package/profile resolution → configuration → validation → persisted
diagnostic result

## Normalized inventory targets

The inventory should identify, in a stable machine-readable form:

- CPU vendor/model and microcode needs;
- GPU devices, drivers, and hybrid topology;
- network controllers (Ethernet, Wi-Fi);
- Bluetooth controllers;
- audio devices;
- storage controllers (NVMe, SATA);
- input devices;
- power and battery capabilities;
- displays and scaling characteristics;
- firmware requirements.

Inventory v1 (below) covers the CPU, the platform, and PCI and USB devices.
The other targets (audio, input, power, displays, firmware requirements) are
reached through those devices or need later schema versions.

## M1-A contracts

| Contract | Schema | Identifier |
| --- | --- | --- |
| Inventory | [`hardware/schemas/inventory-v1.schema.json`](../hardware/schemas/inventory-v1.schema.json) | `lubarchy.hardware.inventory/v1` |
| Rule | [`hardware/schemas/rule-v1.schema.json`](../hardware/schemas/rule-v1.schema.json) | `lubarchy.hardware.rule/v1` |
| Plan | [`hardware/schemas/plan-v1.schema.json`](../hardware/schemas/plan-v1.schema.json) | `lubarchy.hardware.plan/v1` |

All three are JSON Schema Draft 2020-12 documents with a fixed `$id`
(`urn:lubarchy:hardware:<kind>:v1`). Each document names its contract in a
`schema` field that must equal the identifier exactly; there are no floating
versions. Every declared object rejects undeclared fields, so a schema grows
only by a new version, never by ad-hoc fields. Schemas are versioned
independently of any code.

The executable reference for the semantics below is
[`tests/hwe_reference.py`](../tests/hwe_reference.py), exercised by the static
gate `tests/hardware-contracts.py` (see [testing.md](testing.md)). It is test
tooling, not the production component.

### Observation, policy and plan are separate

1. **Inventory** — observed facts only, as Linux reports them. It never
   contains a package, profile, source, requirement or time.
2. **Rules** — declarative policy that interprets those facts.
3. **Plan** — the proposal that results from applying the rules to one
   inventory.

Nothing turns an observation directly into a side effect. A plan is not an
installation transaction: it contains no commands and performs nothing.

### Observation sources

In priority order: `/sys` and udev device data; kernel modalias and the bound
driver/module; PCI numeric IDs; USB numeric IDs; purpose-built utilities only
as enrichment or diagnostics. Human-readable names are informational and are
never used for matching. The M1-B probe implements the first four; it uses
no enrichment utility.

### Inventory v1

| Section | Fields |
| --- | --- |
| Top level | `schema`, `system`, `cpu`, `devices` |
| `system` | `architecture` (Debian architecture, `amd64`), `kernel_release`, `boot_firmware` (`uefi`/`bios`, absent if unknown), `secure_boot` (`enabled`/`disabled`/`unknown`, required), `virtualization` (`kind`: `none`/`vm`/`container`, optional `technology` such as `kvm`; absent if unknown) |
| `cpu` | `architecture` (`x86_64`), `vendor` (CPUID vendor string, e.g. `GenuineIntel`), `family`, `model`, `stepping` (integers), `model_name` (informational), `microcode_revision` (observed, e.g. `0x1f`) |
| PCI device | `id`, `bus` (`pci`), `parent`, `locator` (`dddd:bb:dd.f`), `vendor_id`, `device_id`, `subsystem_vendor_id`, `subsystem_device_id`, `class_id`, `modalias`, `driver`, `modules` |
| USB device | `id`, `bus` (`usb`), `parent`, `locator` (`<bus>-<port>[.<port>...]`), `vendor_id`, `product_id`, `device_class`, `device_subclass`, `device_protocol`, `driver`, `interfaces` |
| USB interface | `number`, `class`, `subclass`, `protocol`, `modalias`, `driver`, `modules` |

Normalization:

- Hexadecimal IDs are lowercase and fixed-width: PCI vendor, device and
  subsystem IDs and USB vendor/product IDs have 4 digits, the PCI class code
  6, USB class/subclass/protocol and interface numbers 2. The schema rejects
  anything else (`10DE`, `0x10de`, `de`, `unknown`).
- The `modalias` string is the one exception: it is kept byte-for-byte as the
  kernel writes it (uppercase hexadecimal), because kernel module aliases are
  matched against that exact form.
- Missing data is an absent field. Zero IDs are never invented, and the word
  `unknown` is used only where the schema defines it (`secure_boot`).
- Microcode requirements are policy and never appear in the inventory; only
  the observed revision does.
- Canonical order: devices ascending by `id`, USB interfaces by `number`,
  module lists sorted. Producers should emit this order; consumers normalize
  before computing a digest or resolving.

### Snapshot locators are not hardware identity

A device `id` is `<bus>:<locator>` (for example `pci:0000:01:00.0` or
`usb:1-10`). It is unique within one inventory and lets `parent` express the
topology (a GPU behind a bridge, a USB device behind its host controller). It
is a topology position, not a stable identity across boots or machines.
Selectors cannot name ids or locators: matching uses PCI/USB IDs, class
codes, modaliases, CPU vendor/family/model and platform facts only.

### Privacy boundary

The default inventory has no field for, and rejects, serial numbers (system,
chassis, board, disk, NVMe), product UUIDs, WWNs, MAC and Bluetooth
addresses, machine-id, hostname, username, IP addresses, Wi-Fi SSIDs, or
secrets and credentials. There is no generic property map, so an unrestricted
udev/sysfs property dump cannot be smuggled in. Free text is limited to the
CPU model name. A private diagnostic mode, if one is ever needed, requires a
separate design.

### Rule v1

A rule is one JSON document, stored as `<id>.json`:

- `schema`, `id` (globally unique, e.g. `gpu.nvidia.example`),
  `description`, integer `priority` (-1000…1000), `match` (one or more
  selectors) and `effects`.
- Rules are data. No field holds a command, shell fragment or code, and no
  field is ever executed; unknown fields are rejected.

Matching:

- A rule matches if **any** selector matches.
- A selector has exactly one scope: `pci`, `usb`, `cpu` or `platform`.
- Within one selector every supplied field must match (**AND**). Each field
  takes a list of allowed values, any of which may match (**OR**).
- An absent observed field never satisfies a condition, not even a `*` glob
  or `virtualization_kind: ["none"]`.
- `pci`: `vendor_id`, `device_id`, `subsystem_vendor_id`,
  `subsystem_device_id`, `class_id` (2, 4 or 6 digits match the base class,
  base class + subclass, or the full code) and `modalias`.
- `usb`: `vendor_id`, `product_id`, `device_class`, `device_subclass`,
  `device_protocol`, and `interface_class`, `interface_subclass`,
  `interface_protocol` and `modalias`. All interface conditions must hold on
  the **same** interface.
- `cpu`: `architecture`, `vendor`, `family`, `model`.
- `platform`: `architecture`, `boot_firmware`, `virtualization_kind`,
  `virtualization_technology`.
- `modalias` patterns are shell-style globs matched against the whole string,
  case-sensitively: `*`, `?`, `[...]` and `[!...]`. There are no regular
  expressions.

Each matching rule applies to the targets it matched: `cpu`, `platform` or
device ids.

Effects (all declarative):

- `classifications` — hardware classes such as `gpu.nvidia` or
  `network.wifi`.
- `packages` — `{name, disposition, source, reason}`. `disposition` is
  `require` (with the `source` it needs) or `forbid` (no source).
- `profiles` — `{name, reason, excludes}`. Profiles are symbolic labels;
  their contents are future work. `excludes` names profiles that must not be
  required at the same time.
- `warnings` — `{code, message}`.

Priority and conflicts:

- Rules are additive. Evaluation and reporting order is priority descending,
  then rule id ascending. **Priority never overrides another rule.**
- Requirements that agree (same package, same source) merge, keeping every
  rule's reason.
- These are conflicts: a package that is both required and forbidden; a
  package required from different sources; a required profile excluded by
  another required profile. Any conflict makes the result `conflict`, and the
  plan then proposes no package, profile or source. There is no
  last-rule-wins.
- A rule set with duplicate rule ids, a rule file not named after its id, or
  any schema-invalid rule is rejected as a whole.

Known v1 limitation: a selector matches one target, so a rule cannot require
two different devices at once. A hybrid-graphics machine is therefore
represented in the inventory (two display controllers and their topology) and
receives both GPU classifications, but a cross-device `gpu.hybrid`
classification cannot be expressed in rule v1. How to express it (a later
rule version or a defined composition step) is open.

### Package sources: representation is not authorisation

A package requirement names the source it needs: `main`,
`non-free-firmware`, `contrib`, `non-free` (Debian components), `backports`
(the Debian stable backports suite) or `lubarchy` (a future authenticated
LUBARCHY repository, P-007). Naming a source never enables it.

Resolution takes the currently authorised sources as input. Since M1-D
([ADR-0001](decisions/0001-non-free-firmware.md)) that is **Debian `main` and
`non-free-firmware`**; through M1-C it was `main` only. A requirement on any
other source is reported with status `policy_review_required` and keeps the
plan `partial`; deciding whether to use that source is a CTO policy decision.
Resolution changes no APT source or component and decides no NVIDIA
sourcing.

### Plan v1

| Field | Content |
| --- | --- |
| `schema`, `inventory_schema`, `rule_schema` | Contract identifiers |
| `inventory_digest` | SHA-256 of the canonical normalized inventory (absent only for an invalid inventory) |
| `rule_set_digest` | SHA-256 of the canonical rule set (rules ordered by id) |
| `source_policy` | The authorised sources the plan was resolved against |
| `status` | `resolved`, `partial`, `conflict` or `invalid` |
| `matched_rules` | Rule id, priority and matched targets, in evaluation order |
| `classifications` | Class name, contributing rules and targets |
| `packages` | Name, disposition, source, `source_status` and reasons |
| `profiles` | Name, `excludes` and reasons |
| `sources` | Each needed source, its status (`authorised` or `policy_review_required`) and its packages |
| `warnings` | Code, message and rule |
| `unresolved` | Targets (the CPU or a device) that no rule matched, with reason `no_rule_matched` |
| `conflicts` | Kind, subject and the rules involved |
| `errors` | For an invalid inventory only: JSON Pointer and failed check, never the rejected value |

Status:

- `resolved` — every target matched a rule, there is no conflict and every
  source is authorised.
- `partial` — no conflict, but some target is unresolved or some source needs
  policy review.
- `conflict` — matching rules are incompatible; nothing is proposed.
- `invalid` — the inventory was rejected; nothing was evaluated.

### Unknown hardware fails closed

Unknown hardware is valid inventory. A CPU or device that no rule matches
stays in the inventory, is listed under `unresolved` with
`no_rule_matched`, and receives no package, profile or source. Nothing is
guessed.

### Canonical JSON and digests

Canonical JSON is UTF-8 with object keys sorted, no insignificant whitespace,
non-ASCII characters emitted as UTF-8, and integers only (floats, NaN and
duplicate keys are rejected). For documents valid under the v1 schemas this
matches RFC 8785 (JCS). Arrays keep their order, so the inventory is
normalized (sorted as above) before serialization. The digest is
`sha256:` followed by the lowercase SHA-256 of those bytes.

There is no wall-clock time in the inventory, rules or plan: identical
normalized inventory, rule set and source policy produce byte-identical plan
JSON. Collection time and similar evidence belong in a future outer
diagnostic envelope, outside the deterministic input. No UUID is invented as
hardware identity.

## Live probe (M1-B)

`hardware/probe.py` is the production probe: running Linux system →
observed facts → canonical inventory v1. It is observation only. It resolves
no package, does not use the test-only resolver, installs nothing, enables no
package source, loads/unloads/binds no driver or module, causes no udev
event, writes no file, sysfs attribute or EFI variable, changes no power,
firmware, Secure Boot, disk or network setting, and runs as an ordinary user
(no root, no sudo). It is not a service and persists nothing; persistence
belongs to a later diagnostics component.

```sh
hardware/probe.py > inventory.json
```

It takes no options. On success it writes exactly one canonical JSON document
followed by one newline to stdout, leaves stderr empty and exits 0. On
failure stdout stays empty, stderr gets one line
`probe: error: <code>: <message>`, and the exit status is 1. The message never
contains an observed value. Error codes: `unsupported_architecture`,
`tool_missing`, `tool_failed`, `cpu_unavailable`, `malformed_platform_fact`,
`malformed_sysfs_fact`, `unsupported_topology`, `duplicate_device_id`,
`invalid_topology`, `udev_sysfs_mismatch`, `contract_failure`. A missing tool
stops the probe; it never installs anything.

### Sources

| Fact | Source |
| --- | --- |
| Debian architecture | `/usr/bin/dpkg --print-architecture`; only `amd64` is accepted |
| CPU architecture, kernel release | `os.uname()`; only `x86_64` is accepted |
| CPU | `/proc/cpuinfo`, fields `vendor_id`, `cpu family`, `model`, `stepping`, `model name`, `microcode` only |
| Firmware mode | `uefi` if `/sys/firmware/efi` exists, else `bios` |
| Secure Boot | The single variable `/sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c` |
| Virtualization | `/usr/bin/systemd-detect-virt --container`, then `--vm` |
| PCI | `/sys/bus/pci/devices/*`: `vendor`, `device`, `subsystem_vendor`, `subsystem_device`, `class`, `modalias`, and the `driver` and `driver/module` links |
| USB devices | `/sys/bus/usb/devices/*`: `idVendor`, `idProduct`, `bDeviceClass`, `bDeviceSubClass`, `bDeviceProtocol`, `bConfigurationValue`, and the `driver` link |
| USB interfaces | Interfaces of the active configuration: `bInterfaceNumber`, `bInterfaceClass`, `bInterfaceSubClass`, `bInterfaceProtocol`, `modalias`, and the `driver` and `driver/module` links |
| MODALIAS cross-check | `/usr/bin/udevadm info --query=property --property=MODALIAS --value --path=<one device>` |

Commands run only from those absolute paths, without a shell, as fixed argv
lists, with a 10-second timeout and `LC_ALL=C`; a single checker rejects any
other command shape before anything is started.

### Privacy access boundary

The probe reads through fixed allowlists: one per sysfs object type and one
for `/proc/cpuinfo` fields. A read of any other attribute is refused in code,
and there is no generic "read all properties" or sysfs/udev dump facility. The
probe never reads `/etc/machine-id`, `/etc/hostname`, `/sys/class/dmi/id`,
DMI/SMBIOS serials or UUIDs, network interfaces or their addresses, disk or
NVMe identity, USB `serial`/`manufacturer`/`product` strings, USB
`descriptors`/`bos_descriptors`, PCI configuration space or BARs, or the
environment. Only values defined by inventory v1 leave the probe, and the
inventory schema has no field for any privacy-sensitive identifier.

### CPU

The vendor is required and must be identical on every logical CPU. Family,
model, stepping, model name and microcode revision are emitted only when every
logical CPU reports the same well-formed value; otherwise they are left out,
never guessed. The microcode revision is normalized to `0x` and lowercase hex
without leading zeros. The model name stays informational.

### Secure Boot and virtualization

Secure Boot is `enabled` or `disabled` only when that exact variable reads as
4 attribute bytes followed by a data byte of 1 or 0. A BIOS boot, an absent,
unreadable, short, long or other-valued variable gives `unknown`. No other EFI
variable is read and efivarfs is never mounted or written.

Virtualization is `container` if `systemd-detect-virt --container` reports
one, otherwise `vm` if `--vm` reports one, otherwise `none`. The technology
identifier must fit inventory v1. DMI strings are never used.

### PCI and USB

Each `/sys/bus/pci/devices` entry becomes a device with id
`pci:<domain:bus:device.function>`. IDs are normalized to fixed-width
lowercase hex; a missing required attribute or a value that does not have the
kernel's exact form fails the probe. The modalias is kept byte-for-byte.

USB devices whose name is a topology locator (`<bus>-<port>[.<port>...]`)
become devices with id `usb:<locator>`. Root hubs (`usbN`) and their
interfaces are not inventory devices; the PCI host controller represents
them. Only interfaces named `<device>:<bConfigurationValue>.<n>` — the active
configuration — are collected, sorted by interface number. An unconfigured
device has no interfaces. A locator deeper than inventory v1 allows fails the
probe with `unsupported_topology` instead of being truncated.

Drivers and modules come only from the kernel-managed `driver` and
`driver/module` symbolic links (their base names, module `-` normalized to
`_`). They are observations, never configuration.

### Bounded udev use

sysfs is authoritative for the kernel MODALIAS. For each PCI device or USB
interface that has one, the probe asks udev for the `MODALIAS` property of
that one device as secondary corroboration (CTO-accepted interpretation):

| `udevadm` result | Meaning | Probe |
| --- | --- | --- |
| exit 0, same value | corroborated | continues |
| exit 0, empty value | corroboration unavailable | continues with the sysfs value |
| non-zero exit | corroboration unavailable (not proof that the property is absent) | continues with the sysfs value |
| exit 0, different non-empty value | disagreement | fails: `udev_sysfs_mismatch` |
| executable missing | — | fails: `tool_missing` |
| timeout or execution failure | — | fails: `tool_failed` |

The probe's internal counter for the "unavailable" cases is named `absent`;
it means corroboration unavailable, nothing more. No other udev property is
ever requested; database exports, attribute walks, full property queries,
`trigger`, `test` and `control` are impossible through the command checker.

### Topology

Parent links come from the kernel's device tree: the probe resolves each
device's real `/sys/devices` path and links it to the nearest ancestor that
is itself an inventory device (PCI bridge → function, xHCI controller → USB
device through the root hub, USB hub → child). Real sysfs paths are used only
for this analysis and are never emitted.

### Output

Devices are sorted by id, USB interfaces by number and module lists are
sorted. Ids, parents and interface numbers are checked before output, which
uses the M1-A canonical JSON form. The output carries no timestamp, no random
identifier and no inventory digest; digests are computed outside the
inventory, by whoever records it.

A probe run on the builder VM proves only that the probe produces a valid,
deterministic inventory for that VM. It is not real-hardware qualification.

## Production resolver (M1-C)

`hardware/resolver.py` (library) and `hardware/resolve.py` (command line)
implement inventory v1 + production rules → plan v1 with the M1-A semantics.
The resolver reads only the inventory it is given, the production rules and
the v1 schemas. It never reads hardware (that is the probe's job), starts no
process, uses no network, calls no APT, dpkg, modprobe or udevadm, writes no
file and persists nothing. It uses Python 3 and `python3-jsonschema` from
Debian `main`; it is not part of the ISO yet. It does not import the test-only
reference resolver, which stays an independent oracle for tests.

```sh
hardware/probe.py | hardware/resolve.py --inventory -
hardware/resolve.py --inventory inventory.json
```

Output: one canonical plan v1 document and one newline on stdout.

| Exit | Meaning | stdout |
| --- | --- | --- |
| 0 | Valid inventory resolved (status `resolved`, `partial` or `conflict` — a conflict is a result, not a crash) | plan |
| 2 | Inventory rejected (status `invalid`, errors give location and check only) | plan |
| 3 | Input unreadable, too large or not strict UTF-8 JSON | nothing |
| 4 | Production rule set rejected | nothing |
| 5 | Internal contract failure (e.g. the produced plan is not plan v1) | nothing |
| 64 | Usage error | nothing |

On 0 and 2 stderr is empty; otherwise it holds one line
`resolve: error: <code>: <message>` with no input values.

### Production rule trust boundary

Production rules come only from `hardware/rules/`; there is deliberately no
option to use another directory, so a caller cannot replace hardware policy.
Only regular, non-hidden `*.json` files are rules; regular `*.md` files are
documentation; subdirectories are never searched. A symbolic link, hidden
entry, subdirectory or other file, a rule that is not strict UTF-8 JSON
(duplicate keys, floats), fails rule v1, is not named `<id>.json` or repeats
an id rejects the **whole** rule set; a bad rule is never skipped. Rules are
read without following symbolic links and loaded in id order.

### Classification baseline

Rule v1 can express packages, profiles, warnings and sources, but the
production policy is narrower. In M1-C production rules carried
**classification effects only**; since M1-D they may also carry `require`
package effects from an authorised source (see
[Package source policy](#package-source-policy-m1-d)). The resolver rejects a
production rule set with any other effect (`forbid`, profiles, warnings) or
source. The 12 classification rules are listed in [`hardware/rules/README.md`](../hardware/rules/README.md)
with provenance in [`hardware/rules/SOURCES.md`](../hardware/rules/SOURCES.md):
CPU vendor (`GenuineIntel`, `AuthenticAMD`), display controllers (PCI class
`03`) from vendors `8086`, `1002` and `10de`, Ethernet (`0200`), other
network controllers (`0280`, `network.other`, never called Wi-Fi), NVMe
(`010802`), SATA (`0106`), and VM, container and explicit bare-metal
platforms.

A classification says what a function is, not what it needs. `gpu.nvidia`
says nothing about integrated vs discrete roles, generation, driver strategy
or firmware. Devices without a high-confidence rule — including all USB
devices in M1-C — stay `unresolved` and receive nothing, so production plans
are normally `partial`.

### Cross-device composition is not expressed

A rule v1 selector matches one target, and a rule matches when **any**
selector matches; rule v1 therefore cannot require two devices at once. M1-C
adds no `gpu.hybrid` rule, does not treat a multi-selector rule as an AND,
and does not infer hybrid graphics from separate Intel and NVIDIA
classifications. Multi-GPU topology, integrated/discrete roles and
hybrid-laptop classification need a future composition contract or rule
version.

## Package source policy (M1-D)

[ADR-0001](decisions/0001-non-free-firmware.md) (Accepted; D-013) authorises
Debian's `non-free-firmware` component for hardware enablement.

| Component / source | Status |
| --- | --- |
| `main` | authorised |
| `non-free-firmware` | authorised (since M1-D) |
| `contrib` | not authorised |
| `non-free` | not authorised |
| `backports` (`trixie-backports`) | not authorised |
| `lubarchy` (future LUBARCHY repository, P-007) | not authorised |
| Testing, Unstable, Experimental, third-party repositories | not authorised |

Suites stay Debian Stable `trixie`, `trixie-updates` and `trixie-security`
from official Debian infrastructure, verified with the Debian archive
keyring. Plans record `source_policy` `authorised: ["main",
"non-free-firmware"]`; a requirement from any other source is reported as
`policy_review_required`.

**Product policy is not system configuration.** The authorised set describes
what LUBARCHY's hardware enablement may propose. It does not change the
builder's APT sources, the live-build configuration, the ISO package set or
the CI container, which all stay `main` only. Activating
`non-free-firmware` in the produced ISO or installed system is later
integration work.

### CPU microcode mappings

| Rule | Selector | Package | Component |
| --- | --- | --- | --- |
| `package.cpu.intel-microcode` | CPU vendor `GenuineIntel` | `intel-microcode` | `non-free-firmware` |
| `package.cpu.amd64-microcode` | CPU vendor `AuthenticAMD` | `amd64-microcode` | `non-free-firmware` |

Both are `require` effects with no version: APT resolves the qualified
Stable version. Package mapping is kept in its own rules, separate from the
`class.cpu.*` classification rules. Other CPU vendors (for example Hygon,
Centaur/Zhaoxin) and mis-cased or unknown vendor strings receive no microcode
package. In M1-D these were the only two production package mappings; since
M1-E the exact production package set is the six listed under
[Device firmware mappings](#device-firmware-mappings-m1-e).

**A package in a plan is a proposal.** Nothing in LUBARCHY installs it yet:
the resolver calls no APT, dpkg, unattended-upgrades or PackageKit.

### Firmware candidates (not active)

Verified to exist in Debian Trixie `non-free-firmware` (see
[`hardware/rules/SOURCES.md`](../hardware/rules/SOURCES.md)) but **candidate
only — no production mapping**: `firmware-nvidia-graphics`,
`firmware-nvidia-gsp`. (M1-D also listed `firmware-intel-graphics`,
`firmware-amd-graphics`, `firmware-iwlwifi`, `firmware-brcm80211` — mapped
in M1-E — and `firmware-realtek`, `firmware-atheros`, `bluez-firmware` —
mapped for specific families in M1-F.) Mapping any of them needs
device-specific evidence and qualification in a later step.

### NVIDIA boundary

Authorising `non-free-firmware` does **not** authorise Debian `non-free`.
M1-D therefore neither authorises nor maps `nvidia-driver`,
`nvidia-kernel-dkms`, `nvidia-open-kernel-dkms` or any other proprietary
NVIDIA driver-stack package; none of them is available from the authorised
sources. The existence of NVIDIA firmware packages in `non-free-firmware`
does not decide the NVIDIA driver strategy, which receives a separate review.
M1-E keeps this boundary: no NVIDIA firmware or driver is mapped. M1-G
gathers the strategy evidence without changing the boundary (see
[NVIDIA driver strategy evidence](#nvidia-driver-strategy-evidence-m1-g)).

## Device firmware mappings (M1-E)

### Evidence model

A firmware mapping needs evidence for both links:

1. **device → kernel module:** the device's kernel modalias matches a
   module alias of the qualified Debian kernel (`modules.alias`, corroborated
   by `modinfo` against the package-owned module files);
2. **kernel module → Debian package:** the authenticated Debian Trixie
   package description names that module as supported by the package's
   firmware.

```text
device modalias → kernel module alias → kernel module → Debian firmware package (non-free-firmware)
```

Vendor IDs, class codes, human-readable names and PCI/USB databases alone
never establish a firmware requirement. Rules match only the kernel's own
alias patterns, verbatim (case preserved, never widened into vendor-wide
wildcards).

### Pipeline: qualified once, committed, never generated at runtime

```text
qualified Debian kernel modules.alias
  → tools/hwe/qualify-firmware-mappings.py extract
  → tools/hwe/module-alias-evidence.json (committed, normalized)
  → tools/hwe/qualify-firmware-mappings.py check-rules / write-rules
  → hardware/rules/package.firmware.*.json (committed, reviewed)
  → production resolver
```

The extractor keeps only the authorised modules' aliases on buses inventory
v1 represents (`pci:`, `usb:`), sorted bytewise and de-duplicated, with case
preserved; malformed `modules.alias` lines are rejected. The production
resolver never reads `/lib/modules`: repository state is the source of
truth. **After a kernel update the mappings must be requalified** (re-extract
on the new kernel, review the diff, regenerate and commit); qualification
provenance is not part of runtime matching.

Qualified kernel: `linux-image-6.12.111+deb13-amd64`, version `6.12.111-1`
(`uname -r` `6.12.111+deb13-amd64`).

### Active families

| Rule | Kernel modules | Buses | Package (component) | Aliases | Classification |
| --- | --- | --- | --- | --- | --- |
| `package.firmware.iwlwifi` | `iwlwifi`, `iwl3945`, `iwl4965` | PCI | `firmware-iwlwifi` (`non-free-firmware`) | 452 | `network.wifi` |
| `package.firmware.intel-graphics` | `i915`, `xe` | PCI | `firmware-intel-graphics` (`non-free-firmware`) | 373 | — |
| `package.firmware.amd-graphics` | `amdgpu`, `radeon` | PCI | `firmware-amd-graphics` (`non-free-firmware`) | 861 | — |
| `package.firmware.brcm80211` | `brcmfmac` | PCI, USB | `firmware-brcm80211` (`non-free-firmware`) | 27 + 9 | `network.wifi` |

Alias counts are per rule after de-duplication: `i915` and `xe` share 107
aliases and `amdgpu` and `radeon` share 149, always within the same package.
No concrete modalias can match two different firmware packages (checked by
the overlap analysis). `amdgpu` includes the kernel's own vendor+class
aliases for AMD display/processing-accelerator classes; they are kept
verbatim.

After M1-E the exact production package set was `intel-microcode`,
`amd64-microcode`, `firmware-iwlwifi`, `firmware-intel-graphics`,
`firmware-amd-graphics`, `firmware-brcm80211` (M1-F adds four; see below).
Every firmware requirement is `require` from
`non-free-firmware` with no version: APT resolves the qualified Stable
version.

### Wi-Fi and graphics classification

`network.wifi` is emitted only for a target that matches a qualified
`iwlwifi`-family or `brcmfmac` alias. PCI class `0280` alone is never proof
of Wi-Fi: such devices keep `network.other`, and a qualified Wi-Fi device
with class `0280` carries both. Firmware rules add no `gpu.*`
classification; GPU classes still come only from PCI display class `03`, so
a firmware requirement and a hardware class remain separate facts.

### Excluded and deferred

- `brcmsmac`: its only module aliases are on the BCMA bus
  (`bcma:m04BFid0812rev{11,17,18}cl*`), which inventory v1 cannot represent;
  the same cores are claimed by `b43`, whose firmware comes from `contrib`
  (not authorised). Mapping it through the BCMA host bridge's PCI IDs would
  widen scope and create ambiguity, so it is deferred to a future inventory
  and firmware-policy review (CTO-approved scope refinement during M1-E).
- SDIO aliases (23 for `brcmfmac`) and non-bus aliases (`iwl4965`): not
  representable in inventory v1; excluded.
- IPU/camera devices (also in `firmware-intel-graphics`), AMD NPU
  (`amdxdna`) and `r128`: not mapped.
- Deferred families after M1-E: `firmware-realtek`, `firmware-atheros`,
  `bluez-firmware`, `firmware-nvidia-graphics`, `firmware-nvidia-gsp`, Intel
  sound, MediaTek and other Qualcomm firmware (M1-F qualifies some of them;
  see below).

### What this does not prove

Kernel-alias and package evidence plus synthetic tests do not prove that
Wi-Fi associates, graphics acceleration works, firmware loads on physical
devices, suspend/resume works or package installation succeeds. Those need
later installation and real-hardware validation. Nothing is installed and no
module is loaded by mapping or qualification.

## Second-wave firmware mappings (M1-F)

### Strengthened evidence chain

For broad firmware packages a package description is supporting evidence
only. An M1-F family is active only when, for every module in it:

```text
device modalias
  → kernel module alias (binding module; PCI/USB only)
  → binding module + its dependency closure (modinfo "depends")
  → firmware files declared by those modules (modinfo "firmware")
  → ownership of those files in authenticated Debian data
  → exactly one non-free-firmware package
```

- Module metadata is read from the package-owned `.ko` files of the
  qualified kernel (`linux-image-6.12.111+deb13-amd64` 6.12.111-1); the
  qualification tool's own ELF reader agrees with `modinfo` for every mapped
  module.
- File ownership comes from Debian's Contents indices, fetched through an
  isolated APT configuration and verified against the signed `InRelease`,
  and is corroborated by the candidates' downloaded payload manifests (the
  `.deb` hash matches the signed index; payloads are deleted).
- Every declared firmware file of the closure must be shipped by the family
  package or by no package (for example `cfg80211`'s `regulatory.db`); at
  least one must be shipped by the family package. No relevant firmware
  path may be shipped by two packages.
- An alias that another module also claims device-specifically must be
  same-package, explicitly reviewed or excluded.
- Rules with different package **or** classification may not overlap.

An empty firmware declaration is not taken to mean "no firmware": such
modules are deferred unless other authoritative evidence exists. Where a
driver builds file names at runtime, exhaustive file-level proof is not
claimed.

Evidence is recorded in the versioned tooling format
`lubarchy.hardware.firmware-evidence/v2` (`tools/hwe/module-alias-evidence.json`);
the M1-E alias evidence inside it is unchanged, and the M1-E rules still
regenerate byte-for-byte from the original M1-E evidence. This is tooling
evidence, not a change to inventory, rule or plan v1.

### Active second-wave families

| Rule | Kernel modules (binding) | Buses | Package | Classification | Aliases |
| --- | --- | --- | --- | --- | --- |
| `package.firmware.realtek-wifi` | `rtl8188ee`, `rtl8192ce`, `rtl8192cu`, `rtl8192de`, `rtl8192ee`, `rtl8192se`, `rtl8723be`, `rtl8821ae`, `rtl8xxxu`, `rtw88_*e`/`*u` (8723d, 8821c, 8822b, 8822c), `rtw89_*e` (8851b, 8852a, 8852b, 8852bt, 8852c, 8922a) | PCI, USB | `firmware-realtek` | `network.wifi` | 29 + 157 |
| `package.firmware.atheros-wifi` | `ar5523`, `ath10k_pci`, `ath6kl_usb`, `wil6210` | PCI, USB | `firmware-atheros` | `network.wifi` | 12 + 58 |
| `package.firmware.atheros-bluetooth` | `ath3k` | USB | `firmware-atheros` | `bluetooth.controller` | 62 |
| `package.firmware.mediatek-wifi` | `mt7601u`, `mt7615e`, `mt7663u`, `mt76x0e/u`, `mt76x2e/u`, `mt7915e`, `mt7921e/u`, `mt7925e/u`, `mt7996e`, `rt2800pci`, `rt2800usb`, `rt61pci`, `rt73usb` | PCI, USB | `firmware-mediatek` | `network.wifi` | 61 + 415 |
| `package.firmware.bluez-bcm203x` | `bcm203x` | USB | `bluez-firmware` | `bluetooth.controller` | 1 |

The exact production package set is now ten: the six from M1-D/M1-E plus
`firmware-realtek`, `firmware-atheros`, `firmware-mediatek` and
`bluez-firmware`.

Reviewed exclusions (identity ambiguity, narrowing only):
`pci:v000010ECd00008192…` (also `r8192e_pci`, whose firmware Debian does not
ship) is not mapped by `rtl8192se`; `usb:v050Dp7050…` (also `p54usb` and
`rt2500usb`) is not mapped by `rt73usb`.

### Deferred, with reasons

| Family | Reason |
| --- | --- |
| Realtek Ethernet (`r8169`, `r8152`, `r8153_ecm`) | Firmware is loaded only for some chip revisions, which the modalias cannot distinguish (all RTL8168 variants share `10ec:8168`). |
| USB Bluetooth via `btusb` (`btrtl`, `btmtk`, `btintel`, `btbcm`) | `btusb` binds many vendors; its closure declares firmware from `firmware-realtek`, `firmware-mediatek` and `firmware-iwlwifi`, so module → package is ambiguous at the alias level. |
| `hci_uart` | Serial/ACPI/OF transports only. |
| `ath6kl_sdio`, `rtw88_*s` | SDIO only. |
| `ath11k_pci`, `ath12k` | No declared firmware; file names are built at runtime from board data. |
| Realtek audio / card-reader firmware | Not requested by any PCI/USB network or Bluetooth module; out of scope. |
| `bluez-firmware` Raspberry Pi files | Serial/platform only, not amd64 PCI/USB. |

### Bluetooth ownership

A Bluetooth firmware rule matches only the target whose alias binds the
Bluetooth module (`ath3k`, `bcm203x` USB devices). A combo card's PCI Wi-Fi
function never receives a Bluetooth package or `bluetooth.controller` on
the strength of being on the same card; its Bluetooth half, typically a USB
device bound by `btusb`, stays unresolved because `btusb` is deferred.

### Unsupported buses

SDIO, BCMA, ACPI/OF/platform, serdev/UART and other non-PCI/non-USB aliases
are never mapped; inventory v1 does not represent them.

### Reviewed observation on an M1-E family

`brcmfmac` declares `brcm/brcmfmac43456-sdio.bin`, which Debian ships in
`raspi-firmware`. It is firmware for an SDIO-only chip (not mapped), so it
does not affect the PCI/USB `brcmfmac` mapping; it is recorded as a reviewed
foreign request. M1-E families keep their original evidence mode.

## NVIDIA driver strategy evidence (M1-G)

M1-G qualifies the NVIDIA driver options as **architecture evidence only**:
[ADR-0002](decisions/0002-nvidia-driver-strategy.md) is **Proposed** and
nothing is authorised. No NVIDIA source, package, kernel module or production
rule exists. `class.gpu.nvidia` stays classification-only, the production
package set stays at 10, and the source policy stays `main` +
`non-free-firmware`.

- Evidence: [hardware/nvidia/](../hardware/nvidia/README.md):
  `evidence-v1.json` (`lubarchy.hardware.nvidia-evidence/v1`) and a normalized
  snapshot of NVIDIA's `supported-gpus.json` (`supported-gpus-v1.json`,
  `lubarchy.hardware.nvidia-gpu-snapshot/v1`, 2,322 entries with provenance
  hashes). Neither is read by the resolver, the probe or any rule.
- Strategies compared: Debian Stable 550 (A), `trixie-backports` 550 (B),
  NVIDIA's signed Debian 13 repository (C), Nouveau (D). Debian 550 has 93
  open security issues in trixie with no DSA, so A and B are not recommended.
- Support classes derived from the snapshot: `legacy-older`, `legacy-470`
  (Kepler), `legacy-580` (Maxwell/Pascal/Volta), `open-or-proprietary`
  (Turing to Hopper), `open-only` (Blackwell and later). NVIDIA's Debian 13
  repository carries branches 590 to 615 only, so it covers the last two
  classes only.
- Isolated APT simulations on the builder (test-only state, nothing
  installed) record installs, upgrades, the pinned downgrade, removals, the
  i386 libraries, and the takeover of Debian's NVIDIA packages and `dkms`
  when NVIDIA's repository is enabled.
- Proposed tier model: Nouveau (in Debian `main`) as a **baseline/fallback
  candidate**, not a qualified default. Per generation it is limited
  (Kepler and older, Maxwell/Pascal/Volta), a fallback candidate
  (Turing to Hopper) or not yet qualified (Blackwell), on trixie's kernel
  6.12 and Mesa 25.0.7. Strategy C would be an opt-in for Turing and later
  (Proposed only), after a D-005 exception, P-006 (Secure Boot), a
  kernel-update/rollback gate (M4) and real-hardware qualification (M7). No
  NVIDIA generation is production-qualified without real-hardware evidence.
- NVIDIA signing key: the documented `cuda-archive-keyring.gpg` fallback
  URL returned 404 during qualification, and the repository index also
  exposes `8793F200.pub`. Neither is a trust anchor: any future bootstrap
  must verify the full fingerprint
  `02182E60104FCDC26EAE1B8597A5D4CB8793F200` through an explicitly qualified
  mechanism.
- Hybrid and multi-GPU stay out of scope. Future inventory observations
  (`boot_vga`, DRM card/render nodes, connector ownership, PCI power state)
  are recommended for a later inventory-v2 proposal; no schema changes.
- Gate 8 (`tests/nvidia-strategy.py`) enforces the boundary and the
  evidence consistency using committed files only.

## Hardware classes

Initial future classes:

- Intel/AMD CPUs and microcode
- Intel/AMD/NVIDIA graphics
- Hybrid graphics
- Ethernet/Wi-Fi
- Bluetooth
- Audio
- NVMe/SATA
- Input devices
- Power management
- Suspend/resume
- External displays / HiDPI

## Firmware principles

- Firmware and microcode come from authenticated Debian sources.
- Install only the firmware needed or reasonably expected for the detected
  hardware.
- Firmware changes are recorded in diagnostics.

## HWE coordination

Kernel, firmware, Mesa, and driver versions must be coordinated. Any use of
Debian Backports for hardware enablement must be justified and qualified
(D-005).

## NVIDIA requirement

NVIDIA is a first-class requirement. NVIDIA support must be qualified for both
desktop sessions. The driver sourcing approach is not yet defined (see
[NVIDIA boundary](#nvidia-boundary)).

## Desktop fallback

If Hyprland fails qualification on a configuration but Plasma is safe, the
machine should remain usable with Plasma.

## Profiles

Rule v1 can require symbolic profile labels and declare mutually exclusive
ones (see above). What a profile contains is **TBD.**

## Validation requirements

- Every configuration step is validated after it is applied.
- Validation results are persisted.
- A failed validation must leave the system usable and report the failure.

## First-boot concept

Hardware detection and resolution may run during installation and at first
boot. The exact split between installer and first boot is TBD.

## Diagnostics

- Persist inventory, decisions, and validation results.
- Provide actionable diagnostics a user can share for support.
- Diagnostics must not include secrets.

## Hardware matrix

LUBARCHY must maintain a hardware matrix recording qualification results per
hardware class and configuration, using this status vocabulary:

| Status | Meaning |
| --- | --- |
| PASS | Qualified with recorded evidence |
| FAIL | Tested and does not work acceptably |
| LIMITED | Works with documented limitations |
| NOT TESTED | No qualification evidence |
