# ADR-0001 — Authorize Debian non-free-firmware for hardware enablement

Status: Accepted
Date: 2026-10-12

Accepted by the CTO in the M1-D brief. Recorded in the decision register as
[D-013](../decisions.md#d-013--debian-non-free-firmware-authorised-for-hardware-enablement).

## Context

LUBARCHY must make ordinary hardware work without terminal work (D-008).
Many supported CPUs and devices need redistributable binary firmware or
microcode that Debian does not ship in `main`: Debian ships it in the
separate official archive component `non-free-firmware`, created after the
2022 General Resolution on non-free firmware.

Until now, every LUBARCHY package source has been Debian `main` only: the M0
builder, the M0 live-build configuration, the CI container and the M1-C
resolver source policy. Hardware enablement cannot map CPU microcode or most
device firmware without a decision on this component. Adding a package source
requires an ADR (D-005, [ADR process](README.md)).

## Requirements and constraints

- APT/dpkg remains the only host package authority (D-004).
- No FrankenDebian: Debian Stable is never mixed with Testing, Unstable or
  uncontrolled sources (D-005).
- Packages must keep Debian's signing, update and security infrastructure.
- Firmware is installed only where the detected hardware needs it, through
  source-controlled compatibility mappings (hardware-enablement firmware
  principles).
- NVIDIA support is a first-class requirement, but its driver strategy is a
  separate decision.

## Options considered

1. **Stay `main` only.** Rejected: Intel/AMD CPU microcode and most Wi-Fi,
   Bluetooth and GPU firmware would be unavailable, contradicting D-008.
2. **Authorise Debian `non-free-firmware`.** Accepted.
3. **Authorise `non-free` (and `contrib`) as well.** Rejected for now: they
   carry non-firmware non-free software (for example the proprietary NVIDIA
   driver stack) that needs its own review.
4. **Vendor or third-party firmware repositories, or `trixie-backports`.**
   Rejected: weaker provenance, separate keys, uncoordinated updates, and
   (for Backports) a different support model that D-005 reserves for
   justified, qualified cases.

## Decision

LUBARCHY authorises Debian's `non-free-firmware` archive component for
hardware enablement.

- Authorised suites remain only Debian Stable `trixie`, `trixie-updates` and
  `trixie-security`.
- Authorised origins remain official Debian infrastructure, verified with the
  Debian archive keyring.
- The authorised component set for hardware enablement becomes exactly
  `main` and `non-free-firmware`.

This decision does **not** authorise `contrib`, `non-free`,
`trixie-backports`, Testing, Unstable, Experimental or any third-party
repository. It does **not** resolve NVIDIA proprietary-driver policy, and it
does not resolve P-007 (LUBARCHY repository infrastructure): it is Debian
archive-component policy only.

Source authorisation does not mean installing every firmware package.
LUBARCHY selects packages only through source-controlled compatibility
mappings in `hardware/rules/`, each qualified separately.

## Rationale

- Many supported CPUs and devices require redistributable binary firmware
  that Debian ships in `non-free-firmware`.
- Debian treats `non-free-firmware` as a separate official archive component;
  since the 2022 General Resolution its firmware is included in Debian's
  official installation and live media.
- The Debian 13 release notes show `Components: main non-free-firmware` for
  `trixie`, `trixie-updates` and `trixie-security`.
- Using Debian's own component keeps Debian signing, update and security
  infrastructure, and package provenance remains Debian.
- This is materially safer and more maintainable than vendor or random
  repositories.

## Security consequences

- Packages come from the same signed Debian archives and keyring as `main`;
  signature verification stays mandatory and fails closed.
- The firmware and update surface grows: firmware blobs are not built from
  source and cannot be audited like free software. Mapping each package to
  detected hardware limits what is installed.
- Security updates for these packages arrive through `trixie-security` and
  `trixie-updates` like any other Debian package.

## Operational consequences

- The production resolver's source policy becomes `main` +
  `non-free-firmware`; requirements from any other source stay
  `policy_review_required`.
- The first production package mappings are Intel and AMD CPU microcode
  (`intel-microcode`, `amd64-microcode`). Device firmware mappings follow
  only with device-specific qualification.
- **Product policy is not builder or ISO configuration.** This decision does
  not change the builder's APT sources, the live-build configuration, the
  ISO package set or the CI container, which all stay `main` only. Source
  activation in the produced ISO or installed system is later integration
  work.
- Firmware packages add size to images and updates.

## Validation

- Package existence and component verified against signature-verified Debian
  Trixie metadata (M1-D evidence).
- Resolver tests require the exact source policy, the exact production
  package set and `policy_review_required` for any other component.

## Rollback / reversibility

Reversible: remove `non-free-firmware` from the product source policy and the
mappings that use it. The consequence is lost hardware support (microcode
and firmware) on affected machines, which must be weighed at that time.
Installed systems would keep packages already installed until removed.

## Consequences

- Trade-offs accepted: non-free licensing of firmware, a larger
  firmware/update surface, package size, and the need to qualify each
  mapping.
- Existence of a firmware package (including NVIDIA firmware) does not decide
  the NVIDIA driver strategy, which receives a separate review.
