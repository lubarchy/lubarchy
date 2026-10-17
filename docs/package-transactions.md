# Package Transactions

Status: **M1-I contract — PASS (CTO accepted) / CLOSED.** LUBARCHY can
compile and verify an install-only package transaction. At M1-I nothing
applied it. Since M1-K the root-only apply executor is the only component that
applies a freshly recompiled transaction ([package-apply.md](package-apply.md));
the M1-I compiler itself still installs, upgrades, removes and configures
nothing.

## Pipeline and authority

```
fresh local hardware observation (hardware/probe.py)
→ production resolver (hardware/resolver.py, hardware/rules/)
→ canonical plan v1 (a proposal)
→ transaction compiler (hardware/package_transaction.py)
→ transaction verification (recompile and compare)
→ source activation plan and staged payload bundle (M1-J, docs/package-staging.md)
→ privileged apply executor (M1-K, docs/package-apply.md)
→ APT/dpkg
→ later post-install validation
```

**Package names have one origin: the freshly recomputed production plan.**
- An incoming plan, a package list or any caller text is never authority.
  The compiler takes an inventory from the trusted probe and the
  source-controlled production rules, resolves them itself, and derives the
  direct requirements from that result.
- A caller may only supply *expected digests* (inventory, rule set, plan,
  transaction). A mismatch makes the transaction stale.
- The caller never controls package names, versions, architecture
  qualifiers, release or source selectors, APT or dpkg options, environment
  variables, source or preference files, or shell fragments.

Tests inject inventories and a fake package backend through the library API.
Production entry points cannot: `hardware/compile-transaction.py` always
uses the real probe, `resolver.load_production_rules()` and the system
package manager with fixed binaries, and it accepts no package, rules,
schema, binary or configuration arguments.

## Contract: `lubarchy.hardware.package-transaction/v1`

Schema: `hardware/schemas/package-transaction-v1.schema.json`. It is
separate from plan v1, which stays a declarative proposal. The inventory,
rule and plan v1 schemas are unchanged.

| Field | Security meaning |
| --- | --- |
| `architecture` | Native dpkg architecture; operations must be native or `all` |
| `inventory_digest`, `rule_set_digest`, `plan_digest` | Bind the transaction to the observed hardware, the production rules and the canonical plan |
| `plan_status` | `resolved` or `partial` only |
| `source_policy` | The only accepted Debian identities: Origin `Debian`; Label `Debian`/`Debian-Security`; codenames `trixie`, `trixie-updates`, `trixie-security`; components `main`, `non-free-firmware`; Signed-By the Debian archive keyring |
| `repository_state_digest` | Digest over the repository identities used and every selected package's name, architecture, version, SHA-256 and size |
| `installed_state_digest`, `installed_package_count`, `automatic_package_count` | Digest over the normalized installed state: name, architecture, version, dpkg status and APT automatic/manual mark (from `apt-mark showauto`), sorted. Not raw `/var/lib/dpkg/status` or `extended_states` bytes |
| `held_packages` | Sorted holds, including holds on not-installed packages |
| `package_manager_config_digest` | Digest over the security-relevant APT/dpkg configuration: hooks (`Pre-Invoke`, `Post-Invoke`, `Pre-Install-Pkgs`, …), recommends and suggests, `APT::Get::Allow*`, dpkg options and the insecure-repository flags |
| `direct_requirements` | Package and source from the recomputed plan |
| `operations` | The complete APT-selected change set (see below) |
| `unresolved` | Unresolved hardware targets from the plan, kept for diagnostics |
| `safety` | Constants of the install-only model: no recommends, no removals, zero upgrades/downgrades/removals/reinstalls/held changes/architecture changes, exact-version closure verified |

The core holds no timestamp, command string, raw output, secret, user, host
or network identity. Canonical JSON is byte-deterministic for identical
inputs, and the transaction digest is `sha256:` over those bytes.

Each operation records:
- the package, architecture and exact version;
- the action (`install` or `already_satisfied`);
- the relation (`direct` or `dependency`);
- the package mark: `automatic_before` (`null` for a new install) and
  `automatic_after`. A direct requirement ends manual (`false`), and an
  APT-selected dependency ends automatic (`true`);
- every authorised repository identity offering that version (origin,
  label, codename, suite, component, base URI);
- the SHA-256 and size from the signed Debian index.

## Compilation rules

1. **Plan acceptance.** The plan status must be `resolved` or `partial`
   (unresolved hardware never blocks independently proven requirements such
   as CPU microcode). Conflicts must be empty. Every package must be a
   `require` from an authorised source, with no `policy_review_required`
   status. The plan must carry the current production source policy.
   Profiles and warnings have no apply semantics yet, so they fail closed.
   `invalid` and `conflict` plans are rejected.
2. **Configuration.** Insecure-repository flags and
   `APT::Get::AllowUnauthenticated`/`Force-Yes` must be off.
3. **Candidates.** Every direct requirement needs a candidate in the
   configured repositories.
4. **Resolution evidence.** Run `apt-get --simulate --no-install-recommends
   --no-remove install <plan names>`.
   - Simulation proves resolution only. It takes no lock, so it says nothing
     about concurrency or apply safety.
   - Any upgrade, downgrade, removal, purge, reinstall, architecture change,
     held package or unresolvable result rejects the transaction.
   - A direct requirement already installed at its candidate version becomes
     `already_satisfied`.
5. **Provenance.** For every selected package, direct or dependency:
   - each repository offering the selected version must be an authorised
     Debian identity and component, `Trusted: yes`, Signed-By the Debian
     archive keyring (`apt-get indextargets`);
   - the signed index must give one SHA-256 and size;
   - a direct requirement must come from the component the plan declared.

   A package with the right name from the wrong origin, label, codename or
   component rejects the whole transaction.
6. **Exact binding with direct-only targets.** The complete closure in the
   transaction is authoritative, but dependencies need not all appear as
   explicit APT command-line targets.
   - The compiler builds explicit targets for **direct requirements only**:
     `name:architecture=version` (`name:all=version` for `Architecture: all`
     packages).
   - Verified on the builder's APT 3.0.3 (simulation only): both forms
     resolve, and a foreign qualifier (`:i386`, `:arm64`) is rejected with
     `Unable to locate package`. APT treats the native qualifier and `:all`
     interchangeably for native and `Architecture: all` packages, so the
     qualifier alone does not distinguish them. The architecture is bound by
     the closure comparison below, which checks the simulated architecture of
     every package.
   - APT then selects the dependencies itself. A second simulation of exactly
     those targets must reproduce the complete compiled closure: every
     package, architecture and version, install-only, nothing extra or
     missing.
   - Passing dependencies as explicit targets would make them manually
     installed and change their auto-removal semantics, so it is not done.
   - Dependency versions stay bound by the transaction and by revalidation;
     they never become caller input. Caller-supplied versions and
     architecture qualifiers are impossible.
7. **No package expressions.** Names must be plain Debian package names that
   appear in the plan and the simulated universe. No globs, APT patterns,
   regex fallback, `+`/`-` suffixes, `/release`, caller `:arch` or caller
   `=version`.
8. **Package marks.** APT's automatic/manual state is part of package state
   and therefore part of transaction integrity.
   - A newly installed direct requirement ends **manual**, and a newly
     selected dependency ends **automatic**. This is APT's normal behaviour
     for explicit targets versus resolver-selected packages.
   - Packages already installed keep their marks: an existing manually
     installed dependency stays manual and is not an operation.
   - A direct requirement that is already installed but marked automatic
     **fails closed**: the manual-mark transition is not supported yet, and
     marks are never changed silently. Any future marking phase must be a
     transaction-controlled, journaled step whose desired post-state is in
     the contract.
   - `--mark-auto` is not used, because it would also mark the direct
     requirement automatic.
9. **Recommends.** Never implicit (`--no-install-recommends`). A recommended
   package needed for supported hardware must become an explicit
   source-controlled requirement.

## Staleness

Immediately before any future mutation: fresh probe → fresh resolve → fresh
compile, which must reproduce the transaction byte for byte. Any of these
makes it stale, and stale fails closed:
- a hardware, rule-set or plan change;
- an installed-state, hold or automatic/manual-mark change;
- a dependency-closure change (version, architecture, extra or missing
  package);
- a package-manager configuration change;
- a repository-metadata change (identity, version, hash or size);
- a candidate change.

Debian mirrors move, so a transaction is not a historical snapshot: metadata
drift → stale → recompile.

## Privileged executor

Implemented in M1-K by `hardware/package_apply.py` /
`hardware/apply-packages.py`; see [package-apply.md](package-apply.md). It
realises the properties below, with one documented refinement: the executor
generates exactly one internal APT option (the attempt's
`Dir::Cache::archives`) together with `--no-download`. Caller-controlled
`-o`/`APT_CONFIG` stay impossible.

The future caller API is *apply the current qualified hardware plan*, never
*install packages X Y Z*. The privileged side re-observes and revalidates
everything. Required properties:

- Input: only a validated plan/transaction. Package names come from the
  validated plan, never from caller text.
- Caller input cannot inject package versions, source or release selectors,
  APT/dpkg options or environment variables.
- It rejects direct arbitrary package or version requests.
- It drives APT non-interactively through a bounded argv, never a shell.
  `package_transaction.apply_argv()` defines it:

  ```
  ["/usr/bin/apt-get", "install", "--yes", "--quiet", "--no-install-recommends",
   "--no-remove", "<direct-name>:<arch>=<version>", ...]
  ```

  Only direct requirements appear. The executor may run it only after a
  fresh simulation of exactly this request reproduces the transaction's
  complete closure, versions, architectures, actions and package marks.
  Any difference makes the transaction stale.

  These are forbidden: `--allow-unauthenticated`, `--allow-downgrades`,
  `--allow-remove-essential`, `--allow-change-held-packages`, `--force-yes`,
  `--fix-broken`, `-o`, `-c`, `-t`/`--target-release` and any caller
  option.
- Environment built from scratch: `LC_ALL=C`, `LANG=C`, `LANGUAGE=C`, fixed
  `PATH`, `DEBIAN_FRONTEND=noninteractive`. Never the caller's `APT_CONFIG`,
  `DEBIAN_FRONTEND`, `PATH`, proxy or locale.
- It revalidates the plan immediately before mutation and records the
  transaction and the validation result.
- No setuid binary, sudoers rule, broad polkit action or daemon exists or is
  proposed here. Each needs its own security review.

Hooks and configuration: the caller supplies no APT/dpkg hooks, invoke
commands or configuration files. System-owned configuration is trusted
system state, bound through `package_manager_config_digest`. Authenticated
Debian maintainer scripts are part of the trusted supply chain and are not
sandboxed.

### Concurrency

A future mutation takes one LUBARCHY-owned lock
(`/run/lubarchy/package-transaction.lock`), then lets APT and dpkg take
their own locks normally. LUBARCHY never deletes APT/dpkg lock files, never
kills another package manager and never bypasses locking. If another
transaction is active, it waits under a bounded policy or fails safely.
M1-I documents this lock and never creates it.

### State model

`prepared` → `revalidated` → `applying` → `applied` → `validated`, with
`rejected`, `stale` and `failed` as failure states. M1-I implements only
`prepared` (compile), `revalidated` (verify), `rejected` and `stale`.
APT/dpkg are not atomic. Interrupted installation and recovery are later
work.

## CLI

`hardware/compile-transaction.py` (read-only): `compile`, `verify
--transaction PATH|-`, `validate --transaction PATH|-`. There is no `apply`.

| Exit | Meaning |
| --- | --- |
| 0 | ready (compile/verify) or valid (validate) |
| 2 | input malformed |
| 3 | stale transaction |
| 4 | plan rejected or unauthorised source/configuration |
| 5 | unsafe APT change set |
| 6 | repository provenance failure |
| 7 | contract or internal failure |
| 64 | usage error |

On a system whose APT sources do not yet carry `non-free-firmware` (the
builder's `main`-only system configuration, for example), `compile` fails
closed with exit 6. Source activation is later architecture work.
