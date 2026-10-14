# Architecture

This document records the **confirmed** architecture baseline only. Topics
marked TBD are pending decisions (see [decisions.md](decisions.md)) and must
not be treated as decided.

## Intent

LUBARCHY is a clean Debian Stable-based desktop distribution that delivers a
beginner-friendly install, automatic hardware enablement, and core recovery
capability, while remaining auditable and built entirely from
version-controlled configuration.

## Priorities

1. Correctness
2. Security
3. Data integrity
4. Hardware compatibility
5. Reliability
6. Recovery
7. Maintainability
8. Testability
9. Simplicity
10. Performance
11. UX
12. Delivery speed

When goals conflict, the higher priority wins unless the CTO decides
otherwise.

## Conceptual layers

| Layer | Content |
| --- | --- |
| OS foundation | Debian Stable, systemd, Linux kernel, firmware/microcode |
| System services | NetworkManager, PipeWire |
| Hardware enablement | Probe, classify, resolve, configure, validate |
| Desktop sessions | LUBARCHY Hyprland (default), KDE Plasma (supported) |
| Applications | APT packages; Flatpak where appropriate |
| Development environments | Podman/Distrobox containers |
| Installer | Beginner flow with advanced options |
| Recovery | Snapshots, rollback, bootable recovery, diagnostics |
| Build and release | `live-build`, manifests, checksums, qualification |

## Debian foundation

- Debian Stable; initial target Debian 13 "Trixie", amd64.
- Debian Security and Debian Stable Updates.
- Selected Debian Backports only where justified.
- systemd, Linux kernel, firmware and microcode, NetworkManager, PipeWire.

## Package authority

- APT/dpkg is the only host OS package authority.
- Other distribution package managers (for example DNF or pacman) may exist
  only inside isolated containers.
- No FrankenDebian: Debian Stable is never mixed with uncontrolled
  Testing/Unstable repositories.

## Package-source hierarchy

1. Debian Stable
2. Debian Security
3. Debian Stable Updates
4. Selected Debian Backports, where justified
5. Official, authenticated LUBARCHY packages (infrastructure TBD, P-007)
6. Flatpak, for appropriate GUI applications

Any other external repository requires a security review and a CTO decision.

Debian archive components: `main`, plus `non-free-firmware` for hardware
enablement ([ADR-0001](decisions/0001-non-free-firmware.md), D-013). `contrib`
and `non-free` are not authorised. The builder, live-build configuration and
CI still use `main` only; activating `non-free-firmware` in the ISO or
installed system is later integration work. The NVIDIA driver strategy,
including whether NVIDIA's own repository may become a D-005 exception, is
evidence in the Proposed [ADR-0002](decisions/0002-nvidia-driver-strategy.md)
(M1-G) and is not decided.

## ISO architecture

Production images come from version-controlled Debian `live-build`
configuration:

Git → package lists / hooks / includes / metadata → `live-build` → ISO →
manifests/checksums → VM qualification → release

The generated ISO is an output. It is never the source of truth.

## Desktop sessions (future)

- **Default:** LUBARCHY Hyprland, managed as a qualified compatibility set
  rather than following rolling upstream releases. Sourcing/version policy is
  TBD (P-005).
- **Supported fallback:** KDE Plasma.
- Hyprland and KWin are separate sessions. If Hyprland fails qualification on
  a configuration where Plasma is safe, the machine must remain usable with
  Plasma.

## Hardware pipeline

probe → normalized inventory → compatibility classification →
package/profile resolution → configuration → validation → persisted
diagnostic result

Since M1-A the data boundaries are defined as versioned contracts: the
inventory holds observed, privacy-safe facts only; declarative rules hold
policy; resolution produces a proposed plan that contains no commands and
enables no package source. Unknown hardware and rule conflicts fail closed.
Implemented boundary since M1-E: live read-only probe → inventory v1 →
production resolver → plan v1. Production rules classify hardware, map CPU
microcode and, since M1-E, map device firmware through qualified kernel
module aliases (device modalias → module alias → Debian firmware package from
`non-free-firmware`; since M1-F second-wave families also require the
module's declared firmware files to be shipped only by that package), all as
proposals. The rules are generated offline from
committed kernel alias evidence; nothing reads kernel metadata at runtime.
Since M1-I the path from proposal to package mutation is contracted:

plan proposal → transaction compilation (fresh probe, fresh resolve,
install-only APT change set bound to exact versions, architectures,
authorised Debian provenance, installed state including APT automatic/manual
marks, and repository state; only direct requirements become explicit APT
targets) → transaction verification → source-state verification and
activation plan (M1-J: one LUBARCHY-owned deb822 Debian source file realising
D-013, foreign source files never modified) → authenticated metadata
snapshot → exact payload acquisition → staged-bundle verification (SHA-256,
size, `dpkg-deb` identity; bound to the transaction, source-activation and
metadata digests) → final transaction revalidation → privileged apply
executor (M1-K, root only: LUBARCHY lock, durable journal, owned-source
activation, authenticated refresh, fresh transaction, root-owned staging,
immediate re-hash, one offline APT install of the direct targets) →
package/source/file validation → later device validation.

Package names come only from the freshly recomputed plan, never from a
caller. APT/dpkg installation is not atomic: the journal records phases, and
a failure after APT has started is never repaired automatically.
Interrupted-transaction recovery, device-level validation and the supplier
of privilege (installer/system service) are later work. See
[package-transactions.md](package-transactions.md),
[package-staging.md](package-staging.md),
[package-apply.md](package-apply.md) and
[hardware-enablement.md](hardware-enablement.md).

## Installer boundary (future)

Approximately five primary screens with advanced functionality retained.
Installer technology is TBD (P-004). See [installer.md](installer.md).

## Recovery boundary (future)

Recovery is core functionality and must not depend on Hyprland or Plasma
starting. Filesystem/subvolume layout is TBD (P-002). See
[recovery.md](recovery.md).

## Update qualification concept (future)

Updates are qualified before release: tested against defined hardware and
desktop configurations, tied to Git commits, with traceable provenance and a
recovery path. The update system design is a later milestone (M6).

## Unresolved topics

| Topic | Status |
| --- | --- |
| Bootloader / boot architecture | TBD (P-001) |
| Filesystem/subvolume layout | TBD (P-002); Btrfs preferred pending validation |
| Encryption architecture/default | TBD (P-003) |
| Installer technology | TBD (P-004) |
| Hyprland sourcing/version policy | TBD (P-005) |
| Secure Boot ownership/key model | TBD (P-006) |
| NVIDIA driver strategy | Evidence gathered (M1-G); ADR-0002 Proposed, not decided |
| LUBARCHY repository infrastructure | TBD (P-007) |
| Release cadence/support policy | TBD (P-008) |
| Application delivery UX | TBD (P-009) |

UEFI is the primary target (D-011); the exact bootloader and Secure Boot
architecture remain undecided.

## Change control

- Accepted decisions live in [decisions.md](decisions.md); significant new
  decisions require an ADR under [decisions/](decisions/README.md).
- Changing the architecture requires an accepted ADR and updates to this
  document, the decision register, and [project-state.md](project-state.md).
- The executor never accepts architecture decisions independently.
