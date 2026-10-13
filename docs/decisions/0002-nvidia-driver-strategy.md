# ADR-0002 — NVIDIA driver strategy

Status: Proposed
Date: 2026-10-13

**Not authoritative.** Drafted by the executor in M1-G as architecture
evidence for CTO review. Nothing in this ADR is accepted: no NVIDIA source,
package, driver, kernel module or production rule is authorised, and the
decision register records no new accepted decision. Machine-readable
evidence: [hardware/nvidia/evidence-v1.json](../../hardware/nvidia/evidence-v1.json)
(see [hardware/nvidia/README.md](../../hardware/nvidia/README.md)).

## Context

D-008 makes hardware enablement first-class, and NVIDIA support is a
first-class requirement (ADR-0001). ADR-0001 authorised Debian
`non-free-firmware` only and explicitly left the NVIDIA driver strategy to a
separate review. Today the production rule `class.gpu.nvidia` classifies a PCI
display controller from vendor `10de` and has no package effect.

Any proprietary NVIDIA driver needs either Debian `contrib` + `non-free`
(not authorised), `trixie-backports` (D-005: "selective, justified") or a
third-party repository (D-005: needs a security review and a CTO decision).
The kernel modules are built with DKMS, which interacts with Secure Boot
(P-006), kernel updates and rollback (M4), and the desktop requirements
(Hyprland default D-006, Plasma fallback D-007).

All facts below were gathered on 2026-10-13 from official Debian and NVIDIA
sources and isolated, test-only APT simulations on the Debian 13 builder;
sources, hashes and simulation logs are recorded in the evidence file and the
M1-G evidence directory.

## Requirements and constraints

- Security over convenience: a driver path with known, unfixed security
  issues must not be an automatic default.
- APT/dpkg remains the only package authority (D-004); no `.run` installer,
  no FrankenDebian (no Debian 12/Sid/Ubuntu packages, no community DKMS
  packages, no mixed driver methods) (D-005).
- Every source must be signed and verified fail-closed.
- Updates must be coordinated with Debian kernel updates; a kernel update must
  never leave the user without a working display.
- Secure Boot (P-006) is not resolved; disabling Secure Boot is not an
  acceptable workaround.
- Wayland is the primary display stack: Hyprland default, Plasma fallback.
- Hybrid/multi-GPU composition is out of scope for this ADR.

## Options considered

### Strategy A — Debian Stable `nvidia-graphics-drivers` 550 (`contrib`/`non-free`)

- Trixie ships `nvidia-graphics-drivers` **550.163.01-2** (source in
  `non-free-firmware`, binaries in `non-free`; open kernel modules from
  `nvidia-open-gpu-kernel-modules` in `contrib`).
- Debian security tracker: **93 open issues** for trixie, every one
  "vulnerable (no DSA)" (3 from 2024, 9 from 2025, 81 from 2026, up to
  CVE-2026-47604). Grave security bug
  [#1149642](https://bugs.debian.org/1149642) (filed 2026-10-01) is open.
- The open flavor no longer builds on the current trixie kernel (≥ 6.12.100,
  [#1143204](https://bugs.debian.org/1143204), open). The builder runs
  6.12.111.
- No Blackwell support. Debian's wiki: "This version is no longer maintained
  and contains several security issues. It should be used only in properly
  isolated, trusted environments."
- Below 555: no explicit sync, so XWayland applications flicker under KWin
  and Hyprland (KDE: "XWayland apps are usable with 555 and newer"; Hyprland
  wiki: at least 555).
- Requires enabling `contrib` and `non-free`.

### Strategy B — Debian `trixie-backports`

- Same upstream 550.163.01 (`-4~bpo13+1`), so the same open CVEs.
- Built for Backports kernels; Debian's wiki records that it no longer
  compiles on the 6.19 kernels in `trixie-backports`.
- Couples the driver to a Backports kernel, a different support model.
  Requires `contrib`, `non-free` and Backports.

### Strategy C — NVIDIA's official Debian 13 repository

`https://developer.download.nvidia.com/compute/cuda/repos/debian13/x86_64/`

- Flat repository signed with RSA-4096 key
  `02182E60104FCDC26EAE1B8597A5D4CB8793F200` ("Kitmaker (Debian 13 Trixie)",
  created 2025-08-15, no expiry). Observed distribution channels during
  qualification (2026-10-13):
  - `cuda-keyring` 1.1-1 (the guide's primary method), fetched over HTTPS;
    its description gives only the short ID `8793F200`.
  - The documented fallback URL `…/debian13/x86_64/cuda-archive-keyring.gpg`
    returned HTTP 404.
  - The repository index also exposes `8793F200.pub` (SHA-256
    `09edb5196bca272c091633d4e04dd6c8f3a07278834868220aa669c895f6cfd7`),
    whose full fingerprint matches the signing key.

  Neither fact changes the trust decision. Both channels are served from the
  same origin over HTTPS, so neither is an independent trust anchor. Any
  future repository bootstrap must verify the expected full fingerprint
  `02182E60104FCDC26EAE1B8597A5D4CB8793F200` through an explicitly qualified
  mechanism.
- InRelease verifies (`gpgv`: good signature). The Release file has **no
  `Suite`, `Codename` or `Valid-Until`**, so APT cannot detect a replayed or
  frozen index (freeze/rollback attacks) beyond the signature itself.
- Driver branches in the index: 590, 595, 610, 615 (no 580). 554 packages in
  total, most of them CUDA; installing only the driver needs an explicit
  allowlist.
- NVIDIA's guide: "The proprietary kernel modules have been removed. The open
  kernel modules are now the only kernel module flavor provided." In 615,
  `nvidia-kernel-dkms` is a transitional dummy that depends on
  `nvidia-kernel-open-dkms`, so upgrading a proprietary 610 install silently
  switches flavor.
- Branch-agnostic package names since 590; branch locking through
  `nvidia-driver-pinning-<branch>` packages that drop an APT preferences file
  (`Pin-Priority: 1000`).
- Desktop-only install: `nvidia-driver nvidia-kernel-open-dkms` (no CUDA).
- Package names collide with Debian's (`nvidia-driver`, `firmware-nvidia-gsp`,
  `nvidia-kernel-support`, `libnvidia-egl-wayland1`, …) at higher versions, so
  enabling the repository takes these packages over from Debian.
- Unverified secondary sources list 610.57.04 and 615.71.09 as fixing the
  2026 CVEs. NVIDIA's security bulletin was not retrieved, so this is **not
  verified**.
- The repository also carries non-NVIDIA packages that override Debian's
  (`dkms` 3.4.3 over Debian's 3.2.2, `dh-dkms`, `eglexternalplatform-dev`,
  `libnccl2`, …). Of 554 package names, 64 also exist in Debian, and 62 of
  those win on version.
- NVIDIA 615's `nvidia-kernel-support` ships `/etc/modprobe.d/nvidia.conf`,
  which blacklists `nouveau` and `nova-core`. That removes the Nouveau
  fallback for as long as the package stays installed.

### Isolated APT simulation results

Each profile used its own APT state and configuration on the builder
(`apt-get -s` only, unprivileged, the system `/etc/apt` and dpkg database
untouched). All indices verified with no warnings; every inspected `.deb`
matched its index hash and none were kept. Upgrade, downgrade and removal
paths use synthetic "installed" states built from authenticated index
metadata.

| Simulation | Result |
| --- | --- |
| A: `nvidia-driver nvidia-kernel-dkms` | 224 new: 179 `main`, 11 `contrib`, 33 `non-free`, 1 `non-free-firmware` |
| A: open flavor | 224 new; resolves, but does not build on the current kernel (#1143204) |
| A: purge with autoremove | 167 packages removed |
| A + i386 libraries | 298 new, 74 of them i386 |
| B: `-t trixie-backports` | 226 new, 47 from Backports (550.163.01-4~bpo13+1) |
| C: desktop-only `nvidia-driver nvidia-kernel-open-dkms` | 143 new: 112 Debian `main`, 31 NVIDIA; `contrib` not needed; `dkms` comes from NVIDIA |
| C: `nvidia-open` (with CUDA) | 227 new: 175 Debian `main`, 52 NVIDIA |
| C: proprietary 610 by explicit versions | fails (dependencies resolve to 615) |
| C: proprietary 610 with `nvidia-driver-pinning-610` | 143 new |
| C: unpin 610 proprietary, upgrade | 26 upgraded, `nvidia-kernel-open-dkms` newly installed: **silent switch to the open flavor** |
| C: 615 → `nvidia-driver-pinning-595` | 25 downgraded, `nvidia-powerd` removed |
| C: purge with autoremove | 92 packages removed |
| Debian 550 installed, then NVIDIA repo added | 33 upgraded, 9 new, 10 removed: every NVIDIA package **and Debian's `dkms`** taken over; proprietary 550 becomes open 615 |

No install pulls `linux-headers`, so DKMS would build nothing until
`linux-headers-amd64` is installed explicitly. Every strategy therefore needs
explicit header management.

### Strategy D — Nouveau (in-kernel, Mesa)

- Part of the Debian kernel and Mesa in `main`, under Debian security support.
  No new source.
- Turing and later can use NVIDIA's GSP firmware for Nouveau, which Debian
  ships in `firmware-nvidia-graphics` (`non-free-firmware`, already
  authorised by ADR-0001; not mapped yet).
- Limited performance and power management on Maxwell, Pascal and Volta (no
  reclocking). It is the only candidate path for Kepler and older.
- **Not qualified.** Trixie carries kernel 6.12 and Mesa 25.0.7 (NVK).
  Upstream Nouveau/NVK support milestones newer than that baseline mean full
  desktop qualification cannot be claimed for every generation, especially
  Blackwell. Nouveau is a **baseline/fallback candidate**, not a qualified
  default. No NVIDIA generation is production-qualified without real-hardware
  evidence (M7).

## GPU generation matrix

Support classes are derived from NVIDIA's `supported-gpus.json` (normalized
snapshot `hardware/nvidia/supported-gpus-v1.json`, 2,322 entries, not used in
production).

Nouveau status vocabulary: **fallback candidate**: expected to provide a
basic desktop on the trixie baseline, not qualified; **limited**: known
functional limits (no reclocking, legacy hardware); **not yet qualified**:
support on the trixie baseline is not established. Every row is **not
production-qualified**: none has real-hardware evidence.

| Generation | Snapshot class (entries) | Nouveau on trixie (6.12, Mesa 25.0.7) | Debian 550 (A/B) | Open modules | Proprietary modules | GSP | NVIDIA Debian 13 repo (C) | Proposed path (Proposed) | Fallback | Production-qualified |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Fermi and older | legacy-older (1,033) | limited | no | no | legacy ≤390 only | no | no | Nouveau (limited) | framebuffer | no |
| Kepler | legacy-470 (151) | limited | no | no | 470 legacy only | no | no | Nouveau (limited) | framebuffer | no |
| Maxwell, Pascal, Volta | legacy-580 (305) | limited (no reclocking) | yes (proprietary flavor only) | no | 580 legacy branch | no | **no** (580 not in the repo) | Nouveau (limited) | framebuffer | no |
| Turing, Ampere, Ada, Hopper | open-or-proprietary (743) | fallback candidate (GSP) | proprietary only (open FTBFS on current kernel) | yes (recommended by NVIDIA) | ≤610 | yes | yes, open flavor | Candidate C (opt-in, Proposed) | Nouveau candidate | no |
| Blackwell and newer | open-only (90) | **not yet qualified** | **no** | required | no | required | yes, open flavor | Candidate C (opt-in, Proposed) | framebuffer; Nouveau not yet qualified | no |

Upstream anomaly: the 305 legacy-580 entries are flagged `kernelopen` in
`supported-gpus.json`, although NVIDIA's open-module README limits the open
modules to Turing and later. The legacy branch is treated as authoritative.

## Firmware relationship

- `firmware-nvidia-gsp`: GSP firmware for the NVIDIA driver, built from
  `nvidia-graphics-drivers` and versioned in lockstep with the driver (exact
  `=` dependency in both Debian and NVIDIA packaging). Under strategy C it
  comes from NVIDIA's repository and replaces Debian's.
- `firmware-nvidia-graphics`: firmware for Nouveau, from Debian
  `non-free-firmware`. It is relevant only to strategy D and would need its
  own M1-E style qualification before any mapping.
- Neither package is mapped in production; the M1-E/M1-F firmware gate keeps
  both inactive.

## Desktop requirements (Wayland)

- Both Hyprland and KWin need `nvidia_drm modeset=1` (and `fbdev=1`, enabled by
  default from 570.86.16), the NVIDIA EGL Wayland/GBM libraries, and driver
  ≥ 555 with XWayland ≥ 24.1 for explicit sync. Debian 550 does not meet
  this.
- Plasma fallback: KWin supports NVIDIA from 495.44 and implements explicit
  sync from 6.1. On Nouveau, Plasma and Hyprland would run on Mesa. This is
  not qualified for any NVIDIA generation (see the generation matrix).
- Trixie provides Xwayland 24.1.6 and KWin 6.3.6, which meet the
  proprietary-driver desktop requirements. Only the driver version is the
  blocker there. Trixie's Mesa 25.0.7 (NVK) predates newer upstream
  Nouveau/NVK support milestones, so Nouveau desktop support is a candidate
  to qualify, not a given.
- Hyprland is not in trixie (it is only in `trixie-backports`); its sourcing
  is P-005 and is not decided here.

## Secure Boot dependency

DKMS-built modules must be signed with a Machine Owner Key enrolled through
`mokutil`/shim, or the kernel refuses to load them under Secure Boot. That is
an enrollment step at install time and a key LUBARCHY would have to own or
generate per machine, which is exactly P-006. **No NVIDIA proprietary-driver
strategy can be made automatic until P-006 is decided.** Nouveau has no such
dependency (signed in-kernel modules).

## Kernel update coordination

DKMS rebuilds the module for every new kernel and needs the matching
`linux-headers` and a compiler. A build failure leaves the new kernel without
a working NVIDIA module (as happened to Debian 550-open with 6.12.100). The
recommended controls are proposed for later milestones: a kernel/driver
compatibility gate before an update is offered, keeping the previous kernel
bootable, and a Nouveau fallback candidate (subject to the generation
matrix and real-hardware qualification).

## Rollback requirements

- Keep a known-good previous kernel and driver pair bootable.
- Pin the driver branch (strategy C pinning package) and allowlist packages so
  that no CUDA or unrelated package is pulled in.
- Before any driver switch, purge the previous method (never mix methods).
- Downgrades through pinning are possible with APT (`Pin-Priority ≥ 1000`) but
  rebuild DKMS and need a reboot. The isolated simulations record this.
- Last resort: boot with the NVIDIA modules blacklisted and fall back to
  Nouveau where the generation matrix allows it, otherwise to a basic
  framebuffer.

## Hybrid and multi-GPU boundary

Out of scope (no `gpu.hybrid` classification). Inventory v1 records PCI
devices only. A later inventory-v2 proposal may add `boot_vga`, DRM card and
render nodes, connector ownership and PCI power state. This ADR only
recommends that proposal and does not change any schema.

## Security and maintenance scoring

Scores run from 0 (bad) to 3 (good), from the evidence above.

| Criterion | A Debian 550 | B Backports 550 | C NVIDIA repo | D Nouveau |
| --- | --- | --- | --- | --- |
| Known open security issues | 0 (93 open, no DSA) | 0 (same upstream) | 2 (current branches; fixes unverified) | 3 (Debian kernel/Mesa) |
| Security update channel | 0 | 0 | 2 (vendor, no Debian review) | 3 |
| Source provenance / signing | 3 (Debian) | 3 (Debian) | 2 (signed; no Valid-Until; key channels are same-origin HTTPS, fallback URL 404) | 3 |
| Builds on current trixie kernel | 2 (proprietary only) | 0 (needs bpo kernel; FTBFS 6.19) | 2 (to be confirmed by DKMS qualification) | 3 |
| GPU coverage | 1 (no Blackwell) | 1 | 2 (Turing+ only) | 1 (not qualified; limited on legacy; Blackwell not yet qualified) |
| Wayland/explicit sync | 0 (<555) | 0 | 3 | 2 |
| FrankenDebian / takeover risk | 3 | 2 | 1 (replaces Debian package names) | 3 |
| Secure Boot independence | 0 | 0 | 0 | 3 |

## Proposed tier model (for CTO decision)

- **Tier 0 — baseline/fallback candidate:** Nouveau (Strategy D) with no new
  source. This is a candidate, **not a qualified default**: limited on
  legacy generations, not yet qualified for Blackwell, and not
  production-qualified for any generation until real-hardware evidence
  exists (M7).
- **Tier 1 candidate — Turing and later (open-or-proprietary, open-only
  classes):** Strategy C, open flavor, desktop-only package set, branch-pinned
  and allowlisted, as **explicit user opt-in**, only after: (1) a D-005
  external-repository exception accepted by the CTO, (2) P-006 decided, (3) a
  kernel-update compatibility gate and rollback path (M4), and (4)
  real-hardware qualification (M7).
- **Maxwell, Pascal, Volta, Kepler and older:** only Nouveau (limited)
  remains as a candidate. No maintained, D-005-compliant proprietary path
  exists on Debian 13.
- **Strategies A and B: not recommended** as an automatic default or as a
  supported option, because of the unfixed security issues.

## D-005 external-repository exception (only if Strategy C is accepted)

If the CTO accepts Strategy C, the exception should be bounded:

- Origin `developer.download.nvidia.com/compute/cuda/repos/debian13/x86_64/`
  only, with `Signed-By` set to a LUBARCHY-shipped copy of key
  `02182E60104FCDC26EAE1B8597A5D4CB8793F200`. That copy is obtained and
  checked against the full fingerprint through an explicitly qualified
  bootstrap mechanism. Neither `cuda-keyring` nor `8793F200.pub` is a trust
  anchor by itself. Never `trusted=yes`.
- APT preferences: priority −1 for every package from that origin except an
  explicit driver allowlist, pinned to one branch. Debian's `dkms` and every
  other non-NVIDIA package must stay from Debian.
- A LUBARCHY-owned modprobe policy so that Nouveau can be restored as a
  fallback despite NVIDIA's blacklist.
- A compensating freshness control for the missing `Valid-Until`.
- A key rotation and revocation procedure.
- No `contrib`/`non-free` is needed: NVIDIA's guide enables `contrib`, but the
  desktop-only simulation drew all its Debian packages from `main`.

## Decision

**None yet (Proposed).** The executor recommends the tier model above for CTO
review. Until the CTO decides: no NVIDIA package is mapped, no source is
added, the production source policy stays `main` + `non-free-firmware`, and
`class.gpu.nvidia` stays classification-only.

## M1-H repository containment proof (evidence, not a decision)

M1-H tested whether Strategy C's repository could be used without taking over
Debian packages. The repository stays **unauthorised**. This section records
evidence for the CTO and does not change the Decision above.

**Identity, re-verified live on 2026-10-13.** Origin `NVIDIA`, Label
`NVIDIA CUDA`, signing key `02182E60104FCDC26EAE1B8597A5D4CB8793F200` (checked
with gpg on the host, with a standalone fingerprint calculator and with
`sqv` on the builder). Release Date `Tue, 13 Oct 2026 15:21:52 +0000` (later
than M1-G's). `Suite`, `Codename` and `Valid-Until` are absent;
`Acquire-By-Hash: no`. The `cuda-archive-keyring.gpg` fallback URL still
returned 404, and the index still exposes `8793F200.pub` with the same full
fingerprint. Key material was used only as temporary evidence; none is
committed. Branch 615 is unchanged since M1-G: the republish added 27 CUDA,
nvcomp and GDS packages only.

**Candidate policy (inactive).** `hardware/nvidia/apt-policy-v1.json`,
rendered to `hardware/nvidia/apt-preferences.example`, in record order:

1. *Rule D:* the 39 NVIDIA package names that also exist in Debian and are
   outside the closure (`dkms`, `dh-dkms`, `libnvidia-egl-wayland1`,
   `libcuda1`, …), with `Pin: release o=NVIDIA,l=NVIDIA CUDA`, priority −1.
2. *Rules B+C:* 26 driver-versioned binaries, `Pin: version 615.*`,
   priority 500.
3. *Rules B+C:* 4 NVIDIA EGL platform libraries that carry their own versions
   (`libnvidia-egl-gbm1` 1.1.4-2, `libnvidia-egl-wayland21` 1.0.2-1,
   `libnvidia-egl-xcb1`/`libnvidia-egl-xlib1` 1:1.0.6-1), each pinned to its
   exact version, priority 500.
4. *Rule A:* `Package: *`, `Pin: release o=NVIDIA,l=NVIDIA CUDA`, priority −1.

Semantics verified on APT 3.0.3:
- The first matching specific record wins: a Rule D deny placed first beats a
  later allowlist entry for the same package.
- The general record applies only when no specific record matches.
- A negative priority removes a version from candidacy.

**Allowlisting granularity.** The policy uses an exact binary allowlist.
Source-package allowlisting was rejected: `src:nvidia-graphics-drivers` alone
also admits 45 further binaries, including `cuda-drivers`, `libcuda1`,
`cuda-compat-*`, `nvlink5` and every `nvidia-driver-pinning-*` package.

**Results (isolated `apt-get -s`, product sources `main` + `non-free-firmware`):**

| Model | Result |
| --- | --- |
| S0 Debian only | `nvidia-driver` unavailable |
| S1 uncontained | 558 NVIDIA names eligible; desktop/open install takes 31 NVIDIA packages including NVIDIA's `dkms` 3.4.3-1 |
| S2 default deny | 0 NVIDIA names eligible; driver install impossible |
| S3 contained | 30 NVIDIA names eligible (exactly the closure); desktop/open install solvable (143 packages, 30 NVIDIA); `dkms` 3.2.2-1~deb13u1 from Debian |
| S4 contained upgrade | upgrade, dist-upgrade and full-upgrade change nothing; the in-branch update 615.71.09-1 → -2 upgrades 26 packages |
| S5 source disabled | installed versions stay; `apt-get check` clean; no Debian replacement (Debian's `firmware-nvidia-gsp` 550 is older and not forced); reinstall impossible and no further updates |
| S6 rollback | pin variants with priority 1001 downgrade exactly 26 driver-versioned packages to 615.71.09-1 or to 610.57.04-1; the 4 EGL libraries stay |
| Negative tests | 24 denied names (CUDA toolkit and metapackages, `libcuda1`, `nvidia-smi`, fabric manager, NSCQ, IMEX, Nsight, CUPTI, profiler API, DCGM, GDS, `cuda-keyring`, `nvidia-driver-pinning-615`, assistant, settings, xconfig, persistenced, `nvidia-kernel-dkms`, `libnccl2`, `nvidia-fs-dkms`, `dh-dkms`) install nothing from NVIDIA |
| Branch escape | explicit 610, 595 and 590 driver requests are unsolvable; `-t o=NVIDIA dkms` still selects Debian |
| Takeover audit | 64 NVIDIA names overlap Debian (all components); uncontained, NVIDIA wins all 64; contained, the 39 outside the closure resolve to Debian and only the 25 closure members resolve to NVIDIA; compilers, headers, initramfs-tools, systemd, udev, Mesa, Xorg, XWayland, Vulkan loader and GL/EGL/glvnd all stay Debian |

**Vendor pinning package.** `nvidia-driver-pinning-615` (615-1) installs
`Pin-Priority: 1000` records for `src:` patterns, including `cuda-drivers*`,
fabric manager, NSCQ, NVSDM and IMEX. It has no default deny: with it alone,
all 558 names stay eligible, NVIDIA's `dkms` wins and `cuda-toolkit` installs.
LUBARCHY must not rely on it for containment.

**Observed APT 3.0.3 behaviour (evidence, kept).** NVIDIA-denied versions
have a negative candidate priority (for example `dkms` 3.4.3-1 at −1).
Normal `install`, `upgrade`, `dist-upgrade` and `full-upgrade` resolution
never selects them, and dependencies are resolved only from candidates, so
`cuda-toolkit=13.4.3-1` stays unsolvable. An explicit exact-version
administrator request does override candidate selection for the named
package: `apt-get install dkms=3.4.3-1` and
`nvidia-fabricmanager=615.71.09-1` both select NVIDIA's packages. This
happens even though `apt_preferences(5)` says "P < 0 prevents the version
from being installed". The probe results stay in the committed fixtures.

**Containment boundary.** APT preferences are the candidate-selection policy
for normal APT resolution. They are not a sandbox or MAC mechanism against
root. A root administrator who can run arbitrary `apt install pkg=version`
commands can also edit sources and preferences directly. An explicit
exact-version request is therefore an intentional privileged override,
outside the repository-containment threat boundary. It does not weaken the
containment of LUBARCHY-managed automatic transactions. The LUBARCHY
security boundary is:

untrusted or user request → LUBARCHY resolver and approved plan →
privileged bounded package executor → APT.

**Future requirement (later M1 apply/install step; not implemented in
M1-H): the LUBARCHY privileged package executor.**
- It accepts only a validated, source-controlled plan.
- Package names come from that plan, never from caller-controlled text.
- Caller input cannot inject package versions, source or release selectors,
  or arbitrary APT options.
- It rejects direct arbitrary package or version requests.
- It drives APT non-interactively through a bounded API or command
  construction.
- It revalidates the plan immediately before mutating the system.
- It records the transaction and the validation result.

**Freshness.** A monotonic signed Release Date rule is designed and tested:
persist the newest accepted Date and InRelease hash, and reject a Date
regression or a same-Date content change. With only two observations (M1-G
and M1-H), no maximum age can be justified. Absolute freshness against a
frozen repository therefore needs a future explicit policy. Without
`Acquire-By-Hash`, a fetch can race a repository update. APT then fails
closed with a hash mismatch. Content substitution is still detected through
the signed SHA-256 chain InRelease → Packages → `.deb`.

**Key bootstrap (proposal only).** LUBARCHY would carry the key in its own
signed packaging (P-007), after checking the full fingerprint against the
source-controlled value through an independently qualified channel. Neither
`cuda-keyring` nor `8793F200.pub` is a trust anchor. Never `trusted=yes`.

**Containment assessment: YES — NVIDIA's Debian 13 repository can be
technically contained as a candidate D-005 exception.** For LUBARCHY-managed
automatic transactions, four controls contain every normal APT resolution
path:
- a default deny for the origin;
- a finite 30-binary allowlist;
- a branch-615 lock;
- Debian-infrastructure protection.

`dkms` from NVIDIA is not an APT candidate and cannot be selected by normal
LUBARCHY-managed dependency resolution. Before any activation, a qualified
key bootstrap and an absolute-freshness policy are still required. These
are activation prerequisites, not reasons to reject technical containment.
P-006 (Secure Boot) independently blocks automatic use. None of this
authorises the repository or accepts the strategy: ADR-0002 remains
Proposed.

## Security consequences

If the proposal is accepted: the Nouveau baseline candidate stays within
Debian's security support. The opt-in Strategy C adds a vendor repository and
kernel modules outside Debian's security process, mitigated by the allowlist,
branch pinning, the dedicated key and the freshness control. Strategy A/B
would knowingly ship 93 unfixed vulnerabilities and is not recommended.

## Operational consequences

Kernel/driver coordination, DKMS build tooling (compiler and headers) on
opted-in systems, MOK enrollment (P-006), a larger update surface (~75 MB of
GSP firmware per driver update), and support boundaries per GPU class.

## Validation

`tests/nvidia-strategy.py` (static gate 8) checks that the committed evidence
is consistent, that this ADR stays Proposed, that no production rule or
source policy references NVIDIA drivers, and that Strategy A is not
recommended while open security issues are recorded. Live evidence comes from
the isolated APT simulations recorded in the M1-G evidence directory.

## Rollback / reversibility

The ADR is a proposal; it changes nothing. If later accepted and reversed:
remove the repository, key and pinning files, purge the driver, and fall back
to the Nouveau candidate or a basic framebuffer.

## Consequences

The CTO decides the strategy, the D-005 exception and its ordering relative
to P-006 and M4/M7. M1-H must not start until a brief arrives.
