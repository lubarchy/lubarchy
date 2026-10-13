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

## Debian packages (M1-D)

Verified on 2026-10-13 against Debian Trixie metadata in an isolated APT
configuration (official `deb.debian.org` and `security.debian.org`,
`Signed-By: /usr/share/keyrings/debian-archive-keyring.pgp`, insecure
repositories disallowed, nothing installed; system APT sources untouched).
All three `InRelease` files were also verified independently with `sqv`
against the Debian archive keyring. Their `Components` fields include
`non-free-firmware` for `trixie`, `trixie-updates` and `trixie-security`,
matching the Debian 13 release notes (§4.2.8), which show
`Components: main non-free-firmware` for those suites.

Versions are recorded as observed; rules never pin a version.

### Active mappings

| Rule | Package | Component (Section) | Architecture | Description | Observed version |
| --- | --- | --- | --- | --- | --- |
| `package.cpu.intel-microcode` | `intel-microcode` | `non-free-firmware/admin` | amd64 | Processor microcode firmware for Intel CPUs | 3.20251111.1~deb13u1 (`trixie`); 3.20250812.1~deb13u1 (`trixie-security`) |
| `package.cpu.amd64-microcode` | `amd64-microcode` | `non-free-firmware/admin` | amd64 | Platform firmware and microcode for AMD CPUs and SoCs | 3.20250311.1 (`trixie`) |

### Candidates — NOT ACTIVE (candidate only — no production mapping in M1-D; M1-E activates the four marked "M1-E")

| Package | Source package | Section | Description | Observed version |
| --- | --- | --- | --- | --- |
| `firmware-intel-graphics` (M1-E: active) | `firmware-nonfree` | `non-free-firmware/kernel` | Binary firmware for Intel iGPUs and IPUs | 20250410-2 |
| `firmware-amd-graphics` (M1-E: active) | `firmware-nonfree` | `non-free-firmware/kernel` | Binary firmware for AMD/ATI graphics and NPU chips | 20250410-2 |
| `firmware-iwlwifi` (M1-E: active) | `firmware-nonfree` | `non-free-firmware/kernel` | Binary firmware for Intel Wireless cards | 20250410-2 |
| `firmware-realtek` | `firmware-nonfree` | `non-free-firmware/kernel` | Binary firmware for Realtek network and audio chips | 20250410-2 |
| `firmware-atheros` | `firmware-nonfree` | `non-free-firmware/kernel` | Binary firmware for Qualcomm Atheros wireless cards | 20250410-2 |
| `firmware-brcm80211` (M1-E: active) | `firmware-nonfree` | `non-free-firmware/kernel` | Binary firmware for Broadcom/Cypress 802.11 wireless cards | 20250410-2 |
| `bluez-firmware` | `bluez-firmware` | `non-free-firmware/kernel` | Firmware for Bluetooth devices | 1.2-13 |
| `firmware-nvidia-graphics` | `firmware-nonfree` | `non-free-firmware/kernel` | Binary firmware for Nvidia GPU chips | 20250410-2 |
| `firmware-nvidia-gsp` | `nvidia-graphics-drivers` | `non-free-firmware/kernel` | NVIDIA GSP firmware | 550.163.01-2 |

`nvidia-driver` is not available from the authorised sources (`main`,
`non-free-firmware`); it lives in Debian `non-free`, which is not
authorised. NVIDIA firmware packages existing does not decide the NVIDIA
driver strategy.

## Device firmware mappings (M1-E)

Evidence chain for every active firmware rule: device modalias → kernel
module alias → kernel module → Debian firmware package → `non-free-firmware`.

### Kernel module aliases

Qualified kernel: `linux-image-6.12.111+deb13-amd64`, version `6.12.111-1`
(`uname -r` `6.12.111+deb13-amd64`), on the Debian 13 builder, 2026-10-13.
Source: `/lib/modules/6.12.111+deb13-amd64/modules.alias` (generated by
`depmod` from the package-owned modules; `modules.builtin.alias` absent).
For all nine modules, `modinfo -F alias` on the package-owned `.ko` file
equals the `modules.alias` entries. No module was loaded.

Normalized evidence: [`tools/hwe/module-alias-evidence.json`](../../tools/hwe/module-alias-evidence.json)
(PCI/USB aliases only, bytewise sorted, unique, case preserved). Digests are
SHA-256 of the newline-joined alias list:

| Module | PCI/USB aliases | Digest |
| --- | --- | --- |
| `amdgpu` | 311 | `sha256:19cff2f214241df31431ed4a2e7d4557e907b9688454ff7140249bcb30d90469` |
| `brcmfmac` | 36 | `sha256:76f4a4e347e7536df6c06f5eb246211ff9ef69b53d27545345ab7412320bbfa4` |
| `brcmsmac` | 0 | `sha256:01ba4719c80b6fe911b091a7c05124b64eeece964e09c058ef8f9805daca546b` |
| `i915` | 365 | `sha256:f2200241316a365004f316230a8770548ddd07463a97f0c7e5793f0728169ae8` |
| `iwl3945` | 6 | `sha256:bd12435e97610edaf9589332d5ef97717f8c2b01749a48e448e2ae1b67c55959` |
| `iwl4965` | 2 | `sha256:9ec89c2c03e1113a845acde1e7af6825377b9386fe137b96f767b1cb791c18ef` |
| `iwlwifi` | 444 | `sha256:8a52df9d6cf619982d7b3b14326ad33ee642e0e5ff3cbe80aaa1327091508adb` |
| `radeon` | 699 | `sha256:f11b31a3a6bf999ab461a92e46fee4a7a4506c7ff183bf59944d3c342b8329e7` |
| `xe` | 115 | `sha256:5ace783505953f483f83692c8f78ef9cbae7cb89edfe8ff06ac0fbe058b611bc` |

Excluded (not representable in inventory v1): 23 `brcmfmac` `sdio:`
aliases; the 3 `brcmsmac` `bcma:` aliases (`bcma:m04BFid0812rev11cl*`,
`rev17`, `rev18`); the `iwl4965` non-bus alias `iwl4965`.

### Debian packages (active)

Verified 2026-10-13 against signature-verified Trixie metadata (isolated APT
configuration, Debian archive keyring, `sqv` 3 of 3 InRelease files,
nothing installed). Each `.deb` was downloaded only to corroborate its
contents; its SHA-256 matched the signed Packages index; it was deleted.

| Package | Section | Version | Long description names | Payload corroboration |
| --- | --- | --- | --- | --- |
| `firmware-iwlwifi` | `non-free-firmware/kernel` | 20250410-2 | "supported by the iwl3945, iwl4965, and iwlwifi drivers" | 73 `iwlwifi-*.ucode` |
| `firmware-intel-graphics` | `non-free-firmware/kernel` | 20250410-2 | "supported by the i915, intel-ipu6-isys, ipu3-imgu, mei-vsc-hw, and xe" drivers | 58 `i915/`, 6 `xe/` files |
| `firmware-amd-graphics` | `non-free-firmware/kernel` | 20250410-2 | "supported by the radeon, amdgpu, amdxdna and r128 drivers" | 526 `amdgpu/`, 149 `radeon/` files |
| `firmware-brcm80211` | `non-free-firmware/kernel` | 20250410-2 | "supported by the brcmsmac or brcmfmac driver" | 44 `brcm/brcmfmac*` files |

Only the listed kernel modules are mapped: not `intel-ipu6-isys`,
`ipu3-imgu`, `mei-vsc-hw`, `amdxdna`, `r128` or `brcmsmac`.

### Overlap review

- Between different firmware packages: **none**. No concrete PCI/USB
  modalias can match aliases of two different packages (field-wise analysis
  of all 1,722 active aliases; also enforced by
  `qualify-firmware-mappings.py check-rules`).
- Within a package: `i915`/`xe` share 107 aliases and `amdgpu`/`radeon`
  share 149; both map to the same package.
- Same device claimed by other modules (device-specific aliases): only
  `radeonfb` (the legacy framebuffer driver) shares 96 IDs with `radeon`. It
  loads no firmware package, so there is no firmware ambiguity. No
  `iwlwifi`-family, `i915`/`xe`, `amdgpu` or `brcmfmac` alias is claimed
  device-specifically by another module; in particular no `brcmfmac` ID is
  claimed by `bcma`, `b43` or `brcmsmac`.

## Not classified in M1-C

- USB devices (Bluetooth, Wi-Fi and others): no production USB rule yet;
  they stay unresolved.
- Hybrid or multi-GPU systems: rule v1 matches one target per selector and
  cannot express relationships between devices, so there is no
  `gpu.hybrid` rule.
- Any package, firmware, microcode, profile or source requirement (M1-D
  adds only the two CPU microcode mappings above).
