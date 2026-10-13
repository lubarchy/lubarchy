# NVIDIA driver strategy evidence (M1-G)

**Evidence only. Nothing here is used in production.** The production resolver
and production rules never read these files. No NVIDIA source, package,
driver or rule is authorised. The strategy is the subject of
[ADR-0002](../../docs/decisions/0002-nvidia-driver-strategy.md), which is
**Proposed** and awaits a CTO decision.

| File | Content |
| --- | --- |
| `evidence-v1.json` | `lubarchy.hardware.nvidia-evidence/v1`: Debian package and security state, NVIDIA repository signing and branch facts, GPU generation matrix, desktop and Secure Boot requirements, strategy scores, proposed tiers, isolated APT simulation results |
| `supported-gpus-v1.json` | `lubarchy.hardware.nvidia-gpu-snapshot/v1`: NVIDIA's `supported-gpus.json` (from `nvidia-driver-assistant` 0.62.71.09-1, signed NVIDIA Debian 13 repository), normalized, with provenance and hashes |
| `apt-policy-v1.json` | `lubarchy.hardware.nvidia-apt-policy/v1` (M1-H): **NOT ACTIVE** candidate containment policy for NVIDIA's Debian 13 repository: identity, full fingerprint, default deny, 30-binary closure, branch-615 lock, Debian-overlap deny, freshness rule, limitations |
| `apt-preferences.example` | The policy rendered as apt_preferences(5) text; NOT ACTIVE, not installed or referenced anywhere |
| `apt-fixtures-v1.json` | `lubarchy.hardware.nvidia-apt-fixtures/v1` (M1-H): compact fixtures from authenticated builder captures: Release identity, relevant NVIDIA and competing Debian package records, classification of every NVIDIA package name, overlap audit and recorded APT outcomes |

The snapshot is produced by `tools/hwe/normalize-nvidia-gpus.py normalize`
and checked by `tools/hwe/normalize-nvidia-gpus.py check`. Each upstream entry
keeps its PCI device ID, optional subsystem IDs, name, legacy branch, the
kernel-module support flags and the VDPAU feature set, and gets a derived
`support_class` (`legacy-older`, `legacy-470`, `legacy-580`,
`open-or-proprietary`, `open-only`). The upstream anomaly where legacy-580
chips are flagged `kernelopen` is recorded as published, not corrected.

`tests/nvidia-strategy.py` (static gate 8) validates both files using only
committed data: no network and no new dependency. Refreshing the evidence
means repeating the M1-G retrieval and simulations, regenerating, reviewing
and committing. Nothing is updated automatically.

The M1-H artifacts are produced and checked by
`tools/hwe/validate-nvidia-apt-policy.py` (`render`, `extract` on the
builder, `check`, `freshness`). No signing key is committed: live
requalification uses temporary, evidence-only key material whose full
fingerprint `02182E60104FCDC26EAE1B8597A5D4CB8793F200` is verified first. A
signed repository is not enough. The candidate containment model requires
the exact identity, a default deny, a finite allowlist, a branch lock,
Debian-infrastructure protection, update requalification, and rollback and
freshness controls. These controls govern APT candidate selection for
LUBARCHY-managed automatic transactions. APT preferences are not a sandbox
or MAC mechanism against root. An explicit exact-version administrator
request (`apt-get install pkg=version`) overrides candidate selection in APT
3.0.3. It is recorded as evidence and treated as a privileged override
outside the containment boundary.
