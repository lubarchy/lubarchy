# Hardware Enablement Orchestration and Persistent State (M1-O)

M1-O adds the production coordinator. It composes the already qualified
hardware-enablement primitives into one durable, resumable, idempotent
operation:

> converge the current machine toward its current source-controlled
> qualified hardware plan, or stop safely with a precise state.

**Status:** PASS (CTO accepted) / CLOSED (implementation `4fc443d`, `Static gates` run #40; qualification docs `03be7dc`, run #41).

| Piece | Location |
| --- | --- |
| Library | `hardware/hardware_orchestration.py` |
| CLI (root only) | `hardware/manage-hardware.py status` and `reconcile` |
| Contract | `lubarchy.hardware.orchestration-journal/v1` ([schema](../hardware/schemas/orchestration-journal-v1.schema.json)) |
| Qualification launch | `tools/hwe/trusted-deployment.py run-hwe-status` / `run-hwe-reconcile` |
| Static gate 15 | `tests/hardware-orchestration.py` (fake host roots only) |

M1-O does **not** build:
- an installer UI or desktop diagnostics;
- a systemd service, timer, socket, D-Bus service or daemon;
- NVIDIA production support or Secure Boot/MOK;
- automatic reboot;
- a new privilege mechanism.

Those future components may invoke `reconcile` and `status`.

## Authority

The coordinator is **not** a second resolver, transaction engine, recovery
engine, validator or collector. Every primitive stays authoritative for its
own domain:

| Step | Authority (unchanged) | What the coordinator does |
| --- | --- | --- |
| Observation | `probe.Probe` (M1-B) | calls it; persists the canonical inventory |
| Planning | `resolver.resolve` with the production rules (M1-C) | calls it; persists the canonical plan; `pt.accept_plan` decides policy |
| Package precheck | `package_transaction.compile_transaction` (M1-I), read-only | decides only "a change is needed" vs "nothing to change" |
| Package mutation | `package_apply.apply_current_plan` (M1-K) | invokes it; re-reads its durable journal |
| Recovery | `package_recovery.recover_interrupted_transaction` (M1-L) | invokes it first when legal; re-reads its journal |
| Validation | `hardware_validation.evaluate` + `RealRuntime` (M1-M) | invokes it; persists the exact result |
| GC | `package_gc.collect_garbage` (M1-N) | invokes it only in an eligible terminal state |

The coordinator decides only which primitive runs next, whether execution
may continue, and which orchestration phase and outcome to record. It never:
- builds an APT or dpkg argv or names a package;
- deletes or renames anything itself;
- reboots, shuts down or schedules either;
- treats its own earlier phase as proof that a package operation happened.

## Primitive map (from the repository)

| Primitive | Entry point | Lock | Durable record | Result states |
| --- | --- | --- | --- | --- |
| Apply (M1-K) | `pa.apply_current_plan(host)` | package lock | `/var/lib/lubarchy/package-transactions/<attempt>/journal.json` | `complete` (result `complete` or `nothing_to_apply`), `failed`; nonterminal phases need recovery |
| Recovery (M1-L) | `pr.recover_interrupted_transaction(host)` | package lock | `/var/lib/lubarchy/package-recoveries/<id>/journal.json` | `nothing_to_recover`, `complete`, `abandoned`, `manual_recovery_required`, `failed` |
| Validation (M1-M) | `hv.evaluate(inventory, rules, evidence, previous)` | none (read-only) | none (the coordinator persists the result) | `PASS`, `FAIL`, `LIMITED`, `NOT_TESTED`; boot relation `same_boot`/`post_reboot`/`not_applicable` |
| GC (M1-N) | `gc.collect_garbage(host)` | package lock | `/var/lib/lubarchy/package-gc/<id>.json` | `nothing_to_collect`, `complete`, `partial`, `failed` |

Facts the design relies on, measured in the code:
- Apply always activates the owned source and runs `apt-get update` before
  compiling. The read-only precheck therefore lets an already converged
  machine skip APT entirely.
- An apply attempt in a nonterminal phase, or failed after APT started, is
  unresolved until a `complete` or `abandoned` recovery names it. Apply
  refuses to start while one exists (`recovery_required`).
- A `manual_recovery_required` or `failed` recovery of an unresolved attempt
  blocks apply. Recovery never retries automatically; an operator may run
  the low-level `recover` again.
- The validator's boot relation is `same_boot` when a plan package was
  unpacked after the running boot started.

## Public API and privilege

- `manage-hardware.py reconcile` and `manage-hardware.py status` are the
  only commands. Any other argv is a usage error (exit 64).
- No package, device, path, rule, plan, transaction/attempt/recovery/receipt
  identity, validation file, GC candidate, APT option, environment, reboot
  or force flag is accepted. The library functions take only the host.
- Both commands require effective UID 0. The package journals are root-only
  (0700/0600), and so is the orchestration state.
- In qualification, root runs them only from the root-owned exact-commit
  deployment (`run-hwe-reconcile`, `run-hwe-status`). The launch is
  `env -i python3 -I -B <deployment>/tree/hardware/manage-hardware.py <op>`
  with cwd `/`.
- There is no sudoers rule, Polkit rule, setuid binary, daemon or socket.
  How a future installer or system service supplies privilege is separate
  work.

| Exit | Meaning |
| --- | --- |
| 0 | `complete` (and GC did not fail) |
| 1 | `validation_failed` |
| 2 | `validation_limited` |
| 3 | `stale` |
| 4 | `policy_blocked` |
| 5 | `reboot_required` |
| 6 | the hardware outcome was `complete` or `validation_limited`, but GC failed or was partial |
| 7 | `failed` |
| 8 | another reconcile holds the orchestration lock (nothing written) |
| 9 | `manual_recovery_required` |
| 10 | persistent orchestration state is invalid (preserved; nothing mutated) |
| 64 / 77 | usage / not root |

`status` exits 0, or 10 when the stored state is invalid.

## Persistent ownership root

The fixed root is `/var/lib/lubarchy/hardware-orchestration/` (a source
constant; nothing can override it). It and every object in it must be:
- a real directory or regular file, never a symlink;
- owned by root and not group/other writable;
- on the same filesystem as `/var/lib/lubarchy`.

Each reconcile run owns `<run-id>/`. The run ID is 16 hex digits from the
project's attempt-ID allocator (`os.urandom`), created with an exclusive
`mkdir`. The directory holds:

| File | Content |
| --- | --- |
| `journal.json` | the run journal (`lubarchy.hardware.orchestration-journal/v1`) |
| `inventory.json` | the canonical inventory the run planned from |
| `plan.json` | the canonical plan |
| `validation.json` | the exact canonical validation result (if the run validated) |

Directories are 0700 and files 0600. Every file is canonical JSON plus a
newline, and the journal binds each artifact's digest.

Apply, recovery and GC journals and receipts are **not copied**. They stay
owned by their subsystems and are referenced by identity and digest:
- apply attempt ID, phase, transaction digest and journal digest;
- recovery ID, attempt ID and journal digest;
- GC receipt ID, receipt digest and GC plan digest.

Nothing under the root is ever deleted or rewritten after a later run
starts. History retention is future work, and M1-N does not collect it.

## Run journal

Every journal transition is durable and atomic: temporary file, fsync,
rename, then directory fsync (`package_apply.durable_replace`). The run
directory's creation is followed by an fsync of the root.

A transition is written **before** the step it announces. `recovering`,
`applying` and `collecting` are durable before the primitive is invoked, so
a failed journal write stops the run before any new mutation. An
interrupted transition leaves the previous durable phase visible, never a
partial one.

Fields (all closed and bounded):
- **Identity and chain:** `run_id`, `sequence` (1, 2, 3 …).
  - `previous` holds the predecessor's ID, sequence, journal digest, phase
    and outcome.
  - `awaiting_reboot` holds the pending pre-reboot run, its boot digest and
    its validation digest.
- **State:** `phase`, the monotonic `phases` list, `terminal`, `outcome`, a
  bounded `reason` code, and `reboot_required`.
- **Boot:** `boot_id_digest` (the validator's SHA-256 of the boot ID; the
  raw ID is never stored), `previous_boot_id_digest`, `boot_changed`.
- **Bindings:** inventory, rule-set and plan digests and plan status.
- **Package:**
  - `preflight`: readable, unresolved count, nonterminal recoveries, block;
  - `recovery`: result, recovery ID, attempt ID, journal digest;
  - `decision`: `no_change` or `apply`;
  - the precheck `transaction_digest`;
  - `apply`: attempt ID, result, phase, transaction digest, journal digest;
  - `current_transaction`: the validator's bound apply attempt.
- **Validation:** result digest, status, mode, boot relation, previous
  result digest, `continuity`, critical failure count, `reboot_required`.
- **Maintenance:** `gc` (`not_run`, `nothing_to_collect`, `complete`,
  `partial`, `failed`), a `skipped` reason, receipt ID and digest, GC plan
  digest.
- **Artifacts:** digests of the inventory, plan and validation files.

There are no timestamps. Ordering comes from the sequence, never from
wall-clock time.

Beyond the schema, `check_journal` enforces these invariants on every write
and every load:
- a terminal journal has an outcome and a reason;
- an outcome bound to a validation equals what that validation implies;
- `complete` requires a post-reboot PASS result;
- `reboot_required` requires a same-boot validation **without** a critical
  FAIL;
- `validation_failed` requires an authoritative FAIL;
- `validation_limited` requires a LIMITED or NOT_TESTED result with no
  pending reboot;
- the top-level `reboot_required` flag is the **actionable** reboot state.
  It is true exactly when the outcome is `reboot_required`. The validation
  record's own `reboot_required` keeps the validator's evidence that
  post-reboot observation is pending, even on a failed run;
- an `awaiting_reboot` continuation may only refer to a run whose outcome
  is `reboot_required`, never to a `validation_failed` run (the history
  check rejects it);
- GC appears only in a `complete` or `validation_limited` run;
- an `applied` phase requires a verified apply attempt;
- a resumed validation is bound to the awaited pre-reboot result;
- the boot binding names the awaited boot.

## State machine

```
started -> preflight -> [recovering -> recovered] -> planned
        -> [applying -> applied] -> validated -> [collecting] -> finished
```

Any step may jump to `finished` with an outcome:

Validation outcomes follow a strict precedence:
1. `validation_failed`
2. `reboot_required`
3. `validation_limited`
4. `complete`

| Outcome | When |
| --- | --- |
| `validation_failed` | validation `FAIL` (a critical failure). This holds even on the same boot, after a package install, or when another check would otherwise require a reboot: a critical failure is never hidden behind a reboot request |
| `reboot_required` | no critical FAIL, but the validation is same-boot (a plan package was installed in the running boot, or a check reports `reboot_required`); the result is kept but is not authoritative |
| `validation_limited` | converged; no FAIL and no pending reboot; validation `LIMITED` (or `NOT_TESTED`), never promoted to PASS |
| `complete` | an authoritative post-reboot `PASS` |
| `manual_recovery_required` | package recovery needs a human, or the package journals are unreadable |
| `policy_blocked` | invalid or conflicting plan; profiles, warnings or a source requiring review; a transaction the package layer rejects |
| `stale` | hardware or package state changed during the run (inventory drift, stale transaction, package gate changed) |
| `failed` | recovery failed or could not be verified; apply failed or could not be verified; lock busy; probe, observation or validation errors |

A run that is killed stays nonterminal. The next run records it as its
`previous` (phase, outcome `null`) and starts again from fresh observation.

## Locks and ordering

- `/run/lubarchy/hardware-orchestration.lock`: root-owned parent, regular
  file, `O_NOFOLLOW`, exclusive non-blocking `flock`, never deleted. It
  serializes reconcile runs. A second reconcile exits 8 and writes nothing.
- `/run/lubarchy/package-transaction.lock` stays the package subsystem's own
  lock. Apply, recovery and GC take it inside the primitives. The
  coordinator never takes it itself (gate 15 checks this).
- **Order:** orchestration lock, then package lock. The inverse order never
  occurs in coordinator code. A held package lock yields
  `failed/package_lock_busy` without APT.
- Native APT/dpkg locks are never held or deleted by the coordinator.
- The orchestration lock is not a security boundary against root. Root can
  still run the low-level CLIs; see below.

## Reconcile flow

1. **Lock, then state.** Take the orchestration lock and validate the whole
   persistent history: layout, owner and mode, schema, invariants,
   canonical bytes, artifact digests, a gap-free sequence and digest-bound
   predecessor links. Anything malformed, ambiguous, foreign-owned,
   group/other writable or symlinked raises `state_invalid` (exit 10) before
   a run is created. The bad state is preserved; there is no
   auto-delete and no overwrite. Unrenamed temporary files left by a crash
   inside `durable_replace` are preserved and never trusted.
2. **Recovery first.** Read the apply and recovery journals (authoritative):
   - unreadable journals → `manual_recovery_required`;
   - a manual recovery of an unresolved attempt → `manual_recovery_required`;
   - a failed recovery of an unresolved attempt → `failed/recovery_failed`,
     never retried automatically;
   - an unresolved attempt or a nonterminal recovery → run the bounded M1-L
     recovery, then re-read: `complete`/`abandoned` continue;
     `manual_recovery_required` stops; failure or a result contradicted by
     its own journal stops.

   No generic APT repair and no `dpkg --configure -a`, ever.
3. **Fresh planning.** Fresh probe, the production resolver, the canonical
   inventory and plan persisted. Old plans are historical evidence only.
   Plan policy (`pt.accept_plan`) blocks conflicts, profiles, warnings and
   unauthorised sources.
4. **Decision.** A read-only transaction compile against the current local
   package state:
   - zero install operations → `no_change`; APT is not invoked at all;
   - install operations → `apply`;
   - a local metadata/provenance gap → `apply`, because only the apply
     executor may activate the owned source and refresh metadata, under its
     own lock and revalidation;
   - a transaction the package layer rejects → `policy_blocked`.
5. **Apply** (only if required). Invoke the existing executor. Its return
   value is not trusted on its own. The new attempt's on-disk journal must:
   - exist and be schema-valid;
   - equal the returned document;
   - be `complete` with result `complete` or `nothing_to_apply`;
   - be the only new attempt;
   - leave the package gate clean.

   Otherwise → `failed/apply_unverified`. Apply errors map to `stale`,
   `policy_blocked` or `failed`, with the attempt identity bound.
6. **Validation.** A fresh probe; the plan must equal the planned one (else
   `stale/inventory_changed`). Fresh runtime evidence, the M1-M validator,
   and the exact result persisted. Statuses are never rewritten.
7. **GC** (conservative). Only after `complete` or `validation_limited`, and
   only if the package gate is still clean, through `gc.collect_garbage`.
   The receipt is re-read from disk and bound.

## Recovery and crash behaviour

| Crash point | Next reconcile |
| --- | --- |
| before the apply executor runs (`planned`/`applying`, no attempt) | fresh plan, exactly one apply |
| inside APT/dpkg (attempt nonterminal) | M1-L recovery first; no new apply while it is unresolved |
| after apply completed, before the run journal advanced | the complete attempt is found in the apply journals; the precheck shows no change, so there is no second APT run; the validation binds that attempt |
| during validation or GC | the predecessor is recorded as interrupted; everything is re-observed; GC (M1-N) is idempotent and its own receipts cover a crash |
| external low-level apply interrupted after a finished run | the package journals, not the old outcome, decide: recovery runs first |

## Validation integration

- The validator's result is persisted byte-exact (canonical) with its
  digest. `PASS`, `FAIL`, `LIMITED` and `NOT_TESTED` are preserved in the
  result and in the journal summary.
- `LIMITED` is never promoted to PASS: the outcome is `validation_limited`.
  The contract rejects a `complete` journal without PASS.
- A virtual machine keeps CPU microcode `LIMITED / virtualized_cpu` even
  after installation and a reboot. A VM also proves no physical Wi-Fi,
  Bluetooth radio, GPU acceleration, suspend/resume or external display
  function.

## Reboot-required model

The coordinator **never** reboots, shuts down or schedules a reboot.

- When the validation is same-boot and has no critical FAIL, the run ends
  `reboot_required` (exit 5). A same-boot critical FAIL ends
  `validation_failed` instead:
  - it creates no `awaiting_reboot` continuation and runs no GC;
  - `status` reports the failure as primary and no pending reboot;
  - the validator's artifact is kept exactly, including its same-boot
    evidence.

  If the failure is later corrected while the same boot still needs a
  reboot, a fresh reconcile legitimately ends `reboot_required`.
  It records:
  - the boot digest;
  - the validation digest and the full result;
  - everything needed to resume.

  GC does not run.
- The next run carries `awaiting_reboot`, which refers to that run. A run
  that stops before validating (or is interrupted) carries the reference
  forward unchanged.
- **Same boot:** the run re-validates against the pending result
  (continuity) and stays `reboot_required` (reason `boot_unchanged`). No
  post-reboot qualification is pretended.
- **New boot:** the run re-observes fresh hardware and package state, then
  runs `hv.evaluate(..., previous=<pending result>)` (mode `revalidation`).
  - The validator's continuity checks fail any previously present device
    that disappeared after the reboot.
  - The pending result is only `previous`, never accepted as current. The
    new result is persisted and determines the outcome.
- If the validation profile changed since the pending result, the
  validation runs without continuity and records
  `continuity = profile_changed`.

## GC sequencing

GC runs only after a terminal validation of `complete` or
`validation_limited` (so never before the required post-reboot validation),
and only if the package gate is clean right before it.

It is skipped in every other case, with the reason recorded:
- `reboot_required`;
- `manual_recovery_required`;
- a critical FAIL;
- unresolved package state (`package_state_unresolved`);
- `stale`, `failed` or `policy_blocked` outcomes;
- invalid orchestration state (no run is created).

Deletion is M1-N's alone. A GC failure or partial result is recorded in
`maintenance` and gives exit 6. The hardware outcome and the validation
result stay exactly as validated.

Since the M1-N correction, a really applied bundle is collectable, because
M1-N accepts APT's sandbox-owned `apply-cache/partial/` (`_apt:root`, mode
`0700`, empty). The coordinator never inspects or changes that ownership; it
only invokes M1-N. When M1-N refuses an unsafe candidate, the candidate is
retained, the receipt or `nothing_to_collect` is recorded, and the hardware
outcome is unaffected.

## Idempotence

On a converged machine with no relevant change, a repeated `reconcile`:
- installs, recovers and requests nothing: no APT, no dpkg recovery, no
  reboot request from old state;
- creates no apply attempt;
- produces a deterministic validation result (identical digest for
  identical evidence) and `nothing_to_collect`.

It writes only its own new run directory.

## Status

`status` is read-only. It takes no lock, creates no directory or lock file
and writes nothing. It reports:
- the state (`none`, `valid`, `invalid` with a reason);
- the latest run: ID, sequence, phase, interrupted, outcome, reason, GC, apply
  attempt and recovery ID;
- the latest validation status and digest;
- package recovery need;
- whether a reboot is pending and whether the boot changed since;
- whether the stored validation is still current (`hv.freshness`), when that
  can be determined from a fresh read-only observation.

It contains no raw logs and no raw boot ID.

## Privacy

Persisted data is limited to:
- the inventory and validation result under their existing privacy
  contracts;
- digests, IDs, enums and counts.

There are no MAC addresses, serial numbers, user names, hostnames, IP
addresses, raw boot IDs, raw kernel log lines or timestamps (gate 15 scans
every persisted file).

## External low-level mutation

Root can still run `apply-packages.py apply|recover|gc` directly. That is
safe:
- orchestration state is evidence, not package truth;
- every run re-reads the package journals and re-observes the machine;
- apply's and recovery's own staleness checks remain authoritative;
- drift during a run gives `stale`.

## Future integration boundary

An installer, first-boot service or diagnostics tool would:
- invoke `reconcile`;
- read `status`;
- schedule any reboot itself on exit 5, then invoke `reconcile` again.

How it gets privilege, and whether it runs as a unit, are separate,
separately reviewed work. M1-O adds no such unit.

## Qualification

- Gate 15 is synthetic (fake host roots, real primitives, synthetic runtime
  evidence).
- The real end-to-end qualification runs on the builder under the `pre-m1o`
  checkpoint. The builder lacks `intel-microcode`. The run is:
  1. first reconcile;
  2. an external harness reboot;
  3. the post-reboot reconcile;
  4. an idempotence run;
  5. an exact restore.

See [testing.md](testing.md).
