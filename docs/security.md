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
- Privileged package boundary (M1 requirement; implemented by the M1-K
  executor, [package-apply.md](package-apply.md)):
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
- Package mutation boundary (M1-I contract; executor implemented in M1-K,
  see [package-apply.md](package-apply.md)):
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
- Privileged apply executor (M1-K). Details:
  [package-apply.md](package-apply.md).
  - **Root only, no escalation:** the executor requires effective UID 0 and
    adds no sudo, setuid, sudoers, polkit rule or daemon. Privilege is
    supplied externally later.
  - **No caller authority:** the CLI accepts only `apply`/`status`, with no
    packages, versions, options, sources, paths or environment.
  - **Commands:** an exact command allowlist with absolute binaries, a
    literal environment (C locale, fixed `PATH`,
    `DEBIAN_FRONTEND=noninteractive`, `HOME=/root`) and `shell=False`. The
    only `-o` is LUBARCHY's own archive-cache option for the attempt.
  - **Concurrency:** one root-owned `flock` at
    `/run/lubarchy/package-transaction.lock`, never deleted. APT/dpkg locks
    are never touched.
  - **Freshness:** the final stale-state revalidation and an immediate
    re-hash of every staged payload run before APT, because APT 3.0.3 does
    not re-hash cached archives.
  - **Failure:** APT/dpkg is not atomic. A failure after APT has started is
    journaled and never repaired automatically, and later runs report
    `recovery_required`.
  - **Root/administrator boundary:** a root administrator acting directly
    on APT/dpkg remains outside this boundary.
  - **Trusted deployment:** root never executes LUBARCHY code from a tree
    that an unprivileged user can write. Code must come from a root-owned,
    unprivileged-nonwritable deployment, reached through non-writable
    ancestors without symlinks and derived content-addressed from the exact
    reviewed commit. It is verified before execution and launched with an
    empty environment and `python3 -I -B`. For qualification this is
    `/root/lubarchy-m1k/<commit>/`, created by root with `git`/`tar`;
    production will use package-owned paths.
- Interrupted transaction recovery (M1-L). Details:
  [package-recovery.md](package-recovery.md).
  - The real package state is authoritative and the journal is evidence.
    Attribution requires reconstructing the bound pre-state digest and
    re-verifying the original root-owned bundle.
  - The transaction closure and the recovery impact set are kept apart. A
    package outside the closure can be a recovery target only if durable
    evidence bound before `applying` (dpkg log cursor and prefix digest,
    pre-apply trigger-interest records) and the dpkg log after it prove that
    this exact transaction activated its trigger. Declared trigger interest
    alone, names, current state or timestamps never qualify.
  - The only recovery mutation is exact-package `dpkg --configure`/
    `--triggers-only`, and its targets must be an exact subset of the
    authorised set: non-final closure packages plus non-final attributed
    impact packages.
  - Exact target naming alone is not containment, because dpkg's configure
    also processes pending triggers. Recovery configures with the fixed
    internal `--no-triggers` option (qualified on the builder), and trigger
    processing is a separately authorised `--triggers-only` phase. Each
    command's own dpkg log delta and the re-inspected state must show no
    execution or change outside the authorised set. Recovery never broadens
    its own authorisation: trigger work it newly causes in another package
    is left unexecuted and ends in manual recovery. APT, downloads, `apt-get -f install`,
    `dpkg --configure -a`/`--pending`, upgrades, removals and mark changes are
    never used.
  - Unrelated pending or damaged packages, any version, architecture, mark
    or hold deviation, bundle or source drift, and untrusted or ambiguous
    journals all require manual recovery.
  - Recovery shares the transaction lock, never removes native dpkg locks,
    runs root only from the trusted deployment, and writes a durable
    recovery journal that gates later applies.
- Package state garbage collection (M1-N). Details:
  [package-garbage-collection.md](package-garbage-collection.md).
  - Only fixed LUBARCHY roots; no caller path, ID, age, size, force or plan.
    Deletion requires proof from the journals; unknown, corrupt, ambiguous,
    unresolved and manual-recovery state is retained. Journals are never
    deleted, and neither are APT/dpkg/source state, APT's lock files or
    receipts. Time and mtimes are never authority.
  - Root only, under the shared transaction lock; the plan is recomputed
    under the lock and each candidate re-verified (journal digests, object
    identity, exact listings) immediately before descriptor-relative,
    no-follow, non-recursive deletion. Symlinks, special files, foreign
    owners, group/other-writable objects, mount crossings and unexplained
    hard links are never deleted.
  - A durable receipt precedes the first deletion; a crash or partial
    failure is never reported as success; disk-full fails closed.
- Post-install hardware validation (M1-M). Details:
  [hardware-validation.md](hardware-validation.md).
  - It is read-only: no install, configuration, module load, binding,
    file write, repair or policy change. It reads sysfs/procfs and runs
    seven fixed read-only commands with a literal environment and
    `shell=False`. The CLI takes no device, package, driver, path, command
    or check input.
  - It is unprivileged by default. Root only adds the kernel log on
    restricted systems and the root-only apply journal. Without root these
    observations are `LIMITED`; `kernel.dmesg_restrict`, journal
    permissions, sudoers and Polkit are never changed, and no privilege
    mechanism is added.
  - Results never contain MAC addresses, serial numbers, disk UUIDs,
    hostnames, user names, IP addresses, raw kernel log lines, interface
    names or sysfs paths; the boot ID is stored only as a digest.
  - A result is bound to the inventory, rule set, plan, installed state,
    boot and profile; a stale result is never reused as a current PASS.
    A VM observation is never promoted to a physical-hardware claim, and
    NVIDIA drivers are reported `NOT_TESTED`.
  - Firmware request failures are evidence, not proof. Only a known-required
    file in the source-controlled policy fails; optional or unclassified
    ones are `LIMITED`, so fail-closed never produces a factually wrong FAIL,
    while independently observed device, driver and interface failures still
    fail. The policy is digest-bound and never caller supplied.
- Source activation and payload staging (M1-J; since M1-K used by the
  executor for the real system). Details:
  [package-staging.md](package-staging.md).
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
