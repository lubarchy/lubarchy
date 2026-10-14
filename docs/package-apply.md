# Privileged Hardware-Package Apply Executor (M1-K)

M1-K is the first LUBARCHY code that mutates the package manager. The
privileged operation is **apply the current qualified hardware plan**, never
"install package X".

**Status:** implemented locally; awaiting CTO review. The real mutation is
authorised only on the rollback-protected `lubarchy-builder` qualification
VM, which is restored to its original baseline afterwards. See
[testing.md](testing.md#builder-apply-qualification-m1-k-builder-only).

Supported policy is the accepted Debian hardware policy only (D-013): Debian
`main` + `non-free-firmware`, official archives, the Debian archive keyring.
Not supported: NVIDIA, `contrib`, `non-free`, Backports, a LUBARCHY
repository, third-party repositories and arbitrary packages.

| Piece | Location |
| --- | --- |
| Library | `hardware/package_apply.py` |
| CLI (root only) | `hardware/apply-packages.py apply` / `status` |
| Journal contract | `lubarchy.packages.apply-journal/v1` ([schema](../hardware/schemas/apply-journal-v1.schema.json)) |
| Static gate 11 | `tests/hardware-apply.py` (fake host; never mutates, needs no root) |

## Flow

```
LUBARCHY transaction lock
→ journal: prepared, locked
→ source activation (only /etc/apt/sources.list.d/lubarchy-debian.sources)   → source_activated
→ authenticated apt-get update (system)                                       → metadata_refreshed
→ fresh probe → production resolve → fresh package transaction               → transaction_revalidated
→ root-owned staging /var/lib/lubarchy/package-staging/<transaction-hex>/   → bundle_staged
→ final stale-state revalidation + immediate re-hash of every payload       → bundle_verified
→ journal "applying" (durable) → one offline APT install                     → applying, applied
→ dpkg --audit, apt-get check, exact package/mark/hold/source/file checks    → validated
→                                                                              complete
```

Any failure records `failed` with a code and the phase it was in.

## Privilege and API boundary

- The executor requires effective UID 0 (`not_root`, exit 77 otherwise).
  It never escalates privilege itself: no sudo, setuid binary, sudoers or
  polkit rule, no daemon. Privilege comes from a later installer or system
  service.
- **No caller input:**
  - The CLI accepts only `apply` or `status`.
  - It takes no package, version, architecture qualifier, URI, suite,
    component, APT or dpkg option, environment setting, staging root or
    source path.
  - Everything is derived from source-controlled policy and fresh local
    state.
  - Test fixtures reach the library only through an injected host object,
    never through the CLI.
- **Commands:**
  - Every command has an absolute path and comes from an exact allowlist:
    `apt-get update`, `apt-get check`, `dpkg --audit`, `dpkg-query
    --listfiles`, and the single apply argv.
  - The environment is a literal:
    - `PATH=/usr/sbin:/usr/bin:/sbin:/bin`
    - `LANG=LANGUAGE=LC_ALL=C`
    - `DEBIAN_FRONTEND=noninteractive`
    - `HOME=/root`
  - The runner calls `shell=False` with no stdin.
  - Caller `APT_CONFIG`, proxy, locale and `PYTHON*` variables are never
    inherited.
- **Root/administrator boundary:** these controls govern LUBARCHY-managed
  transactions. A root administrator acting directly on APT/dpkg is outside
  this boundary.
- **Trusted code only:** the executor itself must be executed from a
  root-owned, unprivileged-nonwritable deployment of the exact reviewed
  commit (see below), never from a user-writable checkout.

## Trusted deployment (privilege boundary)

A privileged process must never execute LUBARCHY code from a tree that an
unprivileged user can write. Before any privileged execution, every
LUBARCHY code or configuration file the executor loads must be in a trusted
deployment tree that is:
- owned by root and not writable by group or other;
- reached only through ancestor directories the caller cannot write, with
  no symlink substitution;
- derived content-addressed from the exact reviewed Git commit;
- verified before execution.

An unprivileged working clone is development input only, and root never
runs the apply entrypoint from it.

**Qualification deployment (M1-K).** This is not the product installation;
production will use package- or installer-owned system paths, and M1-K adds
no permanent install mechanism. Root creates
`/root/lubarchy-m1k/<full-commit>/` using system tools only, no LUBARCHY
code:
1. `git bundle verify` the copied bundle.
2. Bare clone it into `repo.git`, then `git fsck --full --strict`.
3. Check the commit and tree SHA.
4. Write `git ls-tree -r --full-tree <commit>` to `manifest.txt`.
5. Run `git -c tar.umask=0022 archive <commit>` and extract it with
   `tar --no-same-owner --no-same-permissions` into `tree/`. Git's default
   `tar.umask` of `0002` would make entries group-writable, and root's
   `tar` would keep those modes.

**Verification.** `tools/hwe/trusted-deployment.py verify`, run by root from
the deployment, records evidence and asserts:
- every ancestor up to `/` is a real root-owned directory, not group/other
  writable, and so is everything in the deployment;
- every file is regular, not a symlink;
- the file set equals the manifest exactly, with no untracked code;
- every Git blob hash and executable bit matches;
- `repo.git` resolves the commit to the expected tree and reproduces the
  manifest.

**Imports.** `imports` runs the interpreter exactly as the launch does,
from a hostile working directory, with `PYTHONPATH`/`PYTHONHOME` pointing at
decoy modules. Every executor module must load from `tree/`, and the
interpreter must be isolated, ignore the environment, skip the user site
and not write bytecode.

**Launch.** `run-apply` verifies, then execs
`/usr/bin/env -i /usr/bin/python3 -I -B <deployment>/tree/hardware/apply-packages.py apply`
from `/`. With `-B` nothing is written into the trusted tree.

These self-checks are evidence, not the boundary. The boundary is the
root-performed, content-addressed, root-owned deployment created before any
root execution.

## Transaction lock

`/run/lubarchy/package-transaction.lock`:
- The parent `/run/lubarchy` is root-owned, mode `0755`, and must not be a
  symlink.
- The lock file is regular, `0600` and root-owned, opened with `O_NOFOLLOW`.
- The lock itself is an exclusive non-blocking `flock`. If another
  transaction holds it, the executor fails with `busy` (exit 8).
- The lock file is never deleted.
- APT and dpkg then take their own native locks. LUBARCHY never removes
  package-manager locks, never kills another package manager and never uses
  force-lock workarounds.

## Journal and state machine

`/var/lib/lubarchy/package-transactions/<attempt>/journal.json`:
- The parent directory is root-owned `0700`, and the attempt id is 16
  random hex digits.
- Every phase change is written durably: a same-directory temporary file
  (`O_EXCL|O_NOFOLLOW`, `0600`), fsync, atomic rename, directory fsync. The
  in-memory journal advances only after the write succeeded.

The journal records:
- the attempt id, the phase, and each phase with its UTC time;
- the result and the failure code;
- whether APT had started;
- the source activation (action, model, pre- and post-state digests);
- the source-activation, repository-metadata, transaction and bundle
  digests, and the starting installed-state digest;
- the expected direct targets and complete closure;
- after validation, the normalized package result and whether a reboot is
  flagged.

It never holds command output, environment, secrets or caller text.
Timestamps are allowed here because the journal is a runtime record, not a
canonical resolution contract.

Phases: `prepared → locked → source_activated → metadata_refreshed →
transaction_revalidated → bundle_staged → bundle_verified → applying →
applied → validated → complete`, or `failed` from any phase. A plan with
nothing to install completes as `nothing_to_apply` without running APT.

**Pre-apply recovery evidence (M1-L).** The `bundle_verified` transition
also records `recovery_evidence`: the dpkg log cursor (size and SHA-256 of
`/var/log/dpkg.log` at that moment) and the package, architecture, version,
status and declaration digest of every installed package that declares dpkg
trigger interest. The journal refuses `applying` unless this evidence is
already durable. Recovery uses it to attribute trigger impact to the exact
transaction ([package-recovery.md](package-recovery.md#transaction-closure-and-recovery-impact-set)).

**No automatic resume.** A journal in a non-terminal phase, or a `failed`
attempt whose APT phase had started, makes every later invocation fail with
`recovery_required` (exit 9) before doing anything. A journal that cannot
be read fails closed the same way. Since M1-L, the explicit
`apply-packages.py recover` operation resolves such an attempt
conservatively ([package-recovery.md](package-recovery.md)). Apply stays
blocked until a recovery ends `complete` or `abandoned`.

**Bundle reuse.** An existing bundle of the same transaction is reused only
if it fully re-verifies against the fresh transaction, activation and
metadata digests. Otherwise, a stale bundle with the valid layout is removed
entry by entry and restaged. This is an M1-L refinement: without it, a retry
after a post-staging failure could never stage again.

## Source activation

- **Recompile:** the M1-J contract is compiled fresh from the real
  `/etc/apt`. Only the recognised topologies are accepted. Ambiguous,
  partial and unauthorised topologies fail before any command runs.
- **Write:** when activation is needed, only
  `/etc/apt/sources.list.d/lubarchy-debian.sources` is written. The
  rendering is written durably (root:root, `0644`), after checking that
  `sources.list.d` is a root-owned real directory.
- **Check:** the topology is re-inspected immediately and must show the
  contract post-state (`already_satisfied`), unchanged foreign files and
  the exact rendered bytes.
- **Never written:** `/etc/apt/sources.list` and every other foreign file.
- **On failure:** if activation fails, the owned file is restored to its
  exact pre-attempt state (removed, or its old bytes written back).
  Foreign files are never rolled back.
- **After activation:** later pre-apply failures leave the valid activated
  source in place.

## Metadata refresh and fresh transaction

- **Refresh:** `/usr/bin/apt-get update` runs with the literal environment.
  Any `W:`/`E:`/`Err:` line or a non-zero exit fails.
- **Metadata check:** the authenticated metadata must equal the contract
  post-state under the M1-J rules (Debian, `Trusted`, keyring-signed; empty
  indexes only as signed).
- **Fresh transaction:** only then are the probe, resolver and package
  transaction run, against the real system. Nothing compiled before
  activation is ever authority.
- **Transaction rules:** the M1-I rules apply: install-only, no upgrade,
  downgrade, removal, reinstall, architecture or held-package change, and
  authorised provenance only.

## Staging and immediate re-hash

- **Location:** payloads are staged into the fixed root-owned
  `/var/lib/lubarchy/package-staging/<transaction-hex>/`. Every path
  component must be a real directory, and the root must be root-owned and
  not group/other-writable. The staging location is never caller input.
  The M1-J staged-bundle contract and atomic finalization apply.
- **Final revalidation:** immediately before apply the executor recompiles
  the source-activation contract, the authenticated metadata and the
  transaction from fresh state, binding:
  - hardware inventory, rule set and plan;
  - installed state, automatic/manual marks and holds;
  - package-manager configuration;
  - source state and repository metadata;
  - the transaction itself.

  The source state and the transaction must be byte-identical, and the
  transaction, source-activation and metadata digests must match the bundle
  (`final_revalidation`). Anything else is stale and nothing is applied;
  restaging happens in a future invocation.
- **Apply cache:** the attempt's APT archive cache
  (`<attempt>/apply-cache/`) holds hard links to the bundle payloads. That
  is the same root-owned inodes: no copy, no download.
- **Immediate re-hash:** the bundle and the apply cache are verified
  **immediately before** `applying`:
  - regular file, APT name, size and SHA-256;
  - `dpkg-deb` package, version and architecture;
  - the exact closure with no extras;
  - the manifest and bundle digests, and the transaction and metadata
    bindings.

  APT 3.0.3 does not re-hash cached archives, so this verification is a
  hard security boundary. A mismatch aborts before APT.

## APT apply

```
["/usr/bin/apt-get", "install", "--yes", "--quiet", "--no-install-recommends", "--no-remove",
 "--no-download", "-o", "Dir::Cache::archives=/var/lib/lubarchy/package-transactions/<attempt>/apply-cache/",
 "<direct-name>:<arch>=<version>", ...]
```

- **No `dpkg -i`.** The install always goes through APT.
- **Targets:** only direct requirements are explicit targets, so they end
  manual. Dependencies are selected by APT and end automatic.
- **Offline:** `--no-download` guarantees no network retrieval during
  apply, and any `Get:` line fails the attempt. `apt-get update` never runs
  in the apply phase.
- **Internal options:** caller-controlled APT options are impossible. The
  one `-o` is generated by LUBARCHY for the attempt's cache directory. It is
  allowlisted by exact shape: a fixed option name, a path under the journal
  root and a 16-hex attempt id.
- **Forbidden:**
  - `--allow-unauthenticated`, `--allow-downgrades`,
    `--allow-remove-essential`, `--allow-change-held-packages`;
  - `--force-yes`, `--fix-broken`;
  - any other `-o`/`-c`, target releases;
  - recommends, reinstall, purge, autoremove.

## Validation

After APT exits 0:
- **Package-manager health:** `dpkg --audit` must be empty and `apt-get
  check` must succeed.
- **Exact installed state.** The post-state must equal the pre-state plus
  exactly the transaction closure: the exact versions and architectures,
  `install ok installed`, direct packages manual and dependencies
  automatic. Any extra package, removal, version change, downgrade,
  status, mark or hold change fails.
- **Source post-state:** the owned file has the exact rendered bytes and is
  root:root `0644`. The contract digest is unchanged and the foreign files
  are unchanged.
- **Package files:** every non-directory path that `dpkg-query --listfiles`
  lists for the installed packages exists.

The normalized result (package, architecture, version, automatic/manual,
dpkg status, relation) is journaled with `validated`. `complete` follows
only after it.

Package installation is not hardware validation. On the VM, M1-K proves
only package-level correctness, maintainer-script and initramfs completion,
and that the system boots afterwards. It does **not** prove that physical
CPU microcode was updated or that real Intel hardware works.

## Failure semantics and non-atomicity

**APT/dpkg installation is not atomic.** The journal is durable phase
evidence, not a transactional rollback.

- **Before `applying`:** a failure is journaled as `failed`. Nothing was
  installed. Only a failed source activation restores the owned source
  file.
- **After APT has started**, when APT exits non-zero or validation fails:
  - the executor journals `failed` with `apply_started: true` and stops;
  - it never runs `apt remove`, `purge`, `apt-get -f install`, dpkg repair
    or any rollback command;
  - every later invocation reports `recovery_required` until a future
    recovery procedure handles it.
- **If the journal itself cannot be written:** the attempt stops. The
  on-disk journal stays at its last durable non-terminal phase, so later
  invocations report `recovery_required`. A failed `applying` write means
  APT never ran.

## Exit status

| Code | Meaning |
| --- | --- |
| 0 | `complete` or `nothing_to_apply` |
| 2 | malformed state |
| 3 | stale (source, transaction or bundle) |
| 4 | unauthorised source or plan rejected |
| 5 | unsafe change set or unsafe path |
| 6 | payload, metadata or provenance mismatch |
| 7 | contract or internal failure (including source activation, metadata refresh, journal) |
| 8 | busy: another LUBARCHY transaction holds the lock |
| 9 | recovery required (or `recover`: manual recovery required) |
| 10 | APT apply, recovery or post-apply validation failed |
| 64 | usage |
| 77 | not root |

## Not in M1-K

- the operator user experience of manual recovery (M1-L implements
  automatic classification and bounded recovery);
- post-install device and function validation;
- staging and journal garbage collection;
- installer and system-service integration (the supplier of privilege);
- NVIDIA (ADR-0002 is Proposed);
- Secure Boot/MOK;
- hybrid graphics;
- diagnostics;
- real-hardware qualification.
