# Project State

Operational checkpoint for LUBARCHY. Baseline date: 2026-10-07.

## Summary

LUBARCHY is a clean Debian Stable-based desktop Linux distribution. The target
experience is: boot USB → simple installer (approximately five primary
screens) → reboot → usable desktop, with ordinary hardware working without
terminal work.

## Confirmed baseline

| Item | Value |
| --- | --- |
| Base | Debian Stable |
| Initial target | Debian 13 "Trixie", amd64 |
| Builder source | `debian-13.7.0-amd64-netinst.iso` |
| Production ISO mechanism | Debian `live-build` |
| Host package authority | APT/dpkg |
| Repository | `lubarchy/lubarchy` |
| Remote | `https://github.com/lubarchy/lubarchy.git` |
| Primary branch | `main` |

Accepted decisions are listed in [decisions.md](decisions.md).

## Current milestone: M0 — Minimal reproducible ISO

**Goal:** prove LUBARCHY can be built cleanly from source-controlled
configuration before adding desktop or installer complexity.

**M0 status: IN PROGRESS.**

### M0 exit checklist

- [ ] Fresh public repository with governance baseline
- [x] Clean Debian builder (M0-D; see [build.md](build.md))
- [x] Documented build prerequisites (builder and live-build tooling; see [build.md](build.md))
- [x] Committed `live-build` configuration (M0-E; validated, not yet built)
- [x] Minimal ISO builds (M0-F; first build, not yet boot-qualified)
- [x] Checksum and package manifest (M0-F; initial artifact metadata)
- [ ] Successful UEFI QEMU/KVM boot
- [ ] Automated boot smoke test
- [ ] Clean rebuild succeeds
- [ ] Lint/CI baseline passes
- [ ] Build documentation accepted

An item is checked only when evidence exists and the CTO has reviewed it.

### M0 execution steps

| Step | Description | Status |
| --- | --- | --- |
| M0-A | Read-only preflight and Debian ISO trust verification | PASS |
| M0-B | Explicit local repository binding | PASS |
| M0-C | Repository/governance baseline | PASS |
| M0-D | Clean Debian 13 builder environment | PASS |
| M0-E | live-build tooling and source-controlled configuration | PASS |
| M0-F | First minimal ISO build and artifact capture | PASS |

- M0-C — repository/governance baseline: **PASS** (CTO-reviewed), commit
  `c0a665985f54de33d813ea1bf62794e7170299be`.
- M0-D — clean Debian builder: **PASS**. `lubarchy-builder` on
  `qemu:///system` is established and qualified (2026-10-08); measured
  baseline in [build.md](build.md).
- M0-E — live-build tooling and configuration: **PASS**. `live-build
  1:20250505+deb13u1` from Debian trixie `main` is installed on the builder;
  the M0 configuration lives in `build/live` and passes
  `tests/live-build-config.sh` on the builder.
- M0-F — first minimal ISO: **PASS**. `lubarchy-m0-amd64.hybrid.iso` was built
  from `a7d1e9c10df6fbe3d2a9a30e6bb3378dcabf1073`, statically inspected,
  checksummed and retained outside Git (see [build.md](build.md)). It has
  **not** been booted.

A step's status is updated only after its execution has been validated and
reviewed by the CTO.

### Verified builder source

- File: `debian-13.7.0-amd64-netinst.iso`
- SHA-512 (from cryptographically authenticated Debian `SHA512SUMS`):
  `ef04d0276850d70e4aaec0b55003628660e9631b26e35215a70ab3c8c707294a4b12731bc73c75a932902f703a7c278310ca99df28d6b36fb06d5618d8d0f678`
- Debian CD signing key fingerprint:
  `DF9B 9C49 EAA9 2984 3258 9D76 DA87 E80D 6294 BE9B`

### Repository reference

- Initial GitHub commit: `f10f25607ecddcface2fed3768f76605a01f5174`
  ("Initial commit", contains `LICENSE` only).
- Governance baseline (M0-C): `c0a665985f54de33d813ea1bf62794e7170299be`
  (local; not yet pushed).

## Intentionally undecided

These are pending ADR subjects (see [decisions.md](decisions.md)). They must
not be treated as decided:

- P-001 — Bootloader / boot architecture
- P-002 — Filesystem/subvolume layout (Btrfs preferred, pending validation)
- P-003 — Encryption architecture/default
- P-004 — Installer technology
- P-005 — Hyprland sourcing/version policy
- P-006 — Secure Boot ownership/key model
- P-007 — LUBARCHY repository infrastructure
- P-008 — Release cadence/support policy
- P-009 — Application delivery UX

## Not implemented

- CI
- Automated boot smoke test
- LUBARCHY package repository
- Desktop stack
- Hardware detector/resolver
- Installer
- Recovery implementation
- Update manager
- Secure Boot implementation
- Factory reset
- Real-hardware qualification

## Milestone order

1. M0 — Minimal reproducible ISO
2. M1 — Hardware enablement foundation
3. M2 — Hyprland + Plasma desktop
4. M3 — Beginner installer
5. M4 — Recovery and rollback
6. M5 — Dual boot / advanced storage
7. M6 — Update system / release qualification
8. M7 — Real-hardware qualification

## Immediate next action

Boot the first M0 ISO in a fresh isolated UEFI KVM/QEMU test VM and establish
the automated serial boot smoke-test baseline.

## PASS rule

A step or milestone is PASS only when every criterion is backed by recorded
evidence (commands, exit statuses, outputs) and reviewed by the CTO. Expected
to work is not PASS. Any unresolved security or authenticity failure is a
NO-GO.
