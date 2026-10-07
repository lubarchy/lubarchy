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
