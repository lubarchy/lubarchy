# Hardware enablement contracts

Source-controlled contracts for the LUBARCHY hardware-enablement subsystem
(D-008), established in M1-A:

`probe → normalized inventory → compatibility rules → resolution plan →`
later installation/configuration → later validation

| Path | Content |
| --- | --- |
| [`schemas/inventory-v1.schema.json`](schemas/inventory-v1.schema.json) | `lubarchy.hardware.inventory/v1`: observed, normalized, privacy-safe facts |
| [`schemas/rule-v1.schema.json`](schemas/rule-v1.schema.json) | `lubarchy.hardware.rule/v1`: one declarative compatibility rule |
| [`schemas/plan-v1.schema.json`](schemas/plan-v1.schema.json) | `lubarchy.hardware.plan/v1`: a proposed resolution plan |
| [`schemas/package-transaction-v1.schema.json`](schemas/package-transaction-v1.schema.json) | `lubarchy.hardware.package-transaction/v1`: install-only package transaction compiled from a fresh plan (M1-I) |
| [`schemas/source-activation-v1.schema.json`](schemas/source-activation-v1.schema.json) | `lubarchy.packages.source-activation/v1`: plan realising the accepted Debian source policy with one LUBARCHY-owned source file (M1-J) |
| [`schemas/apply-journal-v1.schema.json`](schemas/apply-journal-v1.schema.json) | `lubarchy.packages.apply-journal/v1`: durable runtime journal of one privileged apply attempt (M1-K) |
| [`schemas/staged-bundle-v1.schema.json`](schemas/staged-bundle-v1.schema.json) | `lubarchy.packages.staged-bundle/v1`: manifest of a verified payload bundle bound to one transaction (M1-J) |
| [`rules/`](rules/) | Production rules: classification baseline (M1-C), CPU microcode mappings (M1-D), device firmware mappings (M1-E, generated) |
| [`../tools/hwe/`](../tools/hwe/) | Offline qualification tooling: kernel module-alias evidence and firmware rule generation/drift checks (M1-E); NVIDIA supported-GPU snapshot normalizer (M1-G, evidence only); NVIDIA repository containment policy validator (M1-H, inactive policy); package transaction builder qualification (M1-I, builder only); source activation and staging qualification (M1-J, builder only); trusted root-owned qualification deployment checks and launcher (M1-K, builder only) |
| [`probe.py`](probe.py) | Read-only live probe: running system → canonical inventory v1 on stdout (M1-B) |
| [`resolver.py`](resolver.py), [`resolve.py`](resolve.py) | Production resolver library and CLI: inventory v1 + production rules → canonical plan v1 (M1-C) |
| [`package_transaction.py`](package_transaction.py), [`compile-transaction.py`](compile-transaction.py) | Package transaction compiler library and read-only CLI: fresh probe + production resolve → install-only `lubarchy.hardware.package-transaction/v1` (M1-I); no apply ([docs](../docs/package-transactions.md)) |
| [`source_activation.py`](source_activation.py), [`compile-source-activation.py`](compile-source-activation.py) | Debian source activation library and CLI: `/etc/apt` (read-only) → normalized pre-state → source-activation v1 → rendered owned deb822 file; isolated authenticated metadata (M1-J); no activate ([docs](../docs/package-staging.md)) |
| [`package_staging.py`](package_staging.py), [`stage-packages.py`](stage-packages.py) | Staged package acquisition library and CLI: transaction → exact payloads → verified, finalized bundle in a caller workspace (M1-J); no apply ([docs](../docs/package-staging.md)) |
| [`package_apply.py`](package_apply.py), [`apply-packages.py`](apply-packages.py) | Privileged apply executor library and root-only CLI: lock, journal (`schemas/apply-journal-v1.schema.json`), owned-source activation, refresh, fresh transaction, root-owned staging, re-hash, offline APT install, validation (M1-K) ([docs](../docs/package-apply.md)) |
| [`package_recovery.py`](package_recovery.py) | Interrupted transaction recovery (`apply-packages.py recover`, root only): classification from real dpkg state, transaction-attributed recovery impact set, contained exact-package dpkg completion (M1-L) ([docs](../docs/package-recovery.md)) |
| [`package_gc.py`](package_gc.py) | Package state retention and garbage collection (`apply-packages.py gc-plan` / `gc`, root only): journal-derived reference graph, protected/eligible/invalid classification, identity-checked no-follow deletion of proven-obsolete payload state, durable receipts (M1-N) ([docs](../docs/package-garbage-collection.md)) |
| [`hardware_validation.py`](hardware_validation.py), [`validate-hardware.py`](validate-hardware.py) | Read-only post-install hardware validation: fresh plan + validation profile + runtime evidence → `lubarchy.hardware.validation-result/v1` with device-attributed PASS/FAIL/LIMITED/NOT_TESTED checks (M1-M) ([docs](../docs/hardware-validation.md)) |
| [`nvidia/`](nvidia/README.md) | NVIDIA driver strategy evidence and normalized supported-GPU snapshot (M1-G); inactive repository containment policy and APT fixtures (M1-H). Evidence only: never read by the resolver, rules or builds; ADR-0002 is Proposed |

All schemas use JSON Schema Draft 2020-12. Each carries its own version and
changes only by a new schema version, independently of any code.

The probe only observes and the resolver only proposes. Since M1-K the
root-only apply executor is the single component that activates the
LUBARCHY-owned source and installs hardware packages, only for the current
qualified plan.
Firmware/package mappings and installation are later M1 work.

Semantics (matching, priority, conflicts, canonical JSON and digests) are
documented in [docs/hardware-enablement.md](../docs/hardware-enablement.md)
and defined executably by the reference implementation
[`tests/hwe_reference.py`](../tests/hwe_reference.py), which the static gate
`tests/hardware-contracts.py` exercises against synthetic fixtures in
[`tests/fixtures/hardware/`](../tests/fixtures/hardware/).
