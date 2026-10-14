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
- Package mutation boundary (M1-I contract, executor not implemented):
  untrusted or user request → LUBARCHY resolver and approved plan →
  transaction compiler and verification → privileged bounded package
  executor → APT/dpkg. Details:
  [package-transactions.md](package-transactions.md).
  - **Package-name authority:** names come only from a freshly recomputed
    production plan. An incoming plan or package list is never authority;
    callers may pass expected digests only.
  - **Stale-transaction defence:** any change to the inventory, rules, plan,
    installed state, holds, package-manager configuration or repository
    metadata makes the transaction stale. The executor must recompile
    immediately before mutation and fail closed on any difference.
  - **Exact internal binding:** the complete closure in the transaction is
    authoritative, but only direct requirements become explicit APT targets
    (`name:architecture=version`, built by the executor). A fresh simulation
    must reproduce the complete closure before any mutation. Callers can
    never supply versions, architectures, release selectors or options.
  - **Package marks:** APT automatic/manual state is part of transaction
    integrity. New direct requirements end manual and new dependencies
    automatic. Existing marks are never changed silently, and a mark change
    makes a prepared transaction stale.
  - **Environment sanitization:** fixed absolute binaries, an environment
    built from scratch (C locale, fixed `PATH`,
    `DEBIAN_FRONTEND=noninteractive`), no inherited `APT_CONFIG`, proxy or
    locale, `shell=False`. Dangerous APT options (`--allow-*`,
    `--force-yes`, `--fix-broken`, `-o`, `-c`, target release) are
    forbidden.
  - **Locks and concurrency:** one LUBARCHY-owned transaction lock, then
    the normal APT/dpkg locks. LUBARCHY never deletes package-manager locks
    and never kills another package manager.
  - **Root/administrator boundary:** these controls govern
    LUBARCHY-managed transactions. A root administrator acting directly on
    APT/dpkg is outside this boundary.
- Source activation and payload staging (M1-J; no system activation or
  apply implemented). Details: [package-staging.md](package-staging.md).
  - **Source authority:** only the accepted D-013 policy (official Debian
    archives, `main` + `non-free-firmware`, Debian archive keyring). URIs,
    suites, components and keyrings are never caller input. A `trusted`
    option, a foreign keyring, `contrib`, `non-free`, Backports, NVIDIA and
    third-party sources are rejected.
  - **Ownership:** LUBARCHY owns only
    `/etc/apt/sources.list.d/lubarchy-debian.sources` and never modifies a
    foreign source file. Ambiguous topologies fail closed: duplicates, mixed
    schemes, partial suites, unknown files, symlinks and malformed input.
  - **Authentication:** isolated metadata refreshes use an empty
    `trusted.gpg.d`, so only the `Signed-By` keyring authenticates. Every
    index target must be Debian, `Trusted` and keyring-signed. An absent
    target is accepted only if the signed `InRelease` lists it as empty.
  - **Payload integrity:** APT 3.0.3 does not re-hash pre-existing files in
    its archive cache, so LUBARCHY's bundle verification is authoritative:
    regular file, APT name, SHA-256 and size from the authenticated index,
    and `dpkg-deb` identity. It must run immediately before any future apply,
    against a root-owned location no unprivileged user can write.
  - **No weakened trust:** no unsigned or `trusted` local repository and no
    `dpkg -i`. The future offline apply uses APT with `--no-download`,
    direct targets only and the verified bundle as its archive cache, which
    preserves the direct/manual and dependency/automatic marks.
  - **Freshness:** a bundle is accepted only if the fresh transaction,
    source-activation and repository metadata digests equal its bindings.
    Otherwise it is restaged, so a superseded security update is never
    installed silently.
  - **Filesystem:** caller-owned workspaces outside every system path,
    `O_NOFOLLOW`/`O_NONBLOCK` opens, exclusive writes with fsync, atomic
    finalization and bounded entry-by-entry cleanup.

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
