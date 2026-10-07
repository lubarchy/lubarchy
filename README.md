# LUBARCHY

LUBARCHY is a clean, Debian Stable-based desktop Linux distribution. It aims to
take a beginner from a bootable USB stick to a usable, hardware-ready desktop
through a short, safe installer, while keeping the operating system auditable,
recoverable, and built entirely from version-controlled configuration.

## Baseline

| Item | Value |
| --- | --- |
| Base | Debian Stable |
| Initial target | Debian 13 "Trixie" |
| Architecture | amd64 |
| Host package authority | APT/dpkg |
| Production ISO mechanism | Debian `live-build` |

## Status

**Current milestone: M0 — Minimal reproducible ISO**

LUBARCHY is in early bootstrap. **No installable LUBARCHY release exists yet.**
Nothing described under product goals is available today; see
[docs/project-state.md](docs/project-state.md) for the authoritative current
state.

## Product goals

- Boot USB → simple installer (approximately five primary screens) → reboot →
  usable desktop.
- Ordinary hardware setup without terminal work for a normal beginner.
- Wi-Fi, Bluetooth, graphics, audio, firmware, displays, input devices, and
  power management working automatically where practical.
- Recovery as core functionality, independent of the desktop session.

## Architectural principles

Priority order: correctness, security, data integrity, hardware compatibility,
reliability, recovery, maintainability, testability, simplicity, performance,
UX, delivery speed.

- Debian Stable foundation; no mixing with uncontrolled Testing/Unstable
  repositories.
- APT/dpkg is the only host OS package authority.
- Repository state, not a manually modified ISO or builder, is the source of
  truth.
- Production images come from version-controlled `live-build` configuration.
- Fail closed on signature or authenticity failures.
- Significant architecture decisions are recorded in the
  [decision register](docs/decisions.md) and ADRs.

## Milestones

1. M0 — Minimal reproducible ISO *(in progress)*
2. M1 — Hardware enablement foundation
3. M2 — Hyprland + Plasma desktop
4. M3 — Beginner installer
5. M4 — Recovery and rollback
6. M5 — Dual boot / advanced storage
7. M6 — Update system / release qualification
8. M7 — Real-hardware qualification

## Documentation

- [Project state](docs/project-state.md)
- [Architecture](docs/architecture.md)
- [Build environment](docs/build.md)
- [Decision register](docs/decisions.md)
- [ADR process](docs/decisions/README.md)
- [Security](docs/security.md)
- [Hardware enablement](docs/hardware-enablement.md)
- [Installer](docs/installer.md)
- [Recovery](docs/recovery.md)
- [Testing](docs/testing.md)
- [Executor contract](CLAUDE.md)

## Project identity

- Name: LUBARCHY
- Website: `lubarchy.com`
- GitHub: `github.com/lubarchy`
- Repository: `lubarchy/lubarchy` — <https://github.com/lubarchy/lubarchy>

## License

See [LICENSE](LICENSE).
