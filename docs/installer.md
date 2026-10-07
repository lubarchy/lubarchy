# Installer

Requirements for the future LUBARCHY installer (D-009). **No installer is
implemented**; this is planned for M3, with dual boot and advanced storage in
M5.

## Objective

Boot USB → simple installer → reboot → usable desktop, in approximately five
primary screens, while retaining advanced functionality.

## Primary flow

1. Language / keyboard / region
2. Network
3. Installation destination
4. User / authentication
5. Review / install

## Disk modes

- Erase disk and install LUBARCHY
- Install alongside existing OS
- Advanced/manual

## Storage safety invariants

- Robust disk identity; no ambiguous destructive targets.
- Revalidate disk topology immediately before committing changes.
- Preserve foreign EFI data.
- Never silently overwrite another operating system.
- Check available space before committing.
- Handle interruption without leaving an unrecoverable state where
  practical.
- Provide actionable diagnostics on failure.

## Hardware integration boundary

The installer consumes the hardware enablement pipeline
([hardware-enablement.md](hardware-enablement.md)). It does not implement its
own separate hardware detection logic.

## Desktop provisioning

The installed system should provide the default LUBARCHY Hyprland session and
the supported KDE Plasma session, with Plasma available when Hyprland is not
qualified for the hardware.

## Dual-boot requirements

- Detect existing operating systems and EFI entries.
- Preserve other operating systems and their boot entries.
- Present resizing or free-space choices clearly before committing.

## Manual mode safety

- Advanced/manual mode retains the safety invariants above.
- Destructive operations are summarised and confirmed before commit.

## Failure behaviour

- Fail before destructive changes whenever possible.
- On failure, report what changed and what did not.
- Preserve installer logs for diagnostics, without secrets.

## Testing matrix

The installer must be tested across, at minimum:

- each disk mode;
- empty disks, disks with an existing OS, and multi-disk systems;
- NVMe and SATA;
- existing EFI partitions;
- interruption and failure injection;
- representative hardware classes.

See [testing.md](testing.md).

## Open items

- Installer technology: **TBD (P-004).**
- Filesystem/subvolume layout: TBD (P-002).
- Encryption default: TBD (P-003).
- Bootloader: TBD (P-001).
