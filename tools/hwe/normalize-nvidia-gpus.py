#!/usr/bin/python3
"""LUBARCHY NVIDIA supported-GPU snapshot normalizer (M1-G; evidence only).

NVIDIA's supported-gpus.json (shipped in the nvidia-driver-assistant package
of NVIDIA's signed Debian 13 repository) -> deterministic normalization ->
reviewed, committed snapshot (hardware/nvidia/supported-gpus-v1.json).

Modes (nothing is written; output goes to stdout):

  normalize UPSTREAM-JSON PROVENANCE-JSON
      Print the normalized snapshot (lubarchy.hardware.nvidia-gpu-snapshot/v1).
  check SNAPSHOT
      Check that SNAPSHOT is in canonical form and self-consistent; exit 1
      otherwise.

The snapshot is architecture evidence for ADR-0002 (Proposed). The
production resolver and production rules never read it, and no rule may be
generated from it without a CTO decision. This tool reads files only; it
starts no process and uses no network.

Normalization keeps, per upstream chip entry: the PCI device ID, the optional
subsystem vendor/device IDs, the marketing name, NVIDIA's legacy branch
("580.xx" -> "580"), the kernel-module support flags (kernelopen,
gsp_proprietary_supported) and the VDPAU feature-set letter. Display feature
flags are dropped. A derived support_class is added (see SUPPORT_CLASSES).
The upstream flags are recorded as published, including known anomalies.
"""

import hashlib
import json
import re
import sys

SCHEMA_ID = "lubarchy.hardware.nvidia-gpu-snapshot/v1"
HEX4 = re.compile(r"^[0-9a-f]{4}$")
UPSTREAM_HEX = re.compile(r"^0x[0-9A-Fa-f]{4}$")
LEGACY = re.compile(r"^([0-9]+)\.[0-9x]+\.?(xx)?$")
SUPPORT_FLAGS = ("gsp_proprietary_supported", "kernelopen")
OLDER_LEGACY = ("71", "96", "173", "304", "340", "367", "390")

# Derived classes. Ordered; the first matching class wins. The generations
# named here are the usual NVIDIA architecture names for the class, checked
# against sample names in the M1-G evidence, not a per-chip claim.
SUPPORT_CLASSES = (
    ("legacy-older", "legacy branch 390 or older (Fermi and earlier; 367 is Kepler GRID)"),
    ("legacy-470", "legacy branch 470 (Kepler)"),
    ("legacy-580", "legacy branch 580 (Maxwell, Pascal, Volta)"),
    ("open-or-proprietary", "no legacy branch; kernelopen and gsp_proprietary_supported "
                            "(Turing, Ampere, Ada Lovelace, Hopper)"),
    ("open-only", "no legacy branch; kernelopen without gsp_proprietary_supported (Blackwell and newer)"),
)


class SnapshotError(Exception):
    pass


def support_class(chip):
    lb = chip["legacy_branch"]
    if lb is not None:
        if lb in OLDER_LEGACY:
            return "legacy-older"
        if lb in ("470", "580"):
            return "legacy-" + lb
        raise SnapshotError(f"unknown legacy branch {lb!r}")
    flags = set(chip["flags"])
    if "kernelopen" in flags and "gsp_proprietary_supported" in flags:
        return "open-or-proprietary"
    if "kernelopen" in flags:
        return "open-only"
    raise SnapshotError(f"chip {chip['pci_device']} has no legacy branch and no kernelopen flag")


def _hex(value, what):
    if not isinstance(value, str) or not UPSTREAM_HEX.match(value):
        raise SnapshotError(f"bad {what} {value!r}")
    return value[2:].lower()


def normalize_chip(entry):
    if not isinstance(entry, dict) or not {"devid", "name", "features"} <= set(entry):
        raise SnapshotError(f"bad upstream entry {entry!r}")
    unknown = set(entry) - {"devid", "subdevid", "subvendorid", "name", "features", "legacybranch"}
    if unknown:
        raise SnapshotError(f"unknown upstream keys {sorted(unknown)}")
    if ("subdevid" in entry) != ("subvendorid" in entry):
        raise SnapshotError(f"partial subsystem ID in {entry!r}")
    lb = entry.get("legacybranch")
    if lb is not None:
        m = LEGACY.match(lb)
        if not m:
            raise SnapshotError(f"bad legacy branch {lb!r}")
        lb = m.group(1)
    vdpau = None
    flags = []
    for feat in entry["features"]:
        low = feat.lower()
        if low.startswith("vdpaufeatureset"):
            vdpau = low[len("vdpaufeatureset"):].upper()
        elif low in SUPPORT_FLAGS:
            flags.append(low)
    chip = {
        "pci_device": _hex(entry["devid"], "devid"),
        "subsystem_vendor": _hex(entry["subvendorid"], "subvendorid") if "subvendorid" in entry else None,
        "subsystem_device": _hex(entry["subdevid"], "subdevid") if "subdevid" in entry else None,
        "name": entry["name"],
        "legacy_branch": lb,
        "flags": sorted(set(flags)),
        "vdpau_feature_set": vdpau,
    }
    chip["support_class"] = support_class(chip)
    return chip


def chip_key(chip):
    return (chip["pci_device"], chip["subsystem_vendor"] or "", chip["subsystem_device"] or "")


def canonical_chip(chip):
    return json.dumps(chip, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def chips_digest(chips):
    return hashlib.sha256("\n".join(canonical_chip(c) for c in chips).encode("utf-8")).hexdigest()


def counts(chips):
    out = {name: 0 for name, _ in SUPPORT_CLASSES}
    for c in chips:
        out[c["support_class"]] += 1
    return out


def anomalies(chips):
    legacy_open = sum(1 for c in chips if c["legacy_branch"] == "580" and "kernelopen" in c["flags"])
    return [{
        "id": "legacy-580-flagged-kernelopen",
        "count": legacy_open,
        "note": "Upstream flags these legacy-580 (Maxwell/Pascal/Volta) chips 'kernelopen', but NVIDIA's "
                "open-gpu-kernel-modules README states the open modules support Turing and later only. "
                "The legacy branch, not the flag, is authoritative for these chips.",
    }]


def build(upstream, provenance):
    if not isinstance(upstream, dict) or set(upstream) != {"chips"} or not isinstance(upstream["chips"], list):
        raise SnapshotError("upstream document must be {\"chips\": [...]}")
    chips = sorted((normalize_chip(e) for e in upstream["chips"]), key=chip_key)
    keys = [chip_key(c) for c in chips]
    if len(set(keys)) != len(keys):
        raise SnapshotError("duplicate chip keys after normalization")
    return {
        "schema": SCHEMA_ID,
        "production_use": False,
        "provenance": provenance,
        "support_classes": [{"id": n, "meaning": m} for n, m in SUPPORT_CLASSES],
        "counts": {"chips": len(chips), "by_support_class": counts(chips)},
        "upstream_anomalies": anomalies(chips),
        "chips_sha256": chips_digest(chips),
        "chips": chips,
    }


def serialize(doc):
    head = {k: v for k, v in doc.items() if k != "chips"}
    text = json.dumps(head, indent=2, sort_keys=True, ensure_ascii=False)
    body = ",\n".join("    " + canonical_chip(c) for c in doc["chips"])
    return text[:-2] + ',\n  "chips": [\n' + body + "\n  ]\n}\n"


def check(path):
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    doc = json.loads(raw)
    if doc.get("schema") != SCHEMA_ID or doc.get("production_use") is not False:
        raise SnapshotError("wrong schema or production_use")
    chips = doc["chips"]
    for c in chips:
        for k in ("pci_device",):
            if not HEX4.match(c[k]):
                raise SnapshotError(f"bad {k} {c[k]!r}")
        if c["support_class"] != support_class(c):
            raise SnapshotError(f"support_class drift for {c['pci_device']}")
    if [chip_key(c) for c in chips] != sorted({chip_key(c) for c in chips}):
        raise SnapshotError("chips not sorted or not unique")
    if doc["chips_sha256"] != chips_digest(chips):
        raise SnapshotError("chips_sha256 mismatch")
    if doc["counts"] != {"chips": len(chips), "by_support_class": counts(chips)}:
        raise SnapshotError("counts mismatch")
    if doc["upstream_anomalies"] != anomalies(chips):
        raise SnapshotError("anomaly summary mismatch")
    if serialize(doc) != raw:
        raise SnapshotError("snapshot is not in canonical form")
    return doc


def main(argv):
    if len(argv) == 4 and argv[1] == "normalize":
        with open(argv[2], encoding="utf-8") as f:
            upstream = json.load(f)
        with open(argv[3], encoding="utf-8") as f:
            provenance = json.load(f)
        sys.stdout.write(serialize(build(upstream, provenance)))
        return 0
    if len(argv) == 3 and argv[1] == "check":
        doc = check(argv[2])
        print(f"ok: {argv[2]}: {doc['counts']['chips']} chips, canonical, self-consistent")
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except (SnapshotError, OSError, ValueError, KeyError) as e:
        print(f"FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
