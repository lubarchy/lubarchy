# Production hardware rules

LUBARCHY's production compatibility rules: one `lubarchy.hardware.rule/v1`
document per file, named `<rule id>.json`, valid against
[`../schemas/rule-v1.schema.json`](../schemas/rule-v1.schema.json), and
loaded only by the production resolver (`hardware/resolver.py`).

## Classification baseline (M1-C)

| Rule | Matches | Classification |
| --- | --- | --- |
| `class.cpu.intel` | CPU vendor `GenuineIntel` | `cpu.intel` |
| `class.cpu.amd` | CPU vendor `AuthenticAMD` | `cpu.amd` |
| `class.gpu.intel` | PCI vendor `8086`, class `03` | `gpu.intel` |
| `class.gpu.amd` | PCI vendor `1002`, class `03` | `gpu.amd` |
| `class.gpu.nvidia` | PCI vendor `10de`, class `03` | `gpu.nvidia` |
| `class.network.ethernet` | PCI class `0200` | `network.ethernet` |
| `class.network.other` | PCI class `0280` | `network.other` (not Wi-Fi) |
| `class.storage.nvme` | PCI class `010802` | `storage.nvme` |
| `class.storage.sata` | PCI class `0106` | `storage.sata` |
| `class.platform.vm` | virtualization kind `vm` | `platform.vm` |
| `class.platform.container` | virtualization kind `container` | `platform.container` |
| `class.platform.bare-metal` | virtualization kind `none` | `platform.bare-metal` |

## Package mappings (M1-D)

| Rule | Matches | Package (component) |
| --- | --- | --- |
| `package.cpu.intel-microcode` | CPU vendor `GenuineIntel` | `intel-microcode` (`non-free-firmware`) |
| `package.cpu.amd64-microcode` | CPU vendor `AuthenticAMD` | `amd64-microcode` (`non-free-firmware`) |

Every production rule has priority 100. Production rules may carry only
classifications and `require` package effects from an authorised source
(`main`, `non-free-firmware`; [ADR-0001](../../docs/decisions/0001-non-free-firmware.md)).
The resolver refuses a production rule set with any other effect (`forbid`,
profiles, warnings) or source, and the static gates require the production
package set to be exactly `{intel-microcode, amd64-microcode}`. Packages in a
plan are proposals; nothing installs them yet. Device firmware packages are
candidates only (see [SOURCES.md](SOURCES.md)). Identifier and package
provenance is in [SOURCES.md](SOURCES.md).

## Trust boundary

The resolver reads only this directory and never searches subdirectories.
Regular, non-hidden `*.json` files are rules and regular `*.md` files are
documentation. Anything else — a symbolic link, a hidden entry, a
subdirectory, another file type — rejects the whole rule set, as does any
rule that is not strict UTF-8 JSON (duplicate keys, floats), fails rule v1,
is not named after its id, or repeats an id. A bad rule is never skipped.
There is no command-line option to use another rule directory.

Test-only rules live under
[`tests/fixtures/hardware/rules/`](../../tests/fixtures/hardware/rules/) and
must never be copied here: their ids start with `test.` and their package
names (`lubarchy-test-*`) are synthetic.

Rules are data. They contain no commands, shell fragments or code, and a
package source named in a rule is never enabled by the rule (see
[hardware enablement](../../docs/hardware-enablement.md)).
