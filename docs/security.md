# Security

Security baseline for LUBARCHY. This document states goals and requirements.
Unless explicitly stated otherwise, nothing described here is implemented yet.

## Goals

- Software authenticity
- Safe installation
- Least privilege
- Boot integrity
- Data protection
- Recovery integrity
- Minimal exposed services
- Auditable updates
- Supply-chain control

## Initial threat model

Initial threats in scope:

- tampered or substituted installation media or packages;
- compromised or unauthenticated package sources;
- compromised build inputs or builder environment;
- accidental leakage of signing keys or credentials;
- destructive installer mistakes affecting user data or other operating
  systems;
- network-exposed services reachable by default;
- tampering with boot components (design TBD);
- loss or theft of a device containing user data (encryption design TBD);
- failed or malicious updates leaving a system unbootable.

The threat model will be refined as the architecture develops.

## Package and repository security

- Use authenticated Debian repositories only.
- Future LUBARCHY repositories and packages must be authenticated
  (infrastructure TBD, P-007).
- External repositories require a security review and CTO decision.
- Fail closed on signature or authenticity failures.
- No production `curl | sh` installation paths.
- NVIDIA drivers (M1-G evidence,
  [ADR-0002](decisions/0002-nvidia-driver-strategy.md), **Proposed**): no
  NVIDIA source, key or package is authorised. Debian Stable's
  `nvidia-graphics-drivers` 550 has 93 open security issues in trixie with no
  DSA and must not become an automatic default. NVIDIA's own repository would
  need a bounded D-005 exception: a dedicated key, an allowlist, branch
  pinning and a compensating control for its missing `Valid-Until`. It also
  overrides Debian's `dkms` unless pinned away.
- External-repository containment model (M1-H evidence, inactive): a signed
  repository is not enough. Containment requires all of:
  - the expected full signing fingerprint;
  - the exact repository identity (Origin/Label);
  - a default deny for that origin;
  - a finite, exact binary allowlist;
  - a branch lock;
  - protection of Debian infrastructure (for example `dkms`), where a deny
    record placed before the allowlist wins;
  - requalification of every update;
  - rollback and freshness controls. The repository lacks `Valid-Until`, so
    a monotonic signed Release Date is enforced, and absolute freshness needs
    a future policy.

  These controls govern APT candidate selection: what normal
  install/upgrade/dist-upgrade resolution can choose. APT preferences are
  not a sandbox or MAC mechanism against root. In APT 3.0.3 an explicit
  exact-version administrator request (`apt install pkg=version`) overrides
  candidate selection for the named package. That is an intentional
  privileged override, outside the repository-containment boundary, and it
  does not weaken containment of LUBARCHY-managed automatic transactions.
  The vendor's pinning package has no default deny and must not own the
  trust boundary. Key material is never committed for this evidence, and
  `trusted=yes` is never used.
- Privileged package boundary (future M1 requirement, not implemented):
  untrusted or user request → LUBARCHY resolver and approved plan →
  privileged bounded package executor → APT. The executor:
  - accepts only a validated plan;
  - takes package names from the plan, never from caller-controlled text;
  - never lets caller input inject versions, source or release selectors, or
    arbitrary APT options;
  - rejects direct package or version requests;
  - drives APT non-interactively through a bounded API or command
    construction;
  - revalidates the plan immediately before mutation;
  - records the transaction and the validation result.

## Build security

- Build inputs come from version-controlled configuration.
- Document builder prerequisites.
- Verify upstream artifacts cryptographically before use.
- Generate manifests and checksums for build outputs.
- Tie releases to Git commits; release and package provenance must be
  traceable.
- Never commit signing keys or private keys.

## Installer security requirements

- No ambiguous destructive targets; robust disk identity.
- Revalidate disk topology before committing changes.
- Preserve foreign EFI data; never silently overwrite another OS.
- Handle credentials safely; do not log secrets.

See [installer.md](installer.md).

## Boot security

- UEFI-first (D-011).
- Bootloader architecture: TBD (P-001).
- Secure Boot ownership and key model: TBD (P-006). **Secure Boot is not
  implemented.**
- DKMS-built third-party kernel modules (for example NVIDIA's) need MOK
  enrollment under Secure Boot. No such driver may be automatic before P-006
  is decided, and disabling Secure Boot is not an accepted workaround
  (ADR-0002, Proposed).

## Encryption status

Encryption architecture and default: TBD (P-003). **Encryption is not
implemented.**

## Privilege model

- Least privilege for services and users.
- Administrative actions require explicit authentication.
- Hardware enablement and recovery tools must not grant broader privileges
  than needed.

## Network exposure policy

- No unnecessary listening services by default.
- Any default listening service requires justification and review.

## Future desktop security areas

To be addressed in M2 and later:

- session isolation between Hyprland and Plasma configurations;
- screen locking and authentication;
- privilege prompts;
- application sandboxing for Flatpak applications.

## Recovery security

- Recovery tools must not bypass authentication or encryption.
- Snapshots and rollback must not expose data to unauthorised users.
- Recovery images must be authenticated like other release artifacts.

See [recovery.md](recovery.md).

## Update authenticity

- Updates come only from authenticated sources.
- Update qualification is traceable to Git commits and build manifests.
- Failed verification aborts the update.

## Testing expectations

Security testing is part of qualification. See [testing.md](testing.md).

## Vulnerability handling

LUBARCHY must eventually define a vulnerability-handling process covering
reporting, triage, fixes, disclosure, and Debian Security tracking. This is
not yet defined.
