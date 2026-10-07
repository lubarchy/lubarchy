# Recovery

Requirements for LUBARCHY recovery (D-010). **Recovery is not implemented**;
this is planned for M4.

## Principle

Recovery is core functionality, not an add-on. A user must be able to return
to a working system after a failed update, configuration error, or desktop
failure.

## Targets

- Known-good snapshots
- Rollback
- Bootable recovery
- Boot repair
- Package/update repair
- Filesystem diagnostics
- Hardware diagnostics
- Desktop repair
- Eventual factory reset

## Desktop independence

Recovery must not depend on Hyprland or Plasma successfully starting.

## Filesystem

Btrfs is currently preferred, **pending validation and an ADR (P-002).** No
filesystem or subvolume layout has been decided.

## Snapshot design questions

- What is included in and excluded from snapshots?
- How is user data protected from rollback of system state?
- When are snapshots created, and how are they retained and pruned?
- How are snapshots related to package transactions?

## Rollback contract questions

- What state does a rollback restore, and what does it preserve?
- How does a rollback interact with the bootloader (P-001)?
- How is a rollback verified to have succeeded?

## Update integration

Updates should create a known-good point before changes and offer a recovery
path if the updated system fails. The update system is planned for M6.

## Recovery environment

A bootable recovery environment must be available when the installed system
cannot start. Its form and boot integration are TBD.

## Factory reset

Factory reset is a future target. Its scope and data-preservation behaviour are
not yet defined.

## Security concerns

- Recovery must not bypass authentication or encryption.
- Recovery artifacts must be authenticated.

See [security.md](security.md).

## Failure testing

Recovery must be tested against injected failures, including interrupted
updates, broken packages, an unbootable desktop session, and boot failures.
See [testing.md](testing.md).

## Production success criteria

Recovery is production-ready only when each target above has documented
behaviour and recorded test evidence across the supported configurations.
