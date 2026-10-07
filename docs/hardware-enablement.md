# Hardware Enablement

Requirements for LUBARCHY hardware enablement. Hardware enablement is a
first-class subsystem (D-008). **Nothing here is implemented yet**; this is
planned for M1 and later.

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

The inventory schema is not yet defined.

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

Hardware profiles and their contents: **TBD.**

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
