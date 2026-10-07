# Decision Register

Baseline date: 2026-10-07.

This register lists accepted architecture decisions and pending decision
subjects. Significant new decisions receive ADRs under
[decisions/](decisions/README.md). Decisions D-001 to D-012 were accepted
before the ADR process existed and are recorded here directly.

## Accepted decisions

### D-001 — Debian Stable foundation

LUBARCHY is directly based on Debian Stable. Initial target: Debian 13
"Trixie", amd64.

### D-002 — Clean public history

The public Debian project starts cleanly. Historical experiments are not
imported by default.

### D-003 — Debian live-build for production ISO

Production images come from version-controlled `live-build` configuration.
Cubic is not part of the production pipeline.

### D-004 — APT/dpkg owns the host

APT/dpkg is the only host package authority. DNF/pacman may exist inside
isolated containers only.

### D-005 — Controlled package sources

Prefer Debian Stable, Security, Stable Updates, selective Backports, official
LUBARCHY packages, and Flatpak where appropriate. Avoid FrankenDebian.

### D-006 — Hyprland default

The primary future LUBARCHY desktop is a dedicated Hyprland session managed as
a qualified compatibility set.

### D-007 — Plasma officially supported

KDE Plasma is a separate supported alternative/fallback.

### D-008 — Hardware enablement first-class

LUBARCHY owns hardware detection, resolution, installation, and validation for
supported hardware.

### D-009 — Beginner installer

Target approximately five primary user-facing screens while retaining advanced
functionality.

### D-010 — Recovery is core

Snapshots, rollback, bootable recovery, diagnostics, and eventual factory reset
are architecture concerns.

### D-011 — UEFI-first

Modern UEFI is the primary target. The exact bootloader/Secure Boot
architecture remains undecided.

### D-012 — Configuration-as-code

Repository state, not a manually modified ISO/builder, is the authoritative
LUBARCHY description.

## Pending decision subjects

These are **not decided**. Each requires an ADR before acceptance.

### P-001 — Bootloader / boot architecture

Evaluate: Debian integration, UEFI, Secure Boot, recovery, dual boot,
rollback, maintainability.

### P-002 — Filesystem/subvolume layout

Btrfs preferred, pending validation.

### P-003 — Encryption architecture/default

### P-004 — Installer technology

### P-005 — Hyprland sourcing/version policy

### P-006 — Secure Boot ownership/key model

### P-007 — LUBARCHY repository infrastructure

### P-008 — Release cadence/support policy

### P-009 — Application delivery UX

## Decision process

1. Gather authoritative upstream evidence.
2. Identify requirements and constraints.
3. List viable alternatives.
4. Test empirical claims.
5. Document security and operational consequences.
6. Create/review ADR.
7. Update the decision register and [project-state.md](project-state.md).

Only the CTO accepts architecture decisions.
