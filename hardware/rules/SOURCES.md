# Production rule evidence

Provenance of the identifiers used by the M1-C production classification
rules. Rules match numeric facts only; human-readable names below are for
verification and never become rule identity. Nothing here is copied from an
external database, and nothing is fetched at runtime.

Evidence collected on 2026-10-12. Authority, in order of precedence:

1. **PCI-SIG**, *PCI Code and ID Assignment Specification* — the normative
   class-code definitions (base class, subclass, programming interface).
2. **Linux kernel**, `include/linux/pci_ids.h` at tag `v6.12` (the kernel
   series LUBARCHY's Debian 13 base ships), and the x86 CPU vendor tables in
   `arch/x86/kernel/cpu/intel.c` and `arch/x86/kernel/cpu/amd.c` at `v6.12`.
3. **PCI ID Repository** (`pci.ids`, version 2026.10.12) — human-readable
   cross-check only.

## CPU vendors

| Rule | Identifier | Meaning | Evidence |
| --- | --- | --- | --- |
| `class.cpu.intel` | CPUID vendor `GenuineIntel` | Intel processor | Kernel `intel.c`: `.c_ident = { "GenuineIntel" }` |
| `class.cpu.amd` | CPUID vendor `AuthenticAMD` | AMD processor | Kernel `amd.c`: `.c_ident = { "AuthenticAMD" }` |

Other vendor strings (for example Hygon or Zhaoxin) are deliberately not
classified.

## PCI vendors (graphics rules)

| Identifier | Meaning | Kernel `pci_ids.h` | `pci.ids` |
| --- | --- | --- | --- |
| `8086` | Intel | `PCI_VENDOR_ID_INTEL 0x8086` | `Intel Corporation` |
| `1002` | AMD graphics (former ATI vendor ID) | `PCI_VENDOR_ID_ATI 0x1002` | `Advanced Micro Devices, Inc. [AMD/ATI]` |
| `10de` | NVIDIA | `PCI_VENDOR_ID_NVIDIA 0x10de` | `NVIDIA Corporation` |

AMD's other PCI vendor ID, `1022` (`PCI_VENDOR_ID_AMD`), is used for AMD
chipset, CPU and platform functions, not for Radeon graphics, and is
deliberately not part of `class.gpu.amd`.

## PCI class codes

| Rule | Class prefix | Meaning | Kernel `pci_ids.h` | `pci.ids` |
| --- | --- | --- | --- | --- |
| `class.gpu.intel`, `class.gpu.amd`, `class.gpu.nvidia` | `03` | Display controller (any subclass: VGA `0300`, XGA `0301`, 3D `0302`, other `0380`) | `PCI_BASE_CLASS_DISPLAY 0x03` | `C 03 Display controller` |
| `class.network.ethernet` | `0200` | Ethernet controller | `PCI_CLASS_NETWORK_ETHERNET 0x0200` | `C 02` / `00 Ethernet controller` |
| `class.network.other` | `0280` | Other network controller | `PCI_CLASS_NETWORK_OTHER 0x0280` | `C 02` / `80 Network controller` |
| `class.storage.nvme` | `010802` | NVM Express controller | `PCI_CLASS_STORAGE_EXPRESS 0x010802` | `C 01` / `08 Non-Volatile memory controller` / `02 NVM Express` |
| `class.storage.sata` | `0106` | SATA controller (any programming interface, e.g. AHCI `010601`) | `PCI_CLASS_STORAGE_SATA 0x0106` | `C 01` / `06 SATA controller` |

Class `0280` covers Wi-Fi adapters but also other network controllers, so it
is classified as `network.other`, never as `network.wifi`. A display-class
rule says only that a function is a display controller from that vendor: not
integrated vs discrete, generation, driver strategy, firmware need, PRIME
topology or desktop suitability.

## Platform

| Rule | Fact | Evidence |
| --- | --- | --- |
| `class.platform.vm` | `virtualization.kind` = `vm` | `systemd-detect-virt --vm` (systemd-detect-virt(1)), recorded by the M1-B probe |
| `class.platform.container` | `virtualization.kind` = `container` | `systemd-detect-virt --container` |
| `class.platform.bare-metal` | `virtualization.kind` = `none` | Both checks reported `none`; an inventory without a virtualization observation matches nothing |

## Not classified in M1-C

- USB devices (Bluetooth, Wi-Fi and others): no production USB rule yet;
  they stay unresolved.
- Hybrid or multi-GPU systems: rule v1 matches one target per selector and
  cannot express relationships between devices, so there is no
  `gpu.hybrid` rule.
- Any package, firmware, microcode, profile or source requirement.
