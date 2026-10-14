# Interrupted Package Transaction Recovery (M1-L)

M1-L adds a bounded, conservative recovery path for hardware-package apply
attempts (M1-K) that were interrupted or never completed validation.
Recovery never means "something failed, so run `apt-get -f install`". It
first proves what state the system is in.

**Status:** implemented locally; awaiting CTO review.

| Piece | Location |
| --- | --- |
| Library | `hardware/package_recovery.py` |
| CLI (root only) | `hardware/apply-packages.py recover`, plus `status` |
| Journal contract | `lubarchy.packages.recovery-journal/v1` ([schema](../hardware/schemas/recovery-journal-v1.schema.json)) |
| Static gate 12 | `tests/hardware-recovery.py` (fake host; never mutates, needs no root) |

## Principles

- **Priority:** data integrity, then package-database consistency, then
  exact transaction ownership, then security, then usability.
- **The journal is evidence, not truth.** The real dpkg/APT state is
  authoritative, and every observed change must be attributable to the
  original transaction.
- **Not a rollback.** APT/dpkg installation is not atomic. The apply
  journal records phases; recovery decides from real state what can be
  completed safely and refuses everything else.
- **Never repair or upgrade:**
  - no `apt-get -f install`;
  - no `dpkg --configure -a` or `--pending`;
  - no repair of unrelated packages;
  - no install, upgrade, downgrade, removal, purge or mark change;
  - no download.
- **No privilege escalation.** Recovery is root only and runs from the same
  root-owned, content-addressed deployment as apply
  ([package-apply.md](package-apply.md#trusted-deployment-privilege-boundary)).
  It is launched with `trusted-deployment.py run-recover`.

## Measured behaviour (Debian 13, APT 3.0.3, dpkg 1.22.21)

All of this was measured on the rollback-protected builder, with real
SIGKILL of only the qualification attempt's own process tree.
- **APT's two dpkg runs.** APT first runs `dpkg --no-triggers --unpack` of
  the closure, then `dpkg --configure --pending`. Both closure packages are
  unpacked and configured within one second. The initramfs-tools trigger
  then runs for about 5 seconds.
- **Marks first.** APT writes the automatic/manual marks to
  `extended_states` **before** dpkg runs.
- **Interrupted trigger.** Killing during the trigger leaves the closure
  `ii` with correct marks and `initramfs-tools` **half-configured**: outside
  the closure, but its trigger was activated by the transaction.
  `dpkg --audit` reports it and `apt-get check` exits 100.
- **Interrupted unpack start.** Killing at the start of `dpkg --unpack`
  leaves no package change. Only the auto-mark entry for the
  not-yet-installed dependency is present in `extended_states`.
- **Scope of each candidate,** with an unrelated pending package (`hello`
  unpacked):
  - an APT rerun of the exact direct target with `--no-download` and the
    verified cache would configure `hello`;
  - `apt-get -f install` would configure `hello`;
  - `dpkg --configure -a` would configure `hello`;
  - `dpkg --configure initramfs-tools` touches only `initramfs-tools`, then
    processes its own trigger.

  **Only exact-package dpkg completion is narrow in package scope**, and
  even that is not narrow in trigger scope: a plain configure processes
  pending triggers at the end of the run. See
  [Trigger execution boundary](#trigger-execution-boundary).
- **What the dpkg log records.** The transaction's dpkg.log delta (measured
  in the real S3 interruption) shows:
  - `install iucode-tool` → `status triggers-pending man-db` (man-db's
    file trigger, activated while the closure unpacks);
  - `configure intel-microcode` → `status installed intel-microcode` →
    `status triggers-pending initramfs-tools` (intel-microcode's postinst
    runs `dpkg-trigger --no-await update-initramfs`; initramfs-tools
    declares `interest-noawait update-initramfs`);
  - `trigproc man-db` → `status installed man-db`;
  - `trigproc initramfs-tools` → `status half-configured initramfs-tools`,
    then the kill.

## Transaction closure and recovery impact set

S3 showed that the transaction closure (`intel-microcode`, `iucode-tool`)
is not the complete set of packages whose dpkg state the transaction can
disturb: dpkg triggers make one package's operation run processing in
another installed package. Recovery therefore keeps two sets strictly
apart.

| Set | Meaning | Source of authority |
| --- | --- | --- |
| **Transaction closure** | The M1-I install closure, unchanged. | Apply journal `expected.closure`, equal to the re-verified staged bundle |
| **Recovery impact set** | Pre-existing packages outside the closure whose dpkg state the exact interrupted transaction disturbed through triggers. Not dependencies; never added to the closure. | Durable pre-apply evidence plus the dpkg log written after it |

Declaring trigger interest, a package name, a broken current state or a
timestamp never puts a package in the impact set.

### Pre-apply binding

Before the apply journal may enter `applying`, it durably records
`recovery_evidence` (in the `bundle_verified` transition, right after the
final bundle verification and the pre-apply package snapshot):
- `dpkg_log`: the size of `/var/log/dpkg.log` and the SHA-256 of exactly
  those bytes (the cursor);
- `trigger_interest`: for every installed package that declares dpkg
  trigger interest (`/var/lib/dpkg/info/<pkg>[:<arch>].triggers`), its
  package, architecture, version, dpkg status and the SHA-256 of its
  trigger declaration.

The journal refuses `applying` without this evidence. The field is new in
M1-L; an apply journal without it can still be read, but recovery then
cannot attribute anything and fails closed.

### Attribution rule

A package outside the closure is a member of the impact set only if all of
the following hold:
1. **Cursor intact:** the dpkg log is a root-owned regular file whose first
   `size` bytes still hash to the bound digest (so it was not truncated,
   rotated or rewritten), and the delta after it is complete text.
2. **Strict delta:** every line of the delta parses. Every package line
   belongs either to a closure package (`install <none> <v>`,
   `configure`/`trigproc <v>`, `status` with the exact closure version) or
   to a bound trigger-interest package. Any other package, operation,
   version or line (for example `conffile`) makes the whole delta
   unattributable.
3. **Caused by this transaction:** the package's first logged change is
   `status triggers-pending` (a trigger activation) at its bound version,
   after the transaction's first closure operation. Its later lines are
   trigger processing only (`trigproc`, `status triggers-pending`,
   `triggers-awaited`, `half-configured`, `installed`); `configure`,
   `install`, `remove` and anything else are refused.
4. **Healthy before:** its bound pre-apply status is `install ok installed`.
5. **Same package now:** its current package, architecture and version
   equal the bound record, its trigger declaration digest is unchanged, and
   its current dpkg status is exactly its last logged state.

In addition, each closure package's current status must equal its last
logged status. The recovery journal records the cursor, the delta's digest
and line count and, per impact package, its pre-apply and observed status,
the closure operation during which it was activated, its logged event
sequence and its declared trigger interests.

**Current Debian 13 case.** `initramfs-tools 0.148.4` (all) is eligible
because it was `install ok installed` with interest in `update-initramfs`
before apply, its first post-cursor line is
`status triggers-pending initramfs-tools:all 0.148.4` logged during
`configure intel-microcode:amd64`, its only later lines are
`trigproc` and `status half-configured`, and it is half-configured now.
`man-db` is in the impact set as well (activated during
`install iucode-tool:amd64`), but its trigger completed, so it needs no
recovery and is not a target.

## Classifications

| Classification | Condition (from real state) | Action |
| --- | --- | --- |
| `no_mutation` | APT never started (journal before `applying`). If the pre-state was bound, the state digest equals it, and dpkg/APT are healthy | `finalize`: bounded cleanup of attempt-local state; result `abandoned` |
| `no_package_change` | APT started, but no closure package is present and the state digest equals the bound pre-state | `finalize`; result `abandoned` |
| `applied_but_unvalidated` | The exact expected post-state is present, and dpkg/APT are healthy | `validate_only`: audit, check, exact state, files and source, then `complete`. **APT/dpkg is not re-run** |
| `bounded_incomplete_transaction` | dpkg was interrupted; every non-final package is a closure package in a configurable state or a member of the recovery impact set, and the pre-state reconstruction holds | `configure_exact`: exact-package dpkg completion, then full validation, then `complete` |
| `manual_recovery_required` | Anything else | No mutation; recorded with a reason |

### Pre-state reconstruction

The apply journal binds the pre-state only as a digest. Recovery proves
that nothing outside the two sets changed by reconstruction:
1. Take the current state (package, architecture, version, dpkg status,
   automatic/manual mark).
2. Drop the closure packages from it. This is only for the reconstruction:
   the transaction installs packages that were not installed before, so
   they were absent from the pre-state. A closure package that existed
   before apply (an upgrade) cannot be reconstructed this way and fails
   closed. The closure itself stays bound throughout: it is checked
   against the journal and the bundle (exact version, architecture, mark,
   state), it authorises its own targets, and the post-state validation
   requires it installed.
3. Replace each impact-set package's status by its bound pre-apply status.
   No other package is normalised, whatever its trigger declarations.
4. The result must hash to **exactly** the bound pre-state digest.

This proves that no package outside the closure and the impact set
changed version, architecture, mark, hold or state. Then:
- **Closure packages** must have the exact version, architecture and mark,
  in one of the states `installed`, `unpacked`, `half-configured`,
  `triggers-pending` or `triggers-awaited`.
- **Every other non-final package** must be in the impact set.

### Manual recovery is mandatory when

- **Unrelated packages:** an unrelated package is pending or damaged, or a
  package outside the closure changed.
- **Unattributable trigger state:** a package outside the closure is
  non-final but not proven to be in the recovery impact set (no bound
  record, no activation by this transaction, unhealthy before apply, a
  non-trigger operation, version/architecture/declaration drift, or a
  state different from its last logged state).
- **Evidence:** the pre-apply evidence is missing, the dpkg log is missing,
  not a regular root-owned file, truncated, rotated or rewritten before the
  cursor, or its delta contains unknown lines, unknown packages or
  unexpected closure operations.
- **Closure deviations:** a closure package has an unexpected version,
  architecture or mark; is `half-installed` or `reinstreq`; or is absent
  while other closure packages changed. Re-unpacking from a payload is not
  automated.
- **Holds:** a hold changed.
- **Bundle or journal mismatch:** the staged bundle is missing or corrupt, or
  disagrees with the journal (transaction, bundle, metadata, activation
  digests, expected closure).
- **Sources:** the source state no longer equals the transaction's
  activation (owned or foreign drift).
- **Journals:** an apply journal is unreadable or untrusted (fails closed
  as `recovery_required`), or more than one interrupted attempt exists.
- **No mutation, yet changes:** dpkg/APT is unhealthy, or the state
  changed, although no LUBARCHY package mutation started.

## Bounded completion

Immediately before any recovery mutation:
- **Bundle:** the original root-owned bundle
  (`/var/lib/lubarchy/package-staging/<transaction>/`) is re-verified:
  manifest, bundle and transaction digests, repository-metadata and
  source-activation bindings, every payload's SHA-256, size and `dpkg-deb`
  identity, no extras, ownership and modes.
- **Configuration:** the source state must equal the transaction's
  activation exactly, and the package-manager configuration must be
  fail-closed.

The authorised recovery set is:
- **A.** closure packages whose state is `unpacked`, `half-configured`,
  `triggers-pending` or `triggers-awaited`; and
- **B.** impact-set packages in one of those states.

Every recovery argv must name an exact subset of that set; anything else is a
`contract_failure` before any mutation.

### Trigger execution boundary

Naming exact targets is **not** sufficient containment on its own:
dpkg's `--configure <package>` also processes pending triggers at the end of
the run unless `--no-triggers` is given, so a plain configure of an
authorised package could execute another package's trigger handler.
Recovery therefore runs in two separately authorised, separately
contained phases, with the literal executor environment and
`shell=False`:

```
Phase A  ["/usr/bin/dpkg", "--configure", "--no-triggers", <unpacked/half-configured members of A and B>]
Phase B  ["/usr/bin/dpkg", "--triggers-only", <members of A and B that are then triggers-pending>]
```

- `--no-triggers` is a fixed internal option. The allowlist accepts only
  these two exact shapes followed by exact package names; plain
  `--configure <pkg>`, any other option or position, `-a` and `--pending`
  are rejected, and the CLI accepts no input at all.
- **Per command:** immediately before each command a dpkg log cursor is
  bound and journaled together with the argv. Afterwards the command's own
  log delta must show maintainer/trigger execution (`configure` in phase A,
  `trigproc` in phase B) only for that command's targets, and status
  changes only for authorised packages. The package state is then
  normalised again: every package outside the authorised set must be
  exactly unchanged, authorised packages keep their identity and mark, and
  holds are unchanged.
- **No dynamic broadening.** Phase B is computed from the re-inspected
  state, and only authorised packages can be in it. If a recovery command
  activates a trigger of a package outside the authorised set
  (recovery-induced impact, as opposed to impact of the original
  transaction), that package is left trigger-pending, its handler is
  never executed, the evidence is preserved in the journal, and recovery
  ends `manual_recovery_required`. Any logged execution or state change
  outside the authorisation also ends manual.
- **No loop.** If an authorised package is still not final after phase B
  (for example, its own trigger processing re-activated a trigger), the
  result is `manual_recovery_required`; there is no third command.

**Measured on the builder (real S3 state, Debian 13, dpkg 1.22.21):**
- Phase A `dpkg --configure --no-triggers initramfs-tools`: exit 0; output
  "update-initramfs: deferring update (trigger activated)"; log delta
  `configure initramfs-tools`, `half-configured`, `installed`,
  `triggers-pending initramfs-tools` and **no `trigproc`**; afterwards
  initramfs-tools is `triggers-pending` (pending `update-initramfs`, its
  own trigger, re-activated by its own postinst); no other package changed;
  `apt-get check` 0, `dpkg --audit` lists the pending trigger; the initrd
  is not yet regenerated (the interrupted mkinitramfs' 0-byte
  `initrd.img-*.new` and `.dpkg-bak` remain).
- `--no-triggers` alone is therefore not sufficient. Phase B
  `dpkg --triggers-only initramfs-tools` (an already authorised target)
  exits 0, logs only `trigproc initramfs-tools`, `half-configured`,
  `installed`, regenerates the initrd with the Intel microcode, removes
  the stale files, and leaves `dpkg --audit` and `apt-get check` clean.
- Both option orders (`--configure --no-triggers` and
  `--no-triggers --configure`) are accepted by dpkg; LUBARCHY uses only the
  first.

### After recovery

- **No APT:** APT is not run. The original transaction is completed from
  its authenticated, already unpacked payloads, so metadata that moved
  later can never substitute a newer version.
- **Locks:** dpkg takes its own native locks, and they are never removed.
- **Validation:** afterwards the state must equal exactly the reconstructed
  pre-state (impact packages back at their bound status) plus the closure
  (installed, with marks), `dpkg --audit` must be
  clean and `apt-get check` must succeed. Package files and sources must
  be unchanged.
- **On failure:** a failed command leaves the recovery `failed` (durable),
  and nothing else runs.

## Recovery journal, lock and gating

- **Journal:** `/var/lib/lubarchy/package-recoveries/<id>/journal.json`
  (root, `0700`/`0600`). It is written as temp file, fsync, atomic rename,
  directory fsync; memory advances only after the durable write. It binds:
  - the original attempt and its digests;
  - the observed state (state digest, pending packages, audit/check, whether
    the pre-state was reconstructed);
  - the classification, reason and action;
  - the transaction closure, and separately the recovery impact set with
    its evidence;
  - the authorised targets, the exact recovery argv, the per-command
    evidence (log cursor, delta digest and line count, executed handlers,
    authorised packages still pending), the post-state and the result.

  Phases: `prepared`, `locked`, `inspected`, `classified`, `verified`,
  `recovering` (phase A), `processing_triggers` (phase B), `recovered`,
  `validated`, then `complete`, or `abandoned` /
  `manual_recovery_required` / `failed`. It contains no command output,
  environment or caller text.
- **Lock:** recovery takes the same `/run/lubarchy/package-transaction.lock`
  as apply, so apply and recovery never run concurrently.
- **Interrupted recovery:** a recovery journal left non-terminal is closed
  as `failed` (`interrupted`), and the real state is re-inspected. If the
  interrupted or failed recovery's own dpkg run already logged activity
  (for example `configure initramfs-tools`), that activity is not a trigger
  operation of the original transaction, so the re-inspection requires
  manual recovery. A recovery that stopped before dpkg logged anything is
  re-attempted normally.
- **Gating:** an apply attempt is resolved only by a `complete` or
  `abandoned` recovery. Apply refuses with `recovery_required` while any
  attempt is unresolved, or while any recovery is non-terminal, `failed` or
  `manual_recovery_required` without a later resolution. Original apply
  journals are never erased.

## Policies

- **Owned source after an abandoned attempt.** The LUBARCHY-owned Debian
  source file activated by an attempt that never reached APT is
  **retained**. It is exactly the D-013 policy rendering, re-activation is
  idempotent, and foreign sources are never touched.
- **Cleanup.** Abandonment removes only attempt-local state:
  - the attempt's apply cache, checked entry by entry (payload file names,
    APT `lock`, an empty `partial/`);
  - incomplete `.staging-*` directories.

  Anything unexpected aborts as `manual_recovery_required`. Journals and
  finalized bundles are kept.
- **Bundle reuse (M1-K refinement).** A later apply reuses an existing
  bundle of the same transaction only if it fully re-verifies against the
  fresh transaction, activation and metadata digests. A stale bundle (for
  example, moved metadata) is removed entry by entry and restaged.

## CLI

`apply-packages.py recover` accepts nothing else: no package, version,
option, attempt or classification.

Exit codes:
- `0`: `complete`, `abandoned` or nothing to recover;
- `9`: manual recovery required (or an unreadable journal);
- `10`: the recovery failed;
- `8`: busy;
- `77`: not root;
- `64`: usage.

## Not in M1-L

- operator user experience for manual recovery;
- garbage collection of journals and staging;
- re-unpacking a half-installed closure package from the verified payload;
- installer and service integration;
- device-level validation.

The qualification VM checkpoint used to exercise these paths protects only
the test environment. It is not the product recovery architecture.
