# Hardware Enablement

Requirements for LUBARCHY hardware enablement. Hardware enablement is a
first-class subsystem (D-008).

**Status (M1-A):** the data and policy contracts — inventory v1, rule v1 and
plan v1 — are defined and tested against synthetic fixtures. **No live
hardware probe exists, and nothing installs, enables or configures anything.**
Live probing, real compatibility mappings, installation, configuration and
validation are later M1 work.

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

### Observation sources (for the future probe)

In priority order: `/sys` and udev device data; kernel modalias and the bound
driver/module; PCI numeric IDs; USB numeric IDs; purpose-built utilities only
as enrichment or diagnostics. Human-readable names are informational and are
never used for matching. The probe is not implemented in M1-A.

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

Resolution takes the currently authorised sources as input. Today that is
**Debian `main` only**. A requirement on any other source is reported with
status `policy_review_required` and keeps the plan `partial`; deciding
whether to use that source is a CTO policy decision. M1-A changes no APT
source or component and decides no NVIDIA sourcing.

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
desktop sessions. The driver sourcing approach is not yet defined.

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
