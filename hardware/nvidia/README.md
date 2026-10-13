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
