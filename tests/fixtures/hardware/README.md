# Hardware contract fixtures (TEST-ONLY)

Synthetic inputs for `tests/hardware-contracts.py`. **Nothing here is a
production compatibility rule or a real machine's inventory.** No data was
copied from any personal machine: every device, subsystem, CPU model and
microcode value is synthetic. Public PCI/USB vendor IDs (for example `8086`,
`1002`, `10de`, and QEMU's `1234`/`1af4`) are used so the vendor-based test
rules have something to match; device IDs are synthetic (`f0xx`), and fixture
08 uses an invented vendor `f0ff`.

| Path | Content |
| --- | --- |
| `inventory/` | Valid `lubarchy.hardware.inventory/v1` fixtures |
| `invalid/` | Inventories that must be rejected |
| `rules/` | TEST-ONLY rule set (`test.*` ids, `lubarchy-test-*` packages) |
| `rules-conflict/` | TEST-ONLY rules that must produce a conflict |
| `expected/` | Expected plans; the gate compares canonical JSON bytes |

| Fixture | Proves | Expected status |
| --- | --- | --- |
| `01-intel-cpu-intel-gpu` | Intel CPU + Intel integrated GPU | partial (a non-main source needs policy review) |
| `02-amd-cpu-amd-gpu` | AMD CPU + AMD GPU behind a bridge (parent link) | resolved |
| `03-nvidia-discrete-gpu` | NVIDIA discrete GPU; non-main source and warning | partial |
| `04-intel-nvidia-hybrid` | Intel iGPU + NVIDIA 3D controller behind a bridge | partial |
| `05-ethernet-wifi` | Ethernet + Wi-Fi controllers | partial |
| `06-usb-bluetooth` | USB Bluetooth controller (interfaces) under an xHCI controller | partial |
| `07-nvme-storage` | NVMe controller (class code and modalias selectors) | resolved |
| `08-unknown-pci-device` | Unknown CPU vendor and unknown PCI device: no rule, nothing proposed | partial |
| `09-qemu-kvm-guest` | QEMU/KVM-like guest (platform selector, virtio) | resolved |
| `invalid/10-forbidden-field` | Inventory with a `mac_address` field | invalid |
| `invalid/10-uppercase-hex` | Inventory with a non-normalized vendor ID | invalid |
| `rules-conflict/` (`11-conflict`) | A required and a forbidden package for the same device | conflict |
| `12-determinism-shuffled` | `06-usb-bluetooth` with reordered arrays, keys and whitespace | identical digest and plan bytes |

Expected plans are regenerated only when a contract change is intended; any
regeneration must be reviewed line by line, because the gate trusts them as
the specification of the resolver's output.
