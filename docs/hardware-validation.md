# Post-install Hardware Validation (M1-M)

M1-M answers one question:

> Did the qualified hardware-enablement plan actually result in a healthy,
> usable system?

Package installation success is not hardware health. Validation compares
the running system with what the existing plan implies, and says exactly
what it could and could not observe.

**Status:** implemented locally; awaiting CTO review.

| Piece | Location |
| --- | --- |
| Library (evaluator and collector) | `hardware/hardware_validation.py` |
| CLI (read-only) | `hardware/validate-hardware.py` |
| Result contract | `lubarchy.hardware.validation-result/v1` ([schema](../hardware/schemas/validation-result-v1.schema.json)) |
| Static gate 13 | `tests/hardware-validation.py` (synthetic evidence and sysfs; never reads the runner) |

## Place in the pipeline

```
probe → inventory → classify → resolve (plan) → transaction → stage → apply
      → recover if needed → validate
```

- **Separate stage.** Validation is separate from detection, resolution,
  installation and recovery. It consumes their results; it never feeds a
  decision back into them.
- **No second rules engine.** A fresh probe and the existing resolver with
  the production rules give the plan. The validation profile adds no
  matching: it is keyed by the plan's classifications and matched package
  rules and says which properties each implies.
- **Read-only.** Validation never installs, configures, loads or binds
  anything, never writes a file, never repairs packages and never changes
  hardware policy or rules. It only reads sysfs/procfs and runs seven fixed
  read-only commands.

## Expected state: the validation profile

The profile (`PROFILE` in `hardware/hardware_validation.py`, versioned as
`lubarchy.hardware.validation-profile/v1` and bound by digest) maps:

| Plan element | Properties checked |
| --- | --- |
| `gpu.intel`, `gpu.amd` | device present, a kernel driver bound, a DRM card node |
| `gpu.nvidia` | device present; driver **NOT_TESTED** (`nvidia_not_qualified`) |
| `network.wifi` | device present, driver bound, a wireless network interface |
| `network.ethernet` | device present, driver bound, a network interface |
| `bluetooth.controller` | device present, driver bound, a kernel HCI controller |
| `storage.nvme`, `storage.sata` | device present, driver bound |
| `network.other` | device present |
| firmware package rules | the bound driver is one of the family's kernel modules; firmware request failures for this device, classified by the firmware requirement policy (below) |
| microcode package rules | the package installed; microcode applied at boot (see below) |
| every required plan package | installed (`install ok installed`), exact version when bound |

- **Firmware-family modules** come from the canonical committed mapping.
  `tools/hwe/firmware-mappings.json` remains the canonical source of the
  firmware-family module mapping (the same input that generates the firmware
  rules).
  - The validator's embedded copy is a derived representation, never an
    independent source of truth.
  - Gate 13 requires exact semantic equality in both directions (no module,
    family or policy key more or less); any drift fails the gate.
  - Neither the mapping nor the copy can be changed by a caller at runtime.
- **Coverage.** Gate 13 requires every production classification and every
  production package rule to be covered, so a new rule cannot silently go
  unvalidated.
- **Unresolved devices** (no rule matched) are not validated; they are
  listed in `unvalidated_targets`.

## Checks and statuses

| Status | Meaning |
| --- | --- |
| `PASS` | The property was observed and matched the expectation |
| `FAIL` | The property was observable and did not match |
| `LIMITED` | The environment cannot fully verify the property |
| `NOT_TESTED` | The check was intentionally not executed (for example, its prerequisite failed, or NVIDIA) |

Every check records: the check ID, class, target, the inventory device
identity, critical or advisory, the status, a machine-readable reason, and
bounded expected/observed facts.

**Reason codes** include:
- device and driver: `device_missing`, `device_missing_after_reboot`,
  `device_identity_changed`, `expected_driver_not_bound`, `driver_mismatch`;
- firmware: `required_firmware_load_failed`, `optional_firmware_unavailable`,
  `firmware_requirement_unclassified`;
- device functions: `expected_interface_missing`,
  `expected_wireless_interface_missing`, `expected_drm_device_missing`,
  `expected_controller_missing`;
- packages and system: `package_missing`, `package_version_mismatch`,
  `dpkg_unhealthy`, `apt_unhealthy`, `service_failed`,
  `system_critical_unit_failed`, `system_not_running`;
- limitations: `observation_unavailable`, `kernel_log_unavailable`,
  `virtualized_cpu`, `reboot_required`, `nvidia_not_qualified`,
  `prerequisite_failed`;
- storage: `root_read_only`, `root_block_device_missing`,
  `filesystem_errors`.

**Critical and advisory checks.**
- Critical: device present, driver, firmware, device functions,
  microcode, packages, `dpkg --audit`, `apt-get check`, root storage,
  system state and critical units.
- Advisory: failed units outside the critical-unit list, and
  root-filesystem error counters.

**Aggregation (deterministic):**
1. `FAIL` if any **critical** check fails;
2. otherwise `LIMITED` if any check is not `PASS` (an advisory failure, a
   `LIMITED` or a `NOT_TESTED` check);
3. otherwise `PASS`.

`LIMITED` and `NOT_TESTED` are never promoted to `PASS`. A result whose
status or summary disagrees with its checks is rejected.

## Device attribution

- Every hardware-specific check targets a plan target, which is an
  inventory device ID, and carries the device's stable identity: PCI
  vendor/device/subsystem/class, or USB vendor/product.
- At runtime the collector re-reads that identity from sysfs. A different
  identity at the same bus location is `device_identity_changed`.
- Driver binding is read from the device's own `driver` link (and its USB
  interfaces), never from "the module exists on disk".
- Network interfaces, DRM card nodes and HCI controllers count only if
  their sysfs `device` resolves to that device. Sysfs paths are runtime
  evidence only; they are never stored.
- Kernel firmware request failures are attributed to a device only when the
  kernel line names that device (`<driver> <PCI address>:`, a USB interface, or an
  `hciN` that resolves to it). A failure for another device, or one that
  cannot be attributed, never fails the target.
- Two inventory devices resolving to one sysfs device, or evidence that
  does not cover every inventory device, is rejected (fail closed).

## Firmware request failures

A kernel firmware request failure is evidence, not automatic proof of
broken hardware: drivers also request optional firmware and data.
Requiredness is therefore never inferred from the file name, the log
severity, the bound driver, package membership or the module family.

The profile's firmware requirement policy (`firmware_requirements`) lists,
per firmware family, file patterns that are known **required** for the
validated function or known **optional**. Each entry carries a
source-controlled evidence reference. Anything not listed is **unknown**.
The policy is part of the profile digest, so changing it makes existing
results stale. A caller can never supply or change a classification.

| Attributed failures for the device | Result |
| --- | --- |
| A known-required file failed | `FAIL` `required_firmware_load_failed` |
| An unknown or unnamed file failed (and no required one) | `LIMITED` `firmware_requirement_unclassified` |
| Only known-optional files failed | `LIMITED` `optional_firmware_unavailable` (an optional capability may be missing or unverified) |
| None | `PASS` (given a bound driver and a readable kernel log) |

- **Precedence:** required outranks unknown, which outranks optional; a
  required failure is never downgraded.
- **Ambiguity:** a file matched by both a required and an optional entry is
  a contract failure.
- **Current production policy is empty.** LUBARCHY does not yet have
  qualified per-device requiredness data, so every attributed firmware
  failure is `LIMITED` / `firmware_requirement_unclassified`. No entries
  are guessed. Real-hardware qualification will add evidence-backed
  required and optional entries.
- **Real failures are still FAIL.** The firmware check never hides one. A
  missing device, an unbound or wrong driver, or a missing interface, DRM
  node or controller is a separate critical check and still fails
  normally.
- **Fail closed without false FAILs:** an unprovable firmware failure keeps
  the result from passing, but it does not falsely declare otherwise
  healthy hardware failed.

## CPU microcode

The check `cpu.microcode:<package>`:

| Situation | Result |
| --- | --- |
| Virtual machine | `LIMITED` `virtualized_cpu`: the hypervisor controls microcode; a VM never proves physical microcode |
| Container | `LIMITED` `container_platform` |
| Package not installed | `NOT_TESTED` `prerequisite_failed` (the package check fails) |
| Current boot started before the package was installed | `LIMITED` `reboot_required` |
| Kernel log not readable | `LIMITED` `kernel_log_unavailable` |
| Microcode loader error in the kernel log | `FAIL` `microcode_load_failed` |
| Loader reported, with a revision, on bare metal after a reboot | `PASS` |
| Otherwise | `LIMITED` `microcode_state_unreported` |

`PASS` means the kernel microcode loader ran at boot after installation and
reported a revision without error. It does not prove that the revision is
the newest one shipped in the package.

## Freshness, boot identity and reboot

- **Bindings.** A result binds:
  - the inventory digest, rule-set digest and plan digest;
  - the installed-state digest (package, architecture, version, status and
    automatic mark: the same identity the package transaction binds);
  - the boot identity (SHA-256 of the kernel `boot_id`; never the raw value);
  - the profile digest;
  - when readable (root), the latest complete LUBARCHY apply attempt
    (attempt ID and transaction digest), which also makes package checks
    version-bound.
- **Stale.** `validate-hardware.py check-current` recomputes every binding.
  Any change (a device, a rule, the plan, a package version or mark, a
  reboot, the profile) makes the result stale (exit 3). A stale result is
  never reused as a current PASS.
- **Same boot or after a reboot.** `bindings.boot.relation` is
  `post_reboot` only when every plan package is installed and the running
  boot started after the last of them was unpacked. Otherwise it is
  `same_boot`, or `not_applicable` when plan packages are missing. Nothing
  claims post-reboot qualification if no reboot occurred.
- **Continuity.** `revalidate` reads a previous result on stdin and adds a
  check for every device that previously passed. A device that is gone is
  `device_missing_after_reboot` (boot changed) or `device_missing`. A
  previous result from another profile is rejected. The previous result
  can only add expectations; it never removes a check.

## Privilege

- **Unprivileged by default.** sysfs, procfs, `dpkg --audit`, `dpkg-query`,
  `apt-mark` and `systemctl` work without root.
- **Root adds two observations:**
  - the kernel log on systems that restrict it (`kernel.dmesg_restrict`,
    journal permissions);
  - the root-only LUBARCHY apply journal, which provides exact package
    versions.

  Without root these are `LIMITED` (`kernel_log_unavailable`) or recorded
  as the limitation `apply_journal_unreadable`.
- **No weakening.** Validation never changes `dmesg_restrict`, journal
  permissions, sudoers or Polkit, and adds no privilege mechanism.
- **Reading the kernel log.** The kernel log counts as readable only if it
  contains the boot's `Linux version` line. An empty or permission-denied
  read is treated as unavailable, never as "no failures".

## Privacy

The collector never reads MAC addresses, serial numbers, disk UUIDs,
hostnames, user names or IP addresses.
- Network interfaces, DRM nodes and controllers are recorded only as counts.
- Unit instance names are redacted (`user@1000.service` →
  `user@*.service`).
- Kernel log lines are never stored. Only the attributed device ID and a
  bounded firmware file name are kept.
- The boot ID is stored only as a digest.

Gate 13 feeds a synthetic tree full of such values and checks that none of
them appears in the evidence or the result.

## CLI

```
hardware/validate-hardware.py validate                    # current state → result
hardware/validate-hardware.py revalidate    < previous.json
hardware/validate-hardware.py check-current < result.json
hardware/validate-hardware.py evidence                    # normalized, redacted runtime evidence
```

- **No caller input.** The CLI accepts no device, package, driver, path,
  command, check or environment input. What is checked, and what `PASS`
  means, come only from the plan and the profile.
- **Commands.** The seven commands are fixed argv lists, run with a literal
  environment, `shell=False` and no stdin.
- **Exit status:**
  - `0`: `PASS`, or the result is current;
  - `1`: `FAIL`;
  - `2`: `LIMITED`;
  - `3`: stale;
  - `4`: malformed input or evidence;
  - `7`: internal or observation failure;
  - `64`: usage.

## VM and real-hardware boundary

The Debian builder VM can qualify the validator's runtime: system and
package-manager health, boot identity, root storage, virtual device driver
binding and the handling of limitations.

It cannot qualify:
- physical CPU microcode (always `LIMITED` in a VM);
- Wi-Fi or Bluetooth radio operation;
- GPU acceleration;
- suspend/resume and external displays.

Functional exercise of radios, GPUs and displays is not part of v1. Every
result carries the limitation `physical_function_not_exercised`. Those
claims need real-hardware qualification (a later milestone).

## Not in M1-M

- installer, first-boot or service integration, and persisting results;
- a user-facing diagnostics application (the result is designed to feed
  one later, together with support-bundle tooling);
- BlueZ (D-Bus) controller visibility (the kernel HCI controller is
  checked);
- NVIDIA driver qualification;
- real-hardware qualification.
