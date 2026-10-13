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

## Second-wave firmware mappings (M1-F)

Strengthened chain for every active M1-F module: device modalias → binding
module alias → binding module and its dependency closure → declared firmware
files → authenticated Debian owner → one `non-free-firmware` package.
Evidence: [`tools/hwe/module-alias-evidence.json`](../../tools/hwe/module-alias-evidence.json)
(format `lubarchy.hardware.firmware-evidence/v2`), qualified 2026-10-13 on
`linux-image-6.12.111+deb13-amd64` 6.12.111-1 (`modules.alias` SHA-256
`df01d151d286b149b2afa4974686794b0389cb442fb3469cf7a01aec73817699`, identical to M1-E).

- **Module metadata:** `firmware`, `depends` and `alias` read from the
  package-owned `.ko` files by the tool's ELF reader; equal to `modinfo -F`
  output for all 54 mapped modules.
- **File ownership:** Debian Contents indices (`trixie`, `trixie-updates`,
  `trixie-security`; `main` and `non-free-firmware`) fetched by an isolated
  APT configuration and verified against the signed InRelease (also checked
  with `sqv`, 3 of 3); digest of the normalized firmware-path table
  `sha256:851a7323ee32952e5c7d069b9dbf62f7e041f7cb90e333dc827ac50d7a92f0bb`. Contents and payload manifests agree for every
  candidate package.
- **Payloads:** each `.deb` downloaded only for inspection, its SHA-256
  matched against the signed Packages index, its firmware file list
  normalized and hashed, then deleted.

| Package | Version | Section | Firmware entries | `.deb` SHA-256 | Manifest SHA-256 |
| --- | --- | --- | --- | --- | --- |
| `bluez-firmware` | 1.2-13 | `non-free-firmware/kernel` | 10 files, 0 links | `4dba886ff82067c814f81ec9d675aa0631b497ca8e7d7985694bac6cfe61e825` | `6aa721aaa11df485f60f4175c46df6ae7395353ecb7a382688b78346c880fb91` |
| `firmware-amd-graphics` | 20250410-2 | `non-free-firmware/kernel` | 677 files, 215 links | `22fa89a699044d92e54c9a3b6d737da4f474245fa2001d3cbc039b74d56ac933` | `29291e6af76aeb9861f4890753ce9e13dbd692d0172575f6fe106dbd087ce8a2` |
| `firmware-atheros` | 20250410-2 | `non-free-firmware/kernel` | 231 files, 53 links | `8387a45e9307052858185620ab30efebbf9660b072d6c8309673bf112e291f35` | `fe8c913f3f45fa949b8efb4def79911366990db6f8228f4b6050e025a12901a2` |
| `firmware-brcm80211` | 20250410-2 | `non-free-firmware/kernel` | 74 files, 58 links | `266cc703e2299f5253fd1ff9a1fd625d85a2c8e5a88b1a65fcc190ac384ce3d7` | `2b4745423160dd172fedabae0091231651582a38191bdd53c74bdaf28eec6a0c` |
| `firmware-intel-graphics` | 20250410-2 | `non-free-firmware/kernel` | 90 files, 10 links | `253d624068b361cfbc43c88b64ec0904fd14de1b704f89bdbe457a2e7edba2dc` | `cf8c11313e55b4a55dd73431418e05cbc6e001834a1d575c9e15c72e67824a75` |
| `firmware-iwlwifi` | 20250410-2 | `non-free-firmware/kernel` | 117 files, 63 links | `ec0e18c0214abb1402b244c41e8f6769ffb3f4d75486536c007c5bef95f08cf5` | `bac43b64d41fb2d15eed76c401791594fa9029f7899cf160149dd1d94764ce8d` |
| `firmware-mediatek` | 20250410-2 | `non-free-firmware/kernel` | 107 files, 12 links | `957f82d72d413f4577f45bfad03237b1dd6f8693eddc226ac452f9c5d4187012` | `f03d31935f16db89ab88673436861f6753dfcc9a57840f36ab2d897184f81308` |
| `firmware-realtek` | 20250410-2 | `non-free-firmware/kernel` | 128 files, 16 links | `925755a9b0891c7dc39bbb73d28ab8f7193d37303b5022e8bc9d458b9b4aaa4e` | `c8dca93f3fbf2404e686dab1c783d349b56a7a5f520e451bf1367ea4af67e343` |

### Active M1-F modules

| Module | Module file | PCI/USB aliases | Declared firmware (closure) | Alias digest |
| --- | --- | --- | --- | --- |
| `ath3k` | `bluetooth/ath3k.ko.xz` | 62 | 1 by `firmware-atheros`, 0 unshipped | `sha256:3f277ddc43b76ed5…` |
| `ar5523` | `net/wireless/ath/ar5523/ar5523.ko.xz` | 55 | 1 by `firmware-atheros`, 2 unshipped | `sha256:c6a582e1217d427a…` |
| `ath10k_pci` | `net/wireless/ath/ath10k/ath10k_pci.ko.xz` | 9 | 15 by `firmware-atheros`, 8 unshipped | `sha256:d1123be2a9595812…` |
| `ath6kl_usb` | `net/wireless/ath/ath6kl/ath6kl_usb.ko.xz` | 3 | 2 by `firmware-atheros`, 8 unshipped | `sha256:b4a3daef7b0c7b48…` |
| `wil6210` | `net/wireless/ath/wil6210/wil6210.ko.xz` | 3 | 2 by `firmware-atheros`, 5 unshipped | `sha256:1e1fa86a3cfc636a…` |
| `bcm203x` | `bluetooth/bcm203x.ko.xz` | 1 | 2 by `bluez-firmware`, 0 unshipped | `sha256:208d486ad04829b6…` |
| `mt7601u` | `net/wireless/mediatek/mt7601u/mt7601u.ko.xz` | 17 | 1 by `firmware-mediatek`, 2 unshipped | `sha256:98dbcde40c0e66c1…` |
| `mt7615e` | `net/wireless/mediatek/mt76/mt7615/mt7615e.ko.xz` | 3 | 7 by `firmware-mediatek`, 2 unshipped | `sha256:d6c6133d2da8ac5b…` |
| `mt7663u` | `net/wireless/mediatek/mt76/mt7615/mt7663u.ko.xz` | 2 | 4 by `firmware-mediatek`, 2 unshipped | `sha256:3f662eb11c5271ea…` |
| `mt76x0e` | `net/wireless/mediatek/mt76/mt76x0/mt76x0e.ko.xz` | 3 | 2 by `firmware-mediatek`, 2 unshipped | `sha256:18dd199ff3212178…` |
| `mt76x0u` | `net/wireless/mediatek/mt76/mt76x0/mt76x0u.ko.xz` | 25 | 2 by `firmware-mediatek`, 2 unshipped | `sha256:4a4543318a5cc36c…` |
| `mt76x2e` | `net/wireless/mediatek/mt76/mt76x2/mt76x2e.ko.xz` | 3 | 2 by `firmware-mediatek`, 2 unshipped | `sha256:5291a65898162da7…` |
| `mt76x2u` | `net/wireless/mediatek/mt76/mt76x2/mt76x2u.ko.xz` | 16 | 2 by `firmware-mediatek`, 2 unshipped | `sha256:e9f2bfb5105eec09…` |
| `mt7915e` | `net/wireless/mediatek/mt76/mt7915/mt7915e.ko.xz` | 4 | 6 by `firmware-mediatek`, 2 unshipped | `sha256:66debc9999520e10…` |
| `mt7921e` | `net/wireless/mediatek/mt76/mt7921/mt7921e.ko.xz` | 6 | 6 by `firmware-mediatek`, 2 unshipped | `sha256:c75677fd2d916ce7…` |
| `mt7921u` | `net/wireless/mediatek/mt76/mt7921/mt7921u.ko.xz` | 5 | 2 by `firmware-mediatek`, 2 unshipped | `sha256:c51e5eee55ea516e…` |
| `mt7925e` | `net/wireless/mediatek/mt76/mt7925/mt7925e.ko.xz` | 2 | 2 by `firmware-mediatek`, 2 unshipped | `sha256:f72be955de438893…` |
| `mt7925u` | `net/wireless/mediatek/mt76/mt7925/mt7925u.ko.xz` | 3 | 2 by `firmware-mediatek`, 2 unshipped | `sha256:5e299ad827707c97…` |
| `mt7996e` | `net/wireless/mediatek/mt76/mt7996/mt7996e.ko.xz` | 4 | 8 by `firmware-mediatek`, 2 unshipped | `sha256:8b6ce51fe0ee6d14…` |
| `rt2800pci` | `net/wireless/ralink/rt2x00/rt2800pci.ko.xz` | 33 | 1 by `firmware-mediatek`, 2 unshipped | `sha256:e73a7fbebf50d393…` |
| `rt2800usb` | `net/wireless/ralink/rt2x00/rt2800usb.ko.xz` | 276 | 1 by `firmware-mediatek`, 2 unshipped | `sha256:71d0cef1a1c43e0a…` |
| `rt61pci` | `net/wireless/ralink/rt2x00/rt61pci.ko.xz` | 3 | 3 by `firmware-mediatek`, 2 unshipped | `sha256:6f3f2ba0d56f08ff…` |
| `rt73usb` | `net/wireless/ralink/rt2x00/rt73usb.ko.xz` | 75 | 1 by `firmware-mediatek`, 2 unshipped | `sha256:05b249845a30b793…` |
| `rtl8188ee` | `net/wireless/realtek/rtlwifi/rtl8188ee/rtl8188ee.ko.xz` | 1 | 1 by `firmware-realtek`, 2 unshipped | `sha256:e23b123e97e378c4…` |
| `rtl8192ce` | `net/wireless/realtek/rtlwifi/rtl8192ce/rtl8192ce.ko.xz` | 4 | 3 by `firmware-realtek`, 2 unshipped | `sha256:2e36186869a6555a…` |
| `rtl8192cu` | `net/wireless/realtek/rtlwifi/rtl8192cu/rtl8192cu.ko.xz` | 72 | 4 by `firmware-realtek`, 2 unshipped | `sha256:b630504c1d6863e1…` |
| `rtl8192de` | `net/wireless/realtek/rtlwifi/rtl8192de/rtl8192de.ko.xz` | 2 | 1 by `firmware-realtek`, 2 unshipped | `sha256:efb5e7883cb35d5e…` |
| `rtl8192ee` | `net/wireless/realtek/rtlwifi/rtl8192ee/rtl8192ee.ko.xz` | 1 | 1 by `firmware-realtek`, 2 unshipped | `sha256:9b9a4d0b3ca6d7c3…` |
| `rtl8192se` | `net/wireless/realtek/rtlwifi/rtl8192se/rtl8192se.ko.xz` | 5 | 1 by `firmware-realtek`, 2 unshipped | `sha256:7a45c9fc60864cdf…` |
| `rtl8723be` | `net/wireless/realtek/rtlwifi/rtl8723be/rtl8723be.ko.xz` | 1 | 2 by `firmware-realtek`, 2 unshipped | `sha256:7c63984910683d48…` |
| `rtl8821ae` | `net/wireless/realtek/rtlwifi/rtl8821ae/rtl8821ae.ko.xz` | 2 | 2 by `firmware-realtek`, 2 unshipped | `sha256:9067a18011c06293…` |
| `rtl8xxxu` | `net/wireless/realtek/rtl8xxxu/rtl8xxxu.ko.xz` | 33 | 13 by `firmware-realtek`, 3 unshipped | `sha256:a2ede1d9435d3952…` |
| `rtw88_8723de` | `net/wireless/realtek/rtw88/rtw88_8723de.ko.xz` | 1 | 1 by `firmware-realtek`, 2 unshipped | `sha256:5f7c9a4ede60da0d…` |
| `rtw88_8723du` | `net/wireless/realtek/rtw88/rtw88_8723du.ko.xz` | 2 | 1 by `firmware-realtek`, 2 unshipped | `sha256:607dffaabb939bda…` |
| `rtw88_8821ce` | `net/wireless/realtek/rtw88/rtw88_8821ce.ko.xz` | 2 | 1 by `firmware-realtek`, 2 unshipped | `sha256:2d795221a5dbdc7c…` |
| `rtw88_8821cu` | `net/wireless/realtek/rtw88/rtw88_8821cu.ko.xz` | 15 | 1 by `firmware-realtek`, 2 unshipped | `sha256:c42f087dc4804702…` |
| `rtw88_8822be` | `net/wireless/realtek/rtw88/rtw88_8822be.ko.xz` | 1 | 1 by `firmware-realtek`, 2 unshipped | `sha256:9a021688068636b4…` |
| `rtw88_8822bu` | `net/wireless/realtek/rtw88/rtw88_8822bu.ko.xz` | 29 | 1 by `firmware-realtek`, 2 unshipped | `sha256:c95bb5959c6d610a…` |
| `rtw88_8822ce` | `net/wireless/realtek/rtw88/rtw88_8822ce.ko.xz` | 2 | 2 by `firmware-realtek`, 2 unshipped | `sha256:7f166a8c3c34f103…` |
| `rtw88_8822cu` | `net/wireless/realtek/rtw88/rtw88_8822cu.ko.xz` | 7 | 2 by `firmware-realtek`, 2 unshipped | `sha256:40fe11d0ed664cb1…` |
| `rtw89_8851be` | `net/wireless/realtek/rtw89/rtw89_8851be.ko.xz` | 1 | 1 by `firmware-realtek`, 2 unshipped | `sha256:f0af69c38bb2a69b…` |
| `rtw89_8852ae` | `net/wireless/realtek/rtw89/rtw89_8852ae.ko.xz` | 2 | 1 by `firmware-realtek`, 2 unshipped | `sha256:eb2a7e36971f7cb2…` |
| `rtw89_8852be` | `net/wireless/realtek/rtw89/rtw89_8852be.ko.xz` | 2 | 1 by `firmware-realtek`, 2 unshipped | `sha256:1399c3773f3b196c…` |
| `rtw89_8852bte` | `net/wireless/realtek/rtw89/rtw89_8852bte.ko.xz` | 1 | 1 by `firmware-realtek`, 2 unshipped | `sha256:34f67274dc099f15…` |
| `rtw89_8852ce` | `net/wireless/realtek/rtw89/rtw89_8852ce.ko.xz` | 1 | 1 by `firmware-realtek`, 2 unshipped | `sha256:a395e83735855fff…` |
| `rtw89_8922ae` | `net/wireless/realtek/rtw89/rtw89_8922ae.ko.xz` | 1 | 1 by `firmware-realtek`, 2 unshipped | `sha256:7b82ed77d9cac58e…` |

"Unshipped" requests are files no Debian package provides under that name
(mostly `cfg80211`'s `regulatory.db`/`regulatory.db.p7s`, and newer firmware
revisions); no request of an active M1-F module resolves to any other
package.

### Collision and overlap results

- Firmware files shipped by two packages (among the 8 relevant packages):
  **none**.
- Device-alias overlap between rules with different package or
  classification: **none**.
- Device-specific claims by other modules: `radeon`/`radeonfb` (reviewed in
  M1-E, no firmware); `rt73usb`/`p54usb` and `rt73usb`/`rt2500usb` on
  `usb:v050Dp7050…` and `rtl8192se`/`r8192e_pci` on
  `pci:v000010ECd00008192…` — both IDs **excluded** from the rules. All other
  shared claims are within one package (e.g. `mt76x0u`/`mt76x2u`/`mt7601u`/
  `rt2800usb`, `rtw88_8822bu`/`rtw88_8822cu`).
- Reviewed foreign request: `brcmfmac` declares
  `brcm/brcmfmac43456-sdio.bin`, shipped by `raspi-firmware` — an SDIO-only
  chip, not mapped.

### Status matrix

| Package | Status | Active families | Deferred |
| --- | --- | --- | --- |
| `firmware-realtek` | **PARTIAL (active for Wi-Fi)** | rtlwifi, rtl8xxxu, rtw88, rtw89 (PCI/USB) | Ethernet (`r8169`, `r8152`, `r8153_ecm`), Bluetooth (`btusb`/`btrtl`), SDIO `rtw88_*s`, audio/card readers |
| `firmware-atheros` | **PARTIAL (active)** | `ar5523`, `ath10k_pci`, `ath6kl_usb`, `wil6210` (Wi-Fi); `ath3k` (Bluetooth) | `ath6kl_sdio`, `ath11k`, `ath12k` |
| `firmware-mediatek` | **PARTIAL (active for Wi-Fi)** | mt76 family, `mt7601u`, rt2x00 (`rt2800pci/usb`, `rt61pci`, `rt73usb`) | Bluetooth (`btusb`/`btmtk`), SoC/media/SDIO |
| `bluez-firmware` | **PARTIAL (active)** | `bcm203x` (BCM2033 USB Bluetooth) | Raspberry Pi/serial firmware |

## Not classified in M1-C

- USB devices (Bluetooth, Wi-Fi and others): no production USB rule yet;
  they stay unresolved.
- Hybrid or multi-GPU systems: rule v1 matches one target per selector and
  cannot express relationships between devices, so there is no
  `gpu.hybrid` rule.
- Any package, firmware, microcode, profile or source requirement (M1-D
  adds only the two CPU microcode mappings above).
