# Project State

Operational checkpoint for LUBARCHY. Baseline date: 2026-10-07.

## Summary

LUBARCHY is a clean Debian Stable-based desktop Linux distribution. The target
experience is: boot USB → simple installer (approximately five primary
screens) → reboot → usable desktop, with ordinary hardware working without
terminal work.

## Confirmed baseline

| Item | Value |
| --- | --- |
| Base | Debian Stable |
| Initial target | Debian 13 "Trixie", amd64 |
| Builder source | `debian-13.7.0-amd64-netinst.iso` |
| Production ISO mechanism | Debian `live-build` |
| Host package authority | APT/dpkg |
| Repository | `lubarchy/lubarchy` |
| Remote | `https://github.com/lubarchy/lubarchy.git` |
| Primary branch | `main` |

Accepted decisions are listed in [decisions.md](decisions.md).

## Current milestone: M1 — Hardware Enablement Foundation

**Goal:** establish the hardware-enablement foundation:
probe → normalized inventory → compatibility rules → package/profile plan →
later installation/configuration → later validation.

**M1 status: IN PROGRESS.**

### M1 execution steps

| Step | Description | Status |
| --- | --- | --- |
| M1-A | Hardware contracts: inventory/rule/plan v1 schemas, rule model, resolution plan, fixture tests | PASS (CTO accepted) |
| M1-B | Live read-only hardware probe producing inventory v1 | PASS (CTO accepted) |
| M1-C | Production resolver and high-confidence classification baseline | PASS (CTO accepted) |
| M1-D | Debian non-free-firmware policy and CPU microcode package mappings | PASS (CTO accepted) |
| M1-E | Device-specific firmware mapping baseline | PASS (CTO accepted) |
| M1-F | Second-wave network and Bluetooth firmware qualification | PASS (CTO accepted) |
| M1-G | NVIDIA driver strategy qualification and architecture decision evidence | PASS (CTO accepted); ADR-0002 remains Proposed |
| M1-H | NVIDIA repository containment and pinning proof | PASS (CTO accepted); ADR-0002 remains Proposed |
| M1-I | Privileged package apply transaction contract | PASS (CTO accepted) |
| M1-J | Debian source activation and staged package acquisition | PASS (CTO accepted) |
| M1-K | Privileged source activation and package apply executor | PASS (CTO accepted) |
| M1-L | Interrupted package transaction recovery | PASS (CTO accepted) |
| M1-M | Post-install hardware validation and health | PASS (CTO accepted) |
| M1-N | Package state retention and safe garbage collection | PASS (CTO re-accepted) / CLOSED after the APT sandbox `partial/` correction |
| M1-O | Hardware enablement orchestration and persistent state | PASS (CTO accepted) / CLOSED |
| M1-P | Not defined | Not started |

- M1-A — hardware contracts: **PASS** (CTO accepted). Implementation commit
  `91c54072cffe8ca84855904cc85ad7ffd7abac73`, qualified on GitHub Actions
  `Static gates` run #4
  ([38275430654](https://github.com/lubarchy/lubarchy/actions/runs/38275430654)),
  which passed all four gates on that commit. Scope: JSON Schema Draft 2020-12 contracts
  `lubarchy.hardware.inventory/v1`, `lubarchy.hardware.rule/v1` and
  `lubarchy.hardware.plan/v1` under `hardware/schemas/`; selector, priority
  and conflict semantics; source-requirement representation (Debian `main`
  is the only authorised source; anything else is reported as
  `policy_review_required`); canonical JSON and SHA-256 inventory digests; a
  test-only reference resolver (`tests/hwe_reference.py`); synthetic fixtures
  and a TEST-ONLY rule set under `tests/fixtures/hardware/`; the static gate
  `tests/hardware-contracts.py`, added to `tests/run-static.sh` and to the CI
  workflow (renamed `Static gates`). `python3-jsonschema` (Debian trixie
  `main`) was added to the builder as a QA dependency only. Contracts are
  documented in [hardware-enablement.md](hardware-enablement.md).
- M1-A does **not** probe hardware, install or configure drivers or
  firmware, change APT sources or components, change the ISO package set, or
  add production rules (`hardware/rules/` is empty).
- M1-B — live read-only hardware probe: **PASS** (CTO accepted).
  Implementation commit `4afa53fd14e983552dea1f8553366ddd13a59e6a`. `hardware/probe.py` (Python 3 standard library, no
  root) observes the running system and prints one canonical
  `lubarchy.hardware.inventory/v1` document: Debian architecture through
  `dpkg --print-architecture`, the kernel release, allowlisted
  `/proc/cpuinfo` fields, the firmware mode and the single standard
  SecureBoot EFI variable, virtualization through `systemd-detect-virt`, and
  PCI and USB devices from allowlisted sysfs attributes, with each modalias
  cross-checked against udev's `MODALIAS` property for that device only. The
  synthetic gate `tests/hardware-probe.py` is gate 5 of `tests/run-static.sh`
  and of the CI workflow; `Static gates` run #8
  ([38289415670](https://github.com/lubarchy/lubarchy/actions/runs/38289415670))
  passed all five gates on that commit. Live qualification ran on
  the builder VM only, unprivileged. Details:
  [hardware-enablement.md](hardware-enablement.md),
  [testing.md](testing.md).
- M1-B does **not** resolve packages, use the test-only resolver, install or
  configure anything, change APT sources, persist inventories, or run as a
  service. A valid builder-VM inventory is not real-hardware qualification:
  no physical Intel, AMD, NVIDIA, laptop, Wi-Fi, Bluetooth or USB hardware
  has been probed.
- M1-B udev interpretation (CTO accepted): sysfs is authoritative for the
  kernel MODALIAS; the per-device `udevadm` lookup is secondary
  corroboration. A matching value corroborates; an empty value or a non-zero
  exit means corroboration is unavailable (not proof that the property is
  absent); a missing executable, timeout or execution failure, or a
  non-empty differing value, fails the probe.
- M1-C — production resolver and classification baseline: **PASS** (CTO
  accepted). Implementation commit
  `67a659d02a845886be43ef71e32193a53786180e`. `hardware/resolver.py` (library)
  and `hardware/resolve.py` (CLI, `--inventory PATH|-`) resolve an inventory
  v1 against the production rules in `hardware/rules/` into a canonical plan
  v1, with `python3-jsonschema` (Debian `main`) for validation. The rule
  directory is fixed and fail-closed. The 12 production rules are
  classification-only (CPU vendor, display controllers from Intel/AMD/NVIDIA,
  Ethernet, other network controller, NVMe, SATA, VM/container/bare metal)
  and propose no package, profile or source; the source policy stays Debian
  `main` only. The synthetic gate `tests/hardware-resolver.py` (gate 6)
  checks byte parity with the M1-A reference resolver; `Static gates` run #10
  ([38294397684](https://github.com/lubarchy/lubarchy/actions/runs/38294397684))
  passed all six gates on that commit. Live probe → resolve
  qualification ran on the builder VM only. Details:
  [hardware-enablement.md](hardware-enablement.md),
  [testing.md](testing.md).
- M1-C does **not** map firmware, microcode or any package, enable another
  Debian component, decide NVIDIA strategy, classify hybrid graphics or USB
  devices, install or configure anything, or persist plans.
- M1-D — Debian `non-free-firmware` policy and CPU microcode mappings:
  **PASS** (CTO accepted). Implementation commit
  `4917b47948f5b3ab19a292a510a979f674e3c654`; `Static gates` run #12
  ([38321463299](https://github.com/lubarchy/lubarchy/actions/runs/38321463299))
  passed all six gates on that commit.
  [ADR-0001](decisions/0001-non-free-firmware.md) (Accepted, D-013) authorises
  Debian `non-free-firmware` for hardware enablement. The production
  resolver's source policy is now `main` + `non-free-firmware`; `contrib`,
  `non-free` and Backports stay unauthorised. Two production package rules
  map `GenuineIntel` → `intel-microcode` and `AuthenticAMD` →
  `amd64-microcode`, both `require` from `non-free-firmware`, verified
  against signature-verified Debian Trixie metadata. The production rule
  policy now allows classifications and `require` packages from authorised
  sources only. The M1-C exit-code-5 test gap is closed.
- M1-D does **not** install anything, change the builder's or the CI
  container's APT sources (still `main` only), change live-build or the ISO,
  map any device firmware (graphics, Wi-Fi, Bluetooth, network) or decide
  NVIDIA driver policy. Packages in a plan are proposals only.
- M1-E — device-specific firmware mapping baseline: **PASS** (CTO
  accepted). Implementation commit
  `b316042a960683fbf44f2279de0754db1b731abb`; `Static gates` run #14
  ([38362588631](https://github.com/lubarchy/lubarchy/actions/runs/38362588631))
  passed all seven gates on that commit. Four firmware families are mapped from
  qualified Debian kernel module aliases (`linux-image-6.12.111+deb13-amd64`
  6.12.111-1): `iwlwifi`/`iwl3945`/`iwl4965` → `firmware-iwlwifi`,
  `i915`/`xe` → `firmware-intel-graphics`, `amdgpu`/`radeon` →
  `firmware-amd-graphics`, `brcmfmac` (PCI/USB) → `firmware-brcm80211`, all
  `require` from `non-free-firmware`. `network.wifi` is emitted only for
  devices matching the qualified `iwlwifi`-family or `brcmfmac` aliases. The
  rules are generated by `tools/hwe/qualify-firmware-mappings.py` from
  committed, normalized alias evidence and checked for drift and
  cross-package overlap by the new gate 7 (`tests/hardware-firmware.py`).
  Evidence-driven scope refinement (CTO-approved during M1-E): `brcmsmac` is
  excluded because its only aliases are on the BCMA bus, which inventory v1
  cannot represent, and its cores are also claimed by `b43` (firmware from
  `contrib`, not authorised); SDIO aliases are excluded for the same reason.
- M1-E does **not** install anything, load modules, change the builder's or
  CI's APT sources, change live-build or the ISO, map Realtek, Atheros,
  Bluetooth, NVIDIA or other firmware, or qualify real hardware
  (firmware loading, Wi-Fi association, graphics acceleration).
- M1-F — second-wave network and Bluetooth firmware: **PASS** (CTO
  accepted). Implementation commit
  `d56d6eb4c5a61ab0e10f37b23635cf3a8fa8485c`; `Static gates` run #16
  ([38372395051](https://github.com/lubarchy/lubarchy/actions/runs/38372395051))
  passed all seven gates on that commit. Strengthened evidence chain: device
  modalias → kernel module alias → binding module and its dependency closure
  → declared firmware files (package-owned module metadata) → authenticated
  Debian package ownership (Contents indices and payload manifests). Active:
  Realtek Wi-Fi (`rtlwifi`, `rtl8xxxu`, `rtw88`, `rtw89` PCI/USB) →
  `firmware-realtek`; Qualcomm Atheros Wi-Fi (`ar5523`, `ath10k_pci`,
  `ath6kl_usb`, `wil6210`) and USB Bluetooth (`ath3k`) → `firmware-atheros`;
  MediaTek/Ralink Wi-Fi (`mt76` family, `mt7601u`, `rt2800pci/usb`,
  `rt61pci`, `rt73usb`) → `firmware-mediatek`; BCM2033 USB Bluetooth
  (`bcm203x`) → `bluez-firmware`. Deferred with recorded reasons: Realtek
  Ethernet (`r8169`, `r8152`, `r8153_ecm`), `btusb`-based Bluetooth,
  SDIO/serial modules, `ath11k`/`ath12k`. Two ambiguous device IDs are
  excluded. Firmware evidence moved to the versioned format
  `lubarchy.hardware.firmware-evidence/v2`; the M1-E rules regenerate
  unchanged. The production package set is now 10.
- M1-F does **not** install anything, load modules, change the builder's or
  CI's APT sources, change live-build or the ISO, map NVIDIA, or qualify real
  hardware.
- M1-G — NVIDIA driver strategy qualification and architecture decision
  evidence: **PASS** (CTO accepted; ADR-0002 remains Proposed). Implementation
  commit `9beb35d211f5def6b5642d5bb31ef830bf1f6ae4`; `Static gates` run #18
  ([38437605209](https://github.com/lubarchy/lubarchy/actions/runs/38437605209))
  passed all eight gates on that commit. [ADR-0002](decisions/0002-nvidia-driver-strategy.md) is
  **Proposed** and decides nothing. The evidence compares Debian Stable 550,
  `trixie-backports`, NVIDIA's signed Debian 13 repository and Nouveau:
  sources, signing, branches, security state (93 open trixie issues for
  Debian 550, no DSA), GPU generation support, Wayland, Secure Boot,
  kernel-update and rollback requirements, and isolated test-only APT
  simulations. Committed under `hardware/nvidia/` with a normalized NVIDIA
  supported-GPU snapshot that is not used in production. Gate 8
  (`tests/nvidia-strategy.py`) enforces the evidence consistency and the
  policy boundary. Details:
  [hardware-enablement.md](hardware-enablement.md#nvidia-driver-strategy-evidence-m1-g).
- M1-G does **not** authorise or add any NVIDIA source, key, package, driver,
  kernel module or production rule. It does not enable `contrib`/`non-free`/
  Backports, change the builder's, the ISO's or the CI's APT sources, install
  anything, build DKMS modules, load modules, add `gpu.hybrid`, change the
  hardware schemas, or decide P-006.
- M1-H — NVIDIA repository containment and pinning proof: **PASS** (CTO
  accepted). Containment conclusion: YES — NVIDIA's Debian 13 repository can
  be technically contained as a candidate D-005 exception. The repository
  remains unauthorised, the NVIDIA policy remains inactive, ADR-0002 stays
  Proposed, and there is no NVIDIA package mapping. Implementation commit
  `cd8b43fc329a8093c43e97d388689f4386dc3c34`; `Static gates` run #20
  ([38474658686](https://github.com/lubarchy/lubarchy/actions/runs/38474658686))
  passed all eight gates on that commit. It tests whether
  NVIDIA's signed Debian 13 repository can be contained by a LUBARCHY-owned,
  **inactive** APT policy: default deny for Origin `NVIDIA` / Label
  `NVIDIA CUDA`, a finite allowlist of the 30 binaries of the
  `nvidia-driver` + `nvidia-kernel-open-dkms` closure, a branch-615 lock and
  Debian-infrastructure protection (`dkms` stays Debian). Isolated builder
  simulations show the uncontained takeover, default deny, the contained
  install/upgrade/source-disable/rollback paths, the negative and
  branch-escape tests, and the vendor pinning package's broader scope.
  Recommendation: **YES**, technically containable as a candidate D-005
  exception for LUBARCHY-managed automatic transactions. The repository
  remains unauthorised. APT preferences are not a sandbox or MAC mechanism
  against root: an explicit exact-version administrator request
  (`pkg=version`) can override candidate selection in APT 3.0.3. That is
  recorded as evidence and sits outside the containment boundary. Artifacts:
  `hardware/nvidia/apt-policy-v1.json`, `apt-preferences.example`,
  `apt-fixtures-v1.json` and `tools/hwe/validate-nvidia-apt-policy.py`; gate 8
  now has 19 checks. Details:
  [hardware-enablement.md](hardware-enablement.md#nvidia-repository-containment-m1-h).
- M1-H does **not** authorise the NVIDIA repository, commit key material,
  install anything, change the builder's, the ISO's or the CI's APT sources,
  change live-build, the resolver source policy or the production package
  set (10), or decide ADR-0002 or P-006.
- Future M1 requirement (apply/install step): a LUBARCHY privileged package
  executor that installs only a validated, source-controlled plan, never
  forwards caller-controlled package names, versions, source selectors or
  APT options, revalidates before mutation and records the transaction.
- M1-I — privileged package apply transaction contract: **PASS** (CTO
  accepted). Implementation commit
  `3378afb53bd96644f740e79fb23a2a30f88c874c`; `Static gates` run #22
  ([38519747096](https://github.com/lubarchy/lubarchy/actions/runs/38519747096))
  passed all nine gates on that commit, with the package transaction gate at
  17 checks. New downstream
  contract `lubarchy.hardware.package-transaction/v1`
  (`hardware/schemas/package-transaction-v1.schema.json`; inventory, rule
  and plan v1 unchanged). The compiler (`hardware/package_transaction.py`,
  read-only CLI `hardware/compile-transaction.py` with `compile`, `verify`
  and `validate`) derives package names only from a freshly recomputed
  production plan. It binds the inventory, rule set, plan, installed state,
  holds, APT automatic/manual marks, package-manager configuration and
  repository state; selects an install-only, no-recommends change set; and
  records exact versions, architectures, authorised Debian provenance,
  SHA-256 and intended package marks for the complete closure. Only direct
  requirements become explicit APT targets, so dependencies stay
  APT-selected and automatic. Package architecture is bound and verified
  against the closure simulation. Stale inputs fail closed. The future
  privileged executor, its argv and environment contract, the lock and the
  state machine are documented, not implemented. Gate 9
  (`tests/hardware-transaction.py`) runs synthetic only. Details:
  [package-transactions.md](package-transactions.md).
- M1-I does **not** install, upgrade, remove or configure any package,
  activate a source, take a package-manager lock, create a privileged
  daemon/setuid/sudoers/polkit path, change the production rules, the
  resolver source policy (`main` + `non-free-firmware`) or the production
  package set (10), or authorise NVIDIA.
- M1-J — Debian source activation and staged package acquisition:
  **PASS** (CTO accepted). The source-activation contract, the staged-bundle
  contract, exact authenticated payload staging and the offline
  (`--no-download`) future apply model are qualified. No source or package
  mutation had occurred before M1-K. Implementation commit
  `0fab8f899a356685fc6b33117953e333e30742ac`; `Static gates` run #24
  ([38539169890](https://github.com/lubarchy/lubarchy/actions/runs/38539169890))
  passed all ten gates on that commit, with the package staging gate at 19
  checks. Details: [package-staging.md](package-staging.md).
  - Source activation (`hardware/source_activation.py`, CLI
    `hardware/compile-source-activation.py`; contract
    `lubarchy.packages.source-activation/v1`) realises the accepted D-013
    policy (`main` + `non-free-firmware`, Debian archive keyring) without a
    new policy decision. It inspects `/etc/apt` read-only, normalizes and
    digests the source pre-state, and renders one LUBARCHY-owned deb822 file
    (`/etc/apt/sources.list.d/lubarchy-debian.sources`). Foreign files are
    never modified, and ambiguous or unrecognised topologies fail closed. On
    the builder (installer one-line `sources.list`, `main` only, no
    `Signed-By`) the plan adds exactly `non-free-firmware` for the same URIs
    with `Signed-By`. The plan is rendered and authenticated only in
    isolated workspaces; `/etc/apt` is not written.
  - Staging (`hardware/package_staging.py`, CLI `hardware/stage-packages.py`;
    manifest `lubarchy.packages.staged-bundle/v1`) acquires exactly the
    complete change set of a qualified M1-I transaction. Each payload is
    verified by type, APT file name, SHA-256 and size from the
    authenticated index, and `dpkg-deb` identity. The bundle binds the
    transaction, source-activation and repository metadata digests, and any
    change of one of them requires restaging.
  - The future offline apply model (`--no-download`, direct targets only,
    APT archive cache pointed at the verified bundle) was proven on the
    builder in simulation and in a non-root no-op-dpkg sandbox, with marks
    preserved. APT 3.0.3 does not re-hash cached archives, so LUBARCHY's
    bundle verification is the authoritative integrity control.
  - Gate 10 (`tests/hardware-staging.py`) is synthetic and offline. It also
    carries a committed hash baseline of the protected contract schemas,
    which is shallow-CI safe.
- M1-J does **not** activate a source, write `/etc/apt` or
  `/var/lib/lubarchy`, install, upgrade, remove or configure any package,
  change a package mark, implement or run the privileged apply path, change
  the inventory, rule, plan or package-transaction contracts, the resolver,
  the production rules or the production package set (10), or authorise
  NVIDIA.
- M1-K — privileged source activation and package apply executor:
  **PASS** (CTO accepted). Qualified:
  - root-owned exact-commit privileged execution;
  - source activation and an authenticated metadata refresh;
  - exact staged payloads, the immediate pre-apply re-hash and the final
    stale-state revalidation;
  - the transaction lock and the durable journal;
  - one real APT apply, post-apply validation and a reboot;
  - VM baseline restoration.

  Implementation commit
  `bee741485ce2fb6db35d433b8c0102bc59824e9f`; `Static gates` run #26
  ([38560415459](https://github.com/lubarchy/lubarchy/actions/runs/38560415459))
  passed all eleven gates on that commit, with the package staging gate at 19
  checks and the package apply gate at 14. Details:
  [package-apply.md](package-apply.md).
  - Qualification: one corrected real privileged package transaction on the rollback-protected
    builder VM, executed only from the root-owned exact-commit deployment
    `/root/lubarchy-m1k/bee741485ce2fb6db35d433b8c0102bc59824e9f/`.
    It performed:
    - source activation of the LUBARCHY-owned file;
    - an authenticated metadata refresh;
    - a fresh transaction and root-owned staging;
    - an immediate payload re-hash and final stale-state revalidation;
    - one journaled offline APT apply (`intel-microcode` direct, `iucode-tool`
      dependency);
    - package and mark validation, `dpkg --audit` and `apt-get check`;
    - a reboot validation.

    The VM checkpoint was then restored, and a complete audit matched the
    pre-M1-K baseline. CI never performs a real mutation: gate 11 uses a fake
    host and synthetic trees.
  - `hardware/package_apply.py` and the root-only CLI
    `hardware/apply-packages.py` (`apply`, `status`) apply the current
    qualified hardware plan. The executor takes no package, option, source
    or path input and never escalates privilege itself.
  - Chain: LUBARCHY transaction lock → durable journal
    (`lubarchy.packages.apply-journal/v1`) → activation of the
    LUBARCHY-owned Debian source file only → authenticated `apt-get update`
    → fresh probe, resolve and transaction → root-owned staging → final
    stale-state revalidation and immediate payload re-hash → one offline APT
    install (direct targets only, `--no-download`, `--no-remove`, no
    recommends) → validation of the exact package, mark, hold, source and
    file state.
  - APT/dpkg installation is not atomic. Failures after APT has started are
    journaled and never repaired automatically; later runs report
    `recovery_required`.
  - Privileged LUBARCHY code runs only from a root-owned,
    unprivileged-nonwritable, content-addressed deployment of the exact
    reviewed commit (`tools/hwe/trusted-deployment.py`; qualification-only
    location `/root/lubarchy-m1k/<commit>/`). It never runs from a
    user-writable checkout.
  - Gate 11 (`tests/hardware-apply.py`) uses a fake host and never mutates
    anything. The first real package-manager mutation is authorised only on
    the rollback-protected builder VM. The procedure is a libvirt checkpoint,
    one real apply of the exact commit, post-apply and reboot validation,
    then restoring the checkpoint ([testing.md](testing.md)). Results are
    recorded in the evidence bundle for the exact commit. Package success on
    a VM is not physical hardware validation.
- M1-K does **not** add a privilege-escalation path (sudo, setuid, sudoers,
  polkit, daemon), implement interrupted-transaction recovery, support
  NVIDIA, `contrib`, `non-free`, Backports or third-party sources, change the
  inventory, rule, plan, package-transaction, source-activation or
  staged-bundle contracts, the resolver, the production rules or the
  production package set (10).
- M1-L — interrupted package transaction recovery: **PASS** (CTO accepted).
  Implementation commit
  `3b0ec8869a3034c7df38c25c004387bf99750d7b`; workflow `Static gates` run #28
  ([38666440151](https://github.com/lubarchy/lubarchy/actions/runs/38666440151)): all twelve gates passed (gate 10: 19, gate 11: 14,
  gate 12: 15 checks); documentation commit
  `2610a1db5f1ec49ffe99e19eedad07bbeea5b592`, run #29
  ([38666799577](https://github.com/lubarchy/lubarchy/actions/runs/38666799577)).
  The qualification snapshot was deleted after closure. Details:
  [package-recovery.md](package-recovery.md).
  - `hardware/package_recovery.py` and `hardware/apply-packages.py recover`
    classify an interrupted attempt from the real dpkg/APT state. The
    attempt is attributed to the transaction by reconstructing the bound
    pre-state digest and re-verifying the original root-owned bundle. The
    classifications are `no_mutation`, `no_package_change`,
    `applied_but_unvalidated`, `bounded_incomplete_transaction` and
    `manual_recovery_required`.
  - The transaction closure stays the M1-I install closure. Packages
    outside it whose trigger the exact transaction activated form a separate
    recovery impact set, proven from evidence bound in the apply journal
    before `applying` (dpkg log cursor and prefix digest, pre-apply
    trigger-interest records) and the dpkg log written after it.
  - The only recovery mutation is exact-package `dpkg --configure
    --no-triggers` (trigger processing suppressed), then a separately
    authorised `dpkg --triggers-only`, limited to non-final closure packages
    plus non-final impact-set packages. Each command is contained by its own
    dpkg log cursor and a re-inspection; recovery-induced work in any other
    package ends in manual recovery, never in automatic execution. It was measured on the builder: an APT
    rerun, `apt-get -f install` and `dpkg --configure -a` all touch unrelated
    pending packages, so none of them is used.
  - A complete post-state is validated without re-running anything.
    Unrelated, unattributable or unprovable damage fails closed to manual
    recovery.
  - The durable recovery journal (`lubarchy.packages.recovery-journal/v1`)
    and the shared transaction lock gate new applies until the attempt is
    resolved.
  - Gate 12 (`tests/hardware-recovery.py`) is synthetic. Real interruption
    scenarios are qualified only on the rollback-protected builder VM
    ([testing.md](testing.md)).
- M1-L does **not** perform generic package repair, run APT or a download
  during recovery, upgrade, downgrade or remove a package, change a package
  mark, re-unpack half-installed packages, add privilege escalation, or
  support NVIDIA, `contrib`, `non-free` or Backports.
- M1-M — post-install hardware validation and health: **PASS** (CTO
  accepted). PASS qualifies the validation subsystem itself; it does not
  mean every hardware target or the current builder state passes.
  Implementation commit
  `59793b6dcdaddf73946dcdd9a6681ac58b2fc644`; workflow `Static gates` run #30
  ([38732417272](https://github.com/lubarchy/lubarchy/actions/runs/38732417272)): all thirteen gates passed (gate 10: 19, gate 11: 14, gate 12: 15,
  gate 13: 13 checks); documentation commit
  `a40f6a9ebeb6506f1acda596a9a6e3074acaecdb`, run #31
  ([38732989639](https://github.com/lubarchy/lubarchy/actions/runs/38732989639)).
  - **Local exact-commit qualification:** aggregate exit 0; mutations M1-M
    43/43, M1-L 51/51, M1-K 46/46, zero survivors/errors.
  - **Read-only builder qualification:** no M1-M snapshot, reboot freshness
    test passed, builder shut off. The builder's own validation result
    correctly remains `FAIL` (`intel-microcode` is absent from the restored
    baseline while the plan requires it); VM microcode is `LIMITED`, and no
    physical Wi-Fi, Bluetooth or GPU function is claimed.
  - Details: [hardware-validation.md](hardware-validation.md).
  - `hardware/hardware_validation.py` and `hardware/validate-hardware.py`
    answer whether the qualified plan resulted in a healthy system. A fresh
    probe and the existing resolver give the plan; a source-controlled
    validation profile (keyed by the plan's classifications and package
    rules, with firmware-family kernel modules from the committed firmware
    evidence) says which properties each implies; the running system is
    compared against it.
  - The result contract `lubarchy.hardware.validation-result/v1` binds the
    inventory, rule set, plan, installed package state, boot identity and
    profile; any change makes a result stale. Checks are attributed to
    inventory device identities and report PASS, FAIL, LIMITED or
    NOT_TESTED with a machine-readable reason; a critical FAIL fails the
    whole result and LIMITED/NOT_TESTED are never promoted to PASS.
  - Firmware request failures are classified by a source-controlled
    requirement policy (known-required → FAIL, known-optional or
    unclassified → LIMITED). The production policy is empty, so every
    attributed failure is unclassified until real-hardware qualification. The
    embedded firmware-family module lists are a derived, gate-enforced copy
    of the canonical `tools/hwe/firmware-mappings.json` (CTO-accepted
    coupling).
  - It is read-only and unprivileged by default; root only adds the kernel
    log on restricted systems and the LUBARCHY apply journal (exact
    versions). A VM never proves physical microcode, radio or GPU function,
    and NVIDIA drivers are not qualified (NOT_TESTED).
  - Gate 13 (`tests/hardware-validation.py`) is synthetic.
- M1-M does **not** install, configure, load or bind anything, repair
  packages, change hardware policy or rules, add privilege escalation,
  qualify NVIDIA, or replace real-hardware qualification.
- M1-N — package state retention and safe garbage collection: **PASS**
  (CTO accepted). The PASS qualifies the LUBARCHY package-state GC subsystem
  only; it is not generic filesystem cleanup. Implementation commit `1eec088956d73cdc53bf450a76d860cbe4ec7e41`; workflow
  `Static gates` run #33 ([38816122990](https://github.com/lubarchy/lubarchy/actions/runs/38816122990)): all fourteen gates passed (gate 10: 19, gate 11: 14, gate 12: 15,
  gate 13: 13, gate 14: 8 checks); documentation commit
  `3ac2961f26590c98b18127ce20c255a3467ca86a`, run #34
  ([38816686555](https://github.com/lubarchy/lubarchy/actions/runs/38816686555)).
  - **Local exact-commit qualification:** aggregate exit 0, 165/165
    mutations killed (M1-N 25, M1-M 43, M1-L 51, M1-K 46). An earlier run
    exited 1 only because of two stale harness mutation targets (L14,
    K02); it was not a product failure.
  - **Real builder GC qualification** under `pre-m1n`:
    - eligible state was removed;
    - the protected manual-recovery material survived (still verifiable,
      apply still blocked), and so did the corrupt material and the
      unknown sentinel;
    - the second run was idempotent;
    - APT, dpkg, source and package state was unchanged;
    - the restore to baseline was exact.

    `pre-m1n` was kept until CTO closure and has since been deleted.
  - The PASS, once given, qualifies the GC subsystem only; it does not
    authorise generic cleanup.
  - Details: [package-garbage-collection.md](package-garbage-collection.md).
  - `hardware/package_gc.py` with `apply-packages.py gc-plan` and `gc` (root
    only, no caller input) classifies every staging bundle and temporary
    staging directory from the apply and recovery journals: protected
    (active, recovery, unknown), eligible (complete, abandoned) or
    invalid/ambiguous, with reason codes.
  - Only eligible state is deleted: verified bundles together with the
    payload links of their attempt caches, and exact-pattern temporary
    staging directories. Journals, unknown, corrupt, ambiguous and
    recovery-relevant state, APT's cache lock file and everything outside
    the fixed roots are preserved; time and mtimes are never authority.
  - `gc` recomputes the plan under the shared transaction lock,
    re-verifies each candidate's identity immediately before deleting it
    through directory descriptors without following links, and writes a
    durable receipt (`lubarchy.packages.gc-receipt/v1`) before the first
    deletion; partial failures are recorded as such.
  - Gate 14 (`tests/hardware-gc.py`) is synthetic. Real GC is qualified
    only on the builder under the `pre-m1n` rollback checkpoint.
- M1-N does **not** delete journals or receipts, use time-based cleanup,
  touch APT/dpkg/source state, add a privilege mechanism, or start M1-O.
- M1-N correction — APT sandbox ownership of the attempt cache `partial/`:
  **PASS (CTO re-accepted) / CLOSED.**
  - **Scope:** M1-N remains narrowly scoped. The only accepted non-root
    ownership exception is the exact `<attempt>/apply-cache/partial/`,
    under the qualified APT sandbox contract (`_apt` from the root-owned
    `/etc/passwd`, group root, mode `0700`, empty, APT started, re-checked
    before removal). Every other GC object remains root-only, and Debian's
    APT sandbox is not weakened or overridden.
  - **Chain:** product correction `dfd82cb`, test-only harness fix
    `9e7ad19`, qualification docs `cfd6d0a` (`Static gates` run #38,
    [38897405669](https://github.com/lubarchy/lubarchy/actions/runs/38897405669): 14/14 gates, gate 14 at 10 checks).
  - **Product correction:** `dfd82cb0fecf3f8464f54377f1bd32acf2f4bbc5`.
    - Its first CI run, `Static gates` run #36
      ([38888081993](https://github.com/lubarchy/lubarchy/actions/runs/38888081993)), failed only gate 14.
    - **Cause:** a synthetic inode-reuse defect in the gate's ownership
      overlay. On the runner's filesystem a re-created `partial/` reused
      the freed inode number and inherited the removed object's `_apt`
      overlay. No product failure was found.
  - **Test-only harness fix:** `9e7ad19a17cfecd7a583735286b8b4a2a79a5916`.
    - The overlay now ends with its object's lifetime.
    - Deterministic same-inode-identity cases were added; under a forced
      reuse simulator the old harness reproduces run #36 exactly.
    - Only `tests/hardware-gc.py` changed; `hardware/package_gc.py` is
      byte-identical to `dfd82cb`.
    - CI: `Static gates` run #37 ([38896919894](https://github.com/lubarchy/lubarchy/actions/runs/38896919894)) passed
      14/14 gates (gate 10: 19, gate 11: 14, gate 12: 15, gate 13: 13,
      gate 14: 10 checks).
  - **Exact-commit qualification** of `9e7ad19` (the same 182 mutations
    were also run on `dfd82cb`): aggregate exit 0; 182/182 mutations
    killed (correction 17, M1-N 25, M1-M 43, M1-L 51, M1-K 46), zero
    survivors or harness errors.
  - **Real builder qualification** (of `dfd82cb`; product bytes unchanged
    since) under `pre-m1n-fix`:
    1. A real apply produced the APT-created `partial/`
       (`_apt:root`, mode `0700`, empty).
    2. The completed bundle became `eligible_complete`.
    3. Real GC reclaimed 12,905,139 bytes (bundle, cache payload links,
       `partial/`). The apply journal and APT's cache `lock` were retained
       and the receipt was exact.
    4. A second GC was `nothing_to_collect`.
    5. APT, dpkg, source, package, mark and hold state was unchanged by GC.
    6. The restore to baseline was exact.

    `pre-m1n-fix` was retained until CTO closure and was then deleted
    cleanly.
  - **Defect:** real-builder evidence from the M1-O qualification showed
    that APT 3.0.3 changes `<attempt>/apply-cache/partial/` to its sandbox
    user (`_apt:root`, mode `0700`). The original M1-N treated that as
    `unsafe_object`, so a really applied bundle could never be reclaimed.
    This failed safe but was functionally wrong.
  - **Correction:** the only non-root object GC accepts is that exact empty
    directory. It requires `_apt` resolved from the root-owned
    `/etc/passwd`, group root and mode `0700`, and an attempt whose APT run
    started. It is re-checked before removal.
  - Debian's APT sandbox is kept, and every other object remains root-only.
    Details: [package-garbage-collection.md](package-garbage-collection.md).
  - Gate 14 models the measured ownership (10 checks). The real
    qualification ran on the builder under `pre-m1n-fix`, which was deleted
    cleanly after M1-N closure.
- M1-O — hardware enablement orchestration and persistent state:
  **PASS (CTO accepted) / CLOSED.** Qualification docs `03be7dc`
  (`Static gates` run #41, [38980651256](https://github.com/lubarchy/lubarchy/actions/runs/38980651256): 15/15 gates).
  The qualification snapshot `pre-m1o` was deleted cleanly in a separate
  post-closure cleanup; the builder has no qualification snapshots. Rebuilt
  on the corrected M1-N baseline (`8b77e24`); an earlier unpublished
  candidate was superseded and is kept only as historical evidence.
  Details: [hardware-orchestration.md](hardware-orchestration.md).
  - `hardware/hardware_orchestration.py` with `manage-hardware.py status`
    and `reconcile` (root only, no caller input) composes the qualified
    primitives: package recovery first (M1-L), fresh probe and production
    plan, a read-only package precheck, the existing apply executor only when
    a package change is required (M1-K), validation (M1-M), and GC (M1-N)
    only after a terminal non-critical validation.
  - Validation outcomes follow a strict precedence: `validation_failed`,
    then `reboot_required`, then `validation_limited`, then `complete`. A
    critical FAIL is never hidden behind a reboot request, and a failed run
    never becomes a reboot continuation.
  - Same-boot validation without a critical FAIL ends `reboot_required`
    (exit 5); the coordinator never reboots. The next run proves a boot change and revalidates against
    the pending result (continuity) before any terminal outcome. LIMITED is
    never promoted; GC failure is reported separately.
  - One durable, digest-chained run journal
    (`lubarchy.hardware.orchestration-journal/v1`) plus the canonical
    inventory, plan and validation result per run under the fixed root
    `/var/lib/lubarchy/hardware-orchestration/`. Corrupt or unsafe state
    fails closed and is preserved. Lock order: orchestration lock, then the
    package transaction lock.
  - Gate 15 (`tests/hardware-orchestration.py`) is synthetic. Real
    end-to-end reconciliation is qualified only on the builder under the
    `pre-m1o` rollback checkpoint.
  - **Published:** implementation `4fc443d0540be51028f24e5569fbaaa5fdf60d97`,
    parent `8b77e24` (the corrected M1-N closure). The earlier unpublished
    candidate `a27c392` is kept only as local historical evidence.
  - **CI:** `Static gates` run #40
    ([38980191190](https://github.com/lubarchy/lubarchy/actions/runs/38980191190)) on `4fc443d` passed 15/15 gates (gate 10:
    19, gate 11: 14, gate 12: 15, gate 13: 13, gate 14: 10, gate 15: 21
    checks).
  - **Local exact-commit qualification:** aggregate exit 0. 216/216
    mutations were killed: M1-O 34/34, plus the corrected baseline 182/182
    (M1-N correction 17, M1-N 25, M1-M 43, M1-L 51, M1-K 46). There were
    zero survivors and zero harness errors.
  - **Validation precedence:** `validation_failed` > `reboot_required` >
    `validation_limited` > `complete`.
  - **Real builder lifecycle** under `pre-m1o`:
    1. The first reconcile did one apply (`intel-microcode` direct,
       `iucode-tool` dependency) and ended `reboot_required`. The product
       did not reboot: the boot identity was unchanged.
    2. The qualification harness then rebooted the builder externally.
    3. The post-reboot reconcile did a continuity revalidation, with no
       duplicate apply or recovery, and ended `validation_limited`. The
       corrected M1-N GC reclaimed the real bundle (12,905,139 bytes)
       despite APT's real `_apt:root 0700` `partial/`.
    4. A third run was idempotent (`nothing_to_collect`).
    5. VM microcode stayed `LIMITED/virtualized_cpu`.
    6. The restore to baseline was exact.

    `pre-m1o` was kept until closure and then deleted cleanly; the builder
    is shut off with autostart disabled.
- M1-O does **not** add a systemd unit, timer, daemon, installer UI,
  automatic reboot, NVIDIA support, Secure Boot/MOK or a privilege
  mechanism, and does not start M1-P.
- M1-P has not been defined or started.

## M0 — Minimal reproducible ISO (closed)

**Goal:** prove LUBARCHY can be built cleanly from source-controlled
configuration before adding desktop or installer complexity.

**M0 status: PASS** (M0-J, 2026-10-12). M0 is closed.

### M0 exit checklist

- [x] Fresh public repository with governance baseline (M0-C; published in M0-J)
- [x] Clean Debian builder (M0-D; see [build.md](build.md))
- [x] Documented build prerequisites (builder and live-build tooling; see [build.md](build.md))
- [x] Committed `live-build` configuration (M0-E; built from M0-F onward)
- [x] Minimal ISO builds (M0-F)
- [x] Checksum and package manifest (M0-F; initial artifact metadata)
- [x] Successful UEFI QEMU/KVM boot (M0-G; optical/El Torito EFI)
- [x] Automated boot smoke test (M0-G; `tests/iso-boot-smoke.sh`)
- [x] Virtual UEFI removable-media/hybrid qualification (M0-H; OVMF USB mass-storage, not physical hardware)
- [x] Clean rebuild succeeds (M0-I; bit-for-bit reproducible under identical Debian repository inputs)
- [x] Lint/CI baseline passes (repository lint and ShellCheck since M0-I; GitHub Actions `M0 static gates` green since M0-J)
- [x] Build documentation accepted (M0-J final review)
- [x] Traceability from Git commit to artifacts and tests (commit SHAs recorded with every build, test and CI run)

An item is checked only when evidence exists and the CTO has reviewed it.

### M0 execution steps

| Step | Description | Status |
| --- | --- | --- |
| M0-A | Read-only preflight and Debian ISO trust verification | PASS |
| M0-B | Explicit local repository binding | PASS |
| M0-C | Repository/governance baseline | PASS |
| M0-D | Clean Debian 13 builder environment | PASS |
| M0-E | live-build tooling and source-controlled configuration | PASS |
| M0-F | First minimal ISO build and artifact capture | PASS |
| M0-G | UEFI optical boot qualification and serial smoke baseline | PASS |
| M0-H | Hybrid removable-media and unattended UEFI qualification | PASS |
| M0-I | Deterministic build gates and clean-rebuild reproducibility | PASS |
| M0-J | Publication, real GitHub CI, final review and M0 acceptance | PASS |

- M0-C — repository/governance baseline: **PASS** (CTO-reviewed), commit
  `c0a665985f54de33d813ea1bf62794e7170299be`.
- M0-D — clean Debian builder: **PASS**. `lubarchy-builder` on
  `qemu:///system` is established and qualified (2026-10-08); measured
  baseline in [build.md](build.md).
- M0-E — live-build tooling and configuration: **PASS**. `live-build
  1:20250505+deb13u1` from Debian trixie `main` is installed on the builder;
  the M0 configuration lives in `build/live` and passes
  `tests/live-build-config.sh` on the builder.
- M0-F — first minimal ISO: **PASS**. `lubarchy-m0-amd64.hybrid.iso` was built
  from `a7d1e9c10df6fbe3d2a9a30e6bb3378dcabf1073`, statically inspected,
  checksummed and retained outside Git (see [build.md](build.md)).
- M0-G — boot qualification: **PASS**. The M0-F ISO passed the isolated UEFI
  optical boot test and the automated serial live-payload smoke test
  (`tests/iso-boot-smoke.sh`). USB/removable-media boot of that ISO: not
  qualified (no hybrid system area).
- M0-H — hybrid and unattended boot: **PASS**. A new ISO built from
  `fc4c03470fbca0376d52cd87dec95a51da3e97cf` (`grub-pc grub-efi` for the
  live-build hybrid system area, 5-second GRUB timeout) passed the hybrid
  structure check, unattended UEFI optical boot, UEFI removable-media boot
  from virtual USB mass-storage, and the serial smoke test. **Physical USB
  real-hardware boot: NOT YET QUALIFIED.**
- M0-I — deterministic build and reproducibility: **PASS**. The first A/B
  rebuild of `47fac2aafd6b8e4d117335b571fdc6f1a42d2046` was NO-GO: APT's
  `/var/cache/apt/pkgcache.bin` embedded build time. A chroot-hook remedy was
  investigated and rejected (not deployed). The fix,
  `build/live/config/rootfs/excludes` (APT binary caches only), was committed
  as `2a2eda4b1c209be7dd5eb668e46402a181f308f6`. Two independent builds of
  that commit (SOURCE_DATE_EPOCH `1791538744`, the commit time) produced
  byte-identical ISOs, SHA-256
  `d697b42871477c5ac5e5055df3472986a55ca9f6216b5110d1cc2b11a2c33db3`, under
  identical, signature-verified Debian repository inputs. Both passed the
  hybrid structure check and the serial smoke test. Repository lint,
  ShellCheck (0 findings) and the live-build configuration gate PASS. Details
  are in [build.md](build.md). At the end of M0-I the pinned static CI
  workflow had not yet run on GitHub (see M0-J). Historical reproducibility
  against moving mirrors is not established.
- M0-J — publication and acceptance: **PASS**. The 12 Debian-generation
  commits were pushed normally to `main` (`f10f256..f79c732`) after a
  history-wide secrets and artifact audit. The first GitHub Actions run of
  `M0 static gates` on `f79c7324b534a37f6ec6c6566cb6615425ad6bce`
  ([37933581297](https://github.com/lubarchy/lubarchy/actions/runs/37933581297), run #1) **failed** at repository lint: the
  job runs as root in the `debian:trixie` container while the checkout is
  owned by the runner user, and Git refused the workspace ("dubious
  ownership"). The CTO approved a workflow-only fix (per-step
  `GIT_CONFIG_*` `safe.directory` for exactly `$GITHUB_WORKSPACE`, no config
  file written), committed as `ce2462e4e3a6b1016e5fe5d23e8dfd4d7d1361ba`.
  Run #2 ([37972612802](https://github.com/lubarchy/lubarchy/actions/runs/37972612802)) on that commit **succeeded**: repository
  lint, ShellCheck (9 scripts, 0 findings) and the live-build configuration
  gate all passed. The final repository and documentation review found no
  contradiction with accepted architecture and no broken local links.

A step's status is updated only after its execution has been validated and
reviewed by the CTO.

### Verified builder source

- File: `debian-13.7.0-amd64-netinst.iso`
- SHA-512 (from cryptographically authenticated Debian `SHA512SUMS`):
  `ef04d0276850d70e4aaec0b55003628660e9631b26e35215a70ab3c8c707294a4b12731bc73c75a932902f703a7c278310ca99df28d6b36fb06d5618d8d0f678`
- Debian CD signing key fingerprint:
  `DF9B 9C49 EAA9 2984 3258 9D76 DA87 E80D 6294 BE9B`

### Repository reference

- Initial GitHub commit: `f10f25607ecddcface2fed3768f76605a01f5174`
  ("Initial commit", contains `LICENSE` only).
- Governance baseline (M0-C): `c0a665985f54de33d813ea1bf62794e7170299be`
  (published in M0-J).
- First published Debian-generation head: `f79c7324b534a37f6ec6c6566cb6615425ad6bce`.
- First green CI commit: `ce2462e4e3a6b1016e5fe5d23e8dfd4d7d1361ba`.

## Intentionally undecided

These are pending ADR subjects (see [decisions.md](decisions.md)). They must
not be treated as decided:

- P-001 — Bootloader / boot architecture
- P-002 — Filesystem/subvolume layout (Btrfs preferred, pending validation)
- P-003 — Encryption architecture/default
- P-004 — Installer technology
- P-005 — Hyprland sourcing/version policy
- P-006 — Secure Boot ownership/key model
- P-007 — LUBARCHY repository infrastructure
- P-008 — Release cadence/support policy
- P-009 — Application delivery UX

## M0 baseline facts

- Source: `build/live` (live-build `1:20250505+deb13u1`, Debian trixie `main`
  with security and updates), deterministic `SOURCE_DATE_EPOCH` from the
  commit time, `config/rootfs/excludes` for the APT binary caches.
- Reproducibility: two independent builds of
  `2a2eda4b1c209be7dd5eb668e46402a181f308f6` produced byte-identical ISOs
  (SHA-256 `d697b42871477c5ac5e5055df3472986a55ca9f6216b5110d1cc2b11a2c33db3`)
  under identical, signature-verified Debian repository inputs.
- Boot: UEFI optical and UEFI virtual removable-media (OVMF USB
  mass-storage) boot qualified; serial smoke and hybrid structure gates pass.
- Static gates: repository lint, ShellCheck and the live-build configuration
  gate, locally and in GitHub Actions (static/configuration CI only; CI does
  not build an ISO).
- M0 boot choices (GRUB EFI plus `grub-pc` for the hybrid system area,
  Secure Boot disabled, no installer) are pipeline choices only and do not
  resolve P-001, P-004 or P-006.

## Not implemented / deferred

- Physical USB boot on real hardware (deferred to later hardware
  qualification)
- Historical reproducibility: the Debian mirrors move, so rebuilding later
  against changed suites is not guaranteed to reproduce an image
- ISO builds in CI (CI runs static/configuration gates only)
- LUBARCHY package repository
- Desktop stack
- Inventory persistence and user-facing diagnostics (M1-M adds a
  read-only validation result only)
- Device firmware mappings deferred by M1-E/M1-F: Realtek Ethernet,
  `btusb`-based Bluetooth (Realtek, MediaTek, Intel, Broadcom), `ath11k`/
  `ath12k`, NVIDIA, Intel sound, BCMA/SDIO/serial devices such as `brcmsmac`
- Activating `non-free-firmware` in the ISO/installed-system sources and
  applying plans (installation)
- NVIDIA driver strategy (evidence in M1-G, repository containment proof in M1-H; ADR-0002 Proposed, not decided)
- Hybrid/multi-GPU composition and USB device classification
- Installer
- Recovery implementation
- Update manager
- Secure Boot implementation
- Factory reset
- Real-hardware qualification

## Milestone order

1. M0 — Minimal reproducible ISO (PASS)
2. M1 — Hardware enablement foundation (IN PROGRESS)
3. M2 — Hyprland + Plasma desktop
4. M3 — Beginner installer
5. M4 — Recovery and rollback
6. M5 — Dual boot / advanced storage
7. M6 — Update system / release qualification
8. M7 — Real-hardware qualification

## Immediate next action

A CTO brief for M1-P, which has not been defined or started. M1-O is PASS
(CTO accepted) / CLOSED (implementation `4fc443d`, CI run #40; qualification
docs `03be7dc`, CI run #41). The `pre-m1o` qualification snapshot awaits a
separate post-closure cleanup. M1-N is
PASS (CTO
re-accepted) / CLOSED after the APT sandbox `partial/` correction (product
`dfd82cb`, harness fix `9e7ad19`, CI runs #37 and #38; original implementation
`1eec088956d73cdc53bf450a76d860cbe4ec7e41`, CI run #33). M1-M is PASS
(CTO accepted; implementation `59793b6dcdaddf73946dcdd9a6681ac58b2fc644`, CI run #30); M1-L is PASS (CTO accepted).

M1-N limitations that remain in force:
- GC covers only proven `eligible_complete` and `eligible_abandoned` payload
  state under the fixed LUBARCHY roots. It never deletes journals, receipts,
  unknown, corrupt, ambiguous or unresolved/manual-recovery state, the APT
  cache in general, dpkg state, installed packages or APT sources.
- Journal archival and reclaiming apply caches whose bundle is already gone
  remain future work.

M1-M limitations that remain in force:
- the current builder's own validation result is `FAIL`, because
  `intel-microcode` is absent from the restored baseline while the plan
  requires it;
- VM microcode is `LIMITED`, and a VM does not prove physical Wi-Fi,
  Bluetooth or GPU function;
- the production firmware requirement policy is empty: known-required
  firmware failures are `FAIL`, optional or unclassified ones `LIMITED`;
- NVIDIA remains `NOT_TESTED` (unqualified);
- real-hardware qualification remains future work.

The security findings stand:
- APT/dpkg mutation is not atomic, and the journals are evidence, not a
  rollback.
- Recovery never performs a generic repair, upgrade or removal.
- Validation is read-only; VM observations are not physical-hardware
  claims.

M1-O limitations that remain in force:
- the coordinator is an API only: no installer, service or diagnostics
  integration invokes it yet, and privilege supply remains future work;
- a VM qualifies the orchestration lifecycle, not physical microcode, radio
  or GPU function;
- orchestration history is retained indefinitely; a retention policy is
  future work.

Open work:
- installer/service integration that invokes the coordinator;
- Secure Boot/MOK;
- the NVIDIA ADR;
- hybrid/multi-GPU;
- user-facing diagnostics;
- real-hardware qualification.

M1-P has not been defined or started.

## PASS rule

A step or milestone is PASS only when every criterion is backed by recorded
evidence (commands, exit statuses, outputs) and reviewed by the CTO. Expected
to work is not PASS. Any unresolved security or authenticity failure is a
NO-GO.
