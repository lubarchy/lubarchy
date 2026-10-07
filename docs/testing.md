# Testing

Testing and qualification baseline for LUBARCHY.

## Principles

- "Works on my VM" is not qualification.
- **Expected to work is not PASS.**
- A result is PASS only with recorded evidence: exact commands, exit statuses,
  relevant output, and the tested configuration.

## Test layers

| Layer | Purpose |
| --- | --- |
| Static validation | Lint, shellcheck, configuration validation |
| Unit testing | Individual tools and scripts |
| Integration testing | Components working together |
| ISO verification | Build outputs, manifests, checksums, signatures |
| Installation testing | Installer flows and storage safety |
| Desktop testing | Hyprland and Plasma sessions |
| Real-hardware testing | Qualification on physical machines |
| Update testing | Update paths between versions |
| Recovery testing | Snapshots, rollback, recovery environment |
| Security testing | Authenticity, exposure, privilege |

## Installer safety matrix

Installer tests must cover each disk mode, existing operating systems,
existing EFI data, multiple disks, insufficient space, and interruption. See
[installer.md](installer.md).

## Security tests

- Signature/authenticity failure handling fails closed.
- No unnecessary listening services by default.
- No secrets or private keys in repository or build outputs.

## Update tests

- Updates from authenticated sources only.
- Upgrade paths between qualified versions.
- Failure during update leaves a recoverable system.

## Recovery tests

Rollback, bootable recovery, and desktop-independent recovery tested with
injected failures. See [recovery.md](recovery.md).

## Reproducibility evidence

Build evidence must include:

- the Git commit built;
- builder environment and tool versions;
- package manifest;
- output checksums;
- build logs.

A clean rebuild must succeed from the same commit.

## M0 future gates

- Repository lint
- shellcheck
- `live-build` configuration validation
- ISO build
- Checksum/manifest
- UEFI VM boot smoke test

None of these gates is implemented yet.

## Failure evidence

Failures are recorded with the same detail as successes: commands, exit
statuses, logs, and configuration. A failed security check is never
downgraded to a warning.

## Release qualification

A release is qualified only when every required gate passes with recorded
evidence tied to the release commit and build manifest.
