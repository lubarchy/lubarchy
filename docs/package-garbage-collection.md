# Package State Retention and Garbage Collection (M1-N)

M1-N reclaims LUBARCHY-owned package payload state that is **proven**
obsolete, without destroying evidence needed for apply recovery, manual
recovery, validation or audit. It is not generic filesystem cleanup.

**Status:** implemented locally; awaiting CTO review.

| Piece | Location |
| --- | --- |
| Library | `hardware/package_gc.py` |
| CLI (root only) | `hardware/apply-packages.py gc-plan` and `gc` |
| Contracts | `lubarchy.packages.gc-plan/v1` ([schema](../hardware/schemas/gc-plan-v1.schema.json)), `lubarchy.packages.gc-receipt/v1` ([schema](../hardware/schemas/gc-receipt-v1.schema.json)) |
| Static gate 14 | `tests/hardware-gc.py` (fake host roots only) |

## Ownership boundaries

GC operates only on fixed, source-controlled roots and never accepts a path:

| Root | Contents | GC role |
| --- | --- | --- |
| `/var/lib/lubarchy/package-staging/` | finalized bundles (`<transaction hex>/`) and temporary staging directories (`.staging-XXXXXXXX`, from `tempfile.mkdtemp`) | primary reclaim target |
| `/var/lib/lubarchy/package-transactions/<attempt>/` | `journal.json` (apply journal) and `apply-cache/` (hard links to the bundle's payloads, APT's `lock`, `partial/`) | journals read only; cache payload links reclaimed with their bundle |
| `/var/lib/lubarchy/package-recoveries/<recovery>/` | `journal.json` (recovery journal) | read only |
| `/var/lib/lubarchy/package-gc/` | GC receipts | written, never collected |

GC never touches:
- `/var/cache/apt/archives`, `/var/lib/dpkg`, `/var/lib/apt`, `/etc/apt` or
  any source file;
- installed packages, marks or holds;
- native APT/dpkg lock files. The `lock` file APT creates in an attempt
  cache is preserved too.

Entries in the staging root that are neither bundles nor exact
product-pattern temporary directories are reported as `unrecognized` and
preserved.

## Reference graph (from the existing code)

- **Apply journal** (`apply-packages.py apply`). It binds
  `digests.transaction_digest`, which is the bundle directory name
  (`package_staging.bundle_name`), and `digests.bundle_digest` (the
  manifest). Its `apply-cache/` hard-links that bundle's payloads; the
  cache is not removed after a successful apply.
- **Bundle reuse.** A later apply reuses an existing bundle only if it fully
  re-verifies. Otherwise apply removes it and restages, so an absent bundle
  only means a fresh download.
- **Recovery journal** (`apply-packages.py recover`). It binds
  `original.attempt_id` and its digests. Bounded recovery re-verifies the
  original bundle only for an **unresolved** attempt whose APT run started.
  A `complete` or `abandoned` recovery resolves the attempt.
- **Validation** (M1-M) reads only the latest complete apply journal's
  `post_state` (exact versions) and never the bundle. `check-current` and
  `revalidate` use no payload.
- **Temporary staging directories** exist only between the start of staging
  and the atomic rename to the bundle name, which happens before the
  attempt's `bundle_staged` phase. Only a nonterminal attempt still at or
  before `transaction_revalidated` can own one.

## Retention classifications

Every bundle is classified from **every** apply journal that references
its transaction digest; the strongest protection wins:

| Classification | When | Deleted |
| --- | --- | --- |
| `protected_active` | a referencing attempt is nonterminal (`nonterminal_apply`), or any recovery journal is nonterminal (`nonterminal_recovery`), or a temporary directory may belong to a nonterminal staging attempt (`temporary_possibly_owned`) | never |
| `protected_recovery` | a referencing attempt failed after APT started without resolution (`recovery_required`), or its recovery ended `manual_recovery_required` or `failed` | never |
| `protected_unknown` | no journal references the bundle (`unreferenced`), or any journal is unreadable, malformed or unexpected (`reference_graph_incomplete`) | never |
| `eligible_complete` | every reference is a complete, validated attempt (`complete_validated`) or one resolved by a complete recovery (`resolved_by_recovery`) | yes, after re-verification |
| `eligible_abandoned` | every reference is a proven no-mutation attempt: failed before APT started (`failed_before_apply`) or resolved by an abandoned recovery (`resolved_without_mutation`); or a safe temporary directory (`abandoned_temporary`) | yes, after re-verification |
| `invalid_or_ambiguous` | an eligible candidate fails verification (`bundle_verification_failed`, `binding_mismatch`, `apply_cache_unexpected`, `hard_link_ambiguity`, `unsafe_object`) | never |

**Deletion requires proof.** The following are all preserved for diagnosis:
- unknown, corrupt, ambiguous and recovery-relevant state;
- a missing or malformed reference (never treated as permission to
  delete);
- a corrupt bundle (`invalid_or_ambiguous`), even when it belongs to a
  complete attempt.

**Why complete is safe, and "complete" alone is not enough.** No supported
operation needs the payload of a complete, validated attempt:
- recovery only re-verifies bundles of unresolved attempts;
- validation and status read journals;
- apply would restage.

Even so, a bundle is eligible only if **all** of its references are
terminal-safe, its integrity re-verifies, and every hard link to its
payloads is accounted for.

## Journal retention policy

Apply and recovery journals are **never deleted** in M1-N. They are small,
security- and audit-relevant, and part of the recovery and validation
evidence. GC parses and validates them to build the reference graph and
records their digests in the plan. Archival or compaction of journals would
be a separate, separately qualified change. GC receipts are not collected
either.

## Eligible candidate verification

**Bundle.** Before a bundle is eligible:
- `package_staging.verify_bundle` must succeed: layout, canonical manifest,
  bundle digest, the name equal to the transaction digest, every payload's
  SHA-256, size and `dpkg-deb` identity, no extras, owner and modes;
- the manifest's bundle digest must equal every referencing journal's
  `bundle_digest`;
- each referencing attempt's `apply-cache` may contain only that bundle's
  payloads (the same inode), APT's `lock` and an empty `partial/` of the
  exact APT sandbox shape (below);
- each payload's link count must equal 1 plus the number of attempt caches
  linking it; any other link is `hard_link_ambiguity`;
- every object must be a real directory or regular file (no symlink,
  FIFO, device or socket), owned by root, not group/other writable, and on
  the same device as the root (no mount crossing). The single exception is
  the APT sandbox `partial/` directory below.

**APT sandbox `partial/` (M1-N correction).** APT acquires through its
sandbox. Before acquiring, it changes its archive cache's `partial/`
directory to the sandbox user. On Debian 13 (APT 3.0.3) this is the system
user `_apt`, with group root and mode `0700`; the builder measured this for
a real LUBARCHY apply. The original M1-N treated that legitimate state as
`unsafe_object`, so no really applied bundle could ever be collected.

LUBARCHY keeps Debian's normal APT sandbox. It sets no
`APT::Sandbox::User`, disables nothing and adds no APT configuration.
Instead, GC accepts exactly one non-root object:
- **Path:** exactly `<attempt>/apply-cache/partial/`, a real directory (no
  symlink, FIFO, socket, device or regular file) on the same filesystem.
- **Owner, group, mode:** exactly the `_apt` user, group root, mode exactly
  `0700`.
- **Contents:** empty when classified, and again immediately before its
  non-recursive `rmdir`.
- **Lifecycle:** only in the cache of an attempt whose APT run started
  (`apply_started`). Where APT never ran, the executor's own root-owned
  `0700` `partial/` is the only accepted shape. A root-owned `partial/` in
  a cache APT used is retained, and so is an `_apt` one where APT never
  ran.
- **Sandbox identity:** the `_apt` UID is read only from the host's own
  `/etc/passwd`, which must be a root-owned, not group/other-writable
  regular file opened without following links.
  - It must contain exactly one `_apt` entry.
  - The UID must be a system UID (1–999), not root's, and used by no other
    entry.
  - Otherwise the identity is unresolvable and the candidate is retained.

  No UID is guessed or defaulted, and neither the caller nor the
  environment can supply one.
- **Recheck before removal:** owner, group, mode, identity and emptiness are
  re-read immediately before removal. A change after classification aborts
  before any unlink, with a `failed` receipt.

All other GC-owned objects keep the root-ownership requirement unchanged:
- the bundle, its manifest and archives;
- payloads and cache payload links;
- the apply-cache directory itself;
- APT's `lock`;
- temporary staging directories.

Unknown ownership or configuration fails closed. The parent apply cache,
the transaction-lock requirement, the terminal and eligible lifecycle, and
every identity, listing and race check are unchanged.

**Temporary directory.** It must match `.staging-[a-z0-9_]{8}` exactly, be
owned by root and not group/other writable, and contain only `archives/`
with regular files and an optional `manifest.json`. Look-alike names are
`unrecognized`.

## Time is not authority

GC never uses wall-clock time, file ages or mtimes. Retention comes only from
journal state and verified object identity, so clock changes cannot affect
deletion safety. There is no "older than N days" mode.

## Lock, plan and TOCTOU

- **Shared lock.** `gc-plan` and `gc` take the same
  `/run/lubarchy/package-transaction.lock` as apply and recovery. GC, apply,
  recovery and another GC never run concurrently; a busy lock exits 8.
  Native APT/dpkg locks are never touched.
- **Plan under the lock.** `gc` recomputes the plan itself under the lock.
  A plan is never accepted as input; `gc-plan` exists only to show it.
- **Revalidation per candidate.** Immediately before deleting each
  candidate, GC:
  - re-reads the digests of the referencing journals; any change gives
    `binding_mismatch`;
  - opens every directory with `O_NOFOLLOW` through directory descriptors;
  - compares each object's identity (device, inode, type and, for files,
    size) with the verified one;
  - requires exact directory listings.

  An extra file, a replaced, substituted or symlinked object, a vanished
  candidate or a changed cache aborts **before** that candidate's first
  unlink.
- **Deletion.** It is entry by entry with `unlinkat`/non-recursive `rmdir`;
  there is no recursive delete.

## Receipts, partial failures, idempotence, disk full

- **Receipt timing.** A durable receipt (temporary file, fsync, atomic
  rename, directory fsync) is written with phase `deleting` **before** the
  first deletion. It is rewritten after each removed candidate and
  finalized as `complete`, `partial` or `failed`. A crash leaves `deleting`,
  never a success.
- **First failure.** On the first failing candidate GC stops. That
  candidate is `failed` with a reason, later ones are `not_attempted`, and
  the result is `partial` (if something was removed) or `failed`; the exit
  status is 10. Nothing is retried automatically. The next run re-inspects
  from scratch: a half-removed bundle then fails verification and is
  retained as `invalid_or_ambiguous`.
- **Idempotence.** A second run without changes finds nothing eligible and
  reports `nothing_to_collect` (exit 0) without writing a receipt.
- **Disk full.** If the receipt cannot be written durably, GC fails before
  deleting anything. There is no emergency cleanup mode; recovery evidence
  is never sacrificed for space.

## Privilege

- **Root only.** `gc-plan` and `gc` require root, because the journals are
  root-only (0700/0600).
- **Qualification launch.** In qualification they run only from the
  root-owned exact-commit deployment (`trusted-deployment.py run-gc-plan` /
  `run-gc`).
- **No caller input.** The CLI accepts nothing else: no path, attempt or
  recovery ID, age, count, size, file name, digest, delete list, force or
  recursive option, or plan.
- **No new mechanism.** There is no new sudoers rule, Polkit rule or
  daemon.

## Qualification

Real GC is qualified only on the builder VM, under a dedicated rollback
checkpoint (`pre-m1n`). It runs against a qualification-only fixture
created by root under the production roots; the fixture is not part of the
product or the CLI. The VM checkpoint protects only the test environment; it
is not part of the product's retention design. See
[testing.md](testing.md).
