#!/usr/bin/python3
"""LUBARCHY device firmware mapping qualification (M1-E).

qualified Debian kernel module aliases -> deterministic tooling ->
reviewed, committed production rules (hardware/rules/package.firmware.*.json).

Modes (nothing is written unless 'write-rules' is given explicitly):

  extract --modules-alias PATH --kernel-release R --kernel-package P
          --kernel-package-version V
      Print normalized module-alias evidence (JSON) for the configured
      modules to stdout.
  check-evidence --modules-alias PATH --kernel-release R --kernel-package P
          --kernel-package-version V
      Compare a fresh extraction with the committed evidence; exit 1 on drift.
  check-rules
      Generate rules from the committed evidence and configuration, run the
      cross-package overlap analysis, and compare with the committed rules;
      exit 1 on drift or overlap. This is what the static gate runs.
  write-rules
      Explicit generation mode: write the generated rule files.

The production resolver never reads modules.alias; mappings change only by
requalifying, regenerating, reviewing and committing.
"""

import hashlib
import json
import os
import re
import sys

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(TOOL_DIR))
CONFIG_PATH = os.path.join(TOOL_DIR, "firmware-mappings.json")
EVIDENCE_PATH = os.path.join(TOOL_DIR, "module-alias-evidence.json")
RULES_DIR = os.path.join(REPO_ROOT, "hardware", "rules")

RULE_SCHEMA_ID = "lubarchy.hardware.rule/v1"
FIRMWARE_SOURCE = "non-free-firmware"
MODULE_NAME = re.compile(r"[a-z0-9_]{1,64}")  # configured (mapped/excluded) modules
ALIAS_FILE_MODULE = re.compile(r"[A-Za-z0-9_]{1,64}")  # any module in modules.alias (e.g. BusLogic)
# Field layout of the kernel's PCI and USB modalias strings; each field value
# is exact uppercase hex, '*' or a hex prefix followed by '*'.
FIELDS = {
    "pci": ("v", "d", "sv", "sd", "bc", "sc", "i"),
    "usb": ("v", "p", "d", "dc", "dsc", "dp", "ic", "isc", "ip", "in"),
}
FIELD_VALUE = re.compile(r"[0-9A-F]*\*|[0-9A-F]+")


class QualificationError(Exception):
    pass


def _strict_json(path):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise QualificationError(f"{path}: duplicate key {key!r}")
            out[key] = value
        return out
    with open(path, "rb") as f:
        return json.loads(f.read().decode("utf-8"), object_pairs_hook=pairs)


def load_config(path=CONFIG_PATH):
    config = _strict_json(path)
    if config.get("representable_buses") != ["pci", "usb"]:
        raise QualificationError("representable buses must be exactly pci and usb (inventory v1)")
    seen = set()
    for family in config["families"]:
        for module in family["modules"]:
            if not MODULE_NAME.fullmatch(module) or module in seen:
                raise QualificationError(f"bad or repeated module {module!r}")
            seen.add(module)
    if seen & set(config.get("excluded_modules", {})):
        raise QualificationError("a module is both mapped and excluded")
    return config


def parse_alias(alias):
    """(bus, field values) for a representable PCI/USB alias, else None."""
    bus, sep, rest = alias.partition(":")
    if not sep or bus not in FIELDS:
        return None
    keys = FIELDS[bus]
    pattern = "".join(f"{k}(?P<f{n}>[0-9A-F*]*?)" for n, k in enumerate(keys))
    m = re.fullmatch(pattern, rest)
    if not m:
        return None
    values = tuple(m.group(f"f{n}") for n in range(len(keys)))
    if not all(FIELD_VALUE.fullmatch(v) for v in values):
        return None
    return bus, values


def _field_overlap(x, y):
    xp, yp = x.endswith("*"), y.endswith("*")
    xs, ys = x.rstrip("*"), y.rstrip("*")
    if xp and yp:
        return xs.startswith(ys) or ys.startswith(xs)
    if xp:
        return y.startswith(xs)
    if yp:
        return x.startswith(ys)
    return x == y


def aliases_overlap(a, b):
    """True if some concrete modalias string can match both aliases."""
    pa, pb = parse_alias(a), parse_alias(b)
    if pa is None or pb is None:
        raise QualificationError("overlap analysis needs representable aliases")
    if pa[0] != pb[0]:
        return False
    return all(_field_overlap(x, y) for x, y in zip(pa[1], pb[1]))


def read_module_aliases(path):
    """module -> set of aliases from a modules.alias file. Malformed lines
    are rejected, not skipped."""
    out = {}
    with open(path, "rb") as f:
        text = f.read().decode("ascii")
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split(" ")
        if len(parts) != 3 or parts[0] != "alias" or not parts[1] or not ALIAS_FILE_MODULE.fullmatch(parts[2]):
            raise QualificationError(f"malformed modules.alias line {number}")
        out.setdefault(parts[2], set()).add(parts[1])
    return out


def _digest(aliases):
    return "sha256:" + hashlib.sha256(("\n".join(aliases) + "\n").encode("ascii")).hexdigest()


def extract(config, modules_alias, kernel_release, kernel_package, kernel_package_version):
    """Normalized evidence: per configured module, the representable PCI/USB
    aliases (bytewise sorted, unique, case preserved) and the excluded ones."""
    table = read_module_aliases(modules_alias)
    modules, excluded = {}, {}
    wanted = [m for f in config["families"] for m in f["modules"]] + sorted(config.get("excluded_modules", {}))
    for module in sorted(wanted):
        if module not in table:
            raise QualificationError(f"module {module} has no aliases in {modules_alias}")
        keep = sorted(a for a in table[module] if parse_alias(a) is not None)
        drop = sorted(a for a in table[module] if parse_alias(a) is None)
        for a in drop:
            if a.split(":", 1)[0] in config["representable_buses"]:
                raise QualificationError(f"{module}: alias on a representable bus has an unexpected shape")
        if module in config.get("excluded_modules", {}):
            keep, drop = [], sorted(table[module])
        modules[module] = {"aliases": keep, "count": len(keep), "digest": _digest(keep)}
        if drop:
            excluded[module] = drop
    return {
        "description": "Normalized PCI/USB kernel module aliases for the LUBARCHY firmware mapping families. Generated by tools/hwe/qualify-firmware-mappings.py extract; qualification provenance only.",
        "kernel_release": kernel_release,
        "kernel_package": kernel_package,
        "kernel_package_version": kernel_package_version,
        "source": f"/lib/modules/{kernel_release}/modules.alias",
        "modules": modules,
        "excluded": excluded,
    }


def dump(value):
    return json.dumps(value, indent=2, sort_keys=False, ensure_ascii=True) + "\n"


def check_evidence_integrity(evidence, config):
    """Committed evidence must be normalized and self-consistent."""
    expected = sorted([m for f in config["families"] for m in f["modules"]] + list(config.get("excluded_modules", {})))
    if sorted(evidence["modules"]) != expected:
        raise QualificationError("evidence modules differ from the configuration")
    for module, entry in evidence["modules"].items():
        aliases = entry["aliases"]
        if aliases != sorted(set(aliases)):
            raise QualificationError(f"{module}: aliases not sorted/unique")
        if entry["count"] != len(aliases) or entry["digest"] != _digest(aliases):
            raise QualificationError(f"{module}: count or digest mismatch")
        for a in aliases:
            if parse_alias(a) is None:
                raise QualificationError(f"{module}: unrepresentable alias in evidence")
        if module in config.get("excluded_modules", {}) and aliases:
            raise QualificationError(f"{module}: excluded module has active aliases")


def overlap_report(families):
    """Cross-package overlaps between active alias sets (must be empty)."""
    found = []
    for i, (pkg_a, aliases_a) in enumerate(families):
        for pkg_b, aliases_b in families[i + 1:]:
            if pkg_a == pkg_b:
                continue
            for a in aliases_a:
                for b in aliases_b:
                    if aliases_overlap(a, b):
                        found.append((pkg_a, a, pkg_b, b))
    return found


def generate_rules(config, evidence):
    rules = {}
    families = []
    for family in config["families"]:
        aliases = sorted({a for m in family["modules"] for a in evidence["modules"][m]["aliases"]})
        families.append((family["package"], aliases))
        match = []
        for bus in config["representable_buses"]:
            bus_aliases = [a for a in aliases if a.startswith(bus + ":")]
            if bus_aliases:
                match.append({bus: {"modalias": bus_aliases}})
        effects = {}
        if family["classifications"]:
            effects["classifications"] = list(family["classifications"])
        effects["packages"] = [{"name": family["package"], "disposition": "require",
                                "source": FIRMWARE_SOURCE, "reason": family["reason"]}]
        rule = {"schema": RULE_SCHEMA_ID, "id": family["rule_id"], "description": family["description"],
                "priority": 100, "match": match, "effects": effects}
        rules[family["rule_id"] + ".json"] = json.dumps(rule, indent=2) + "\n"
    overlaps = overlap_report(families)
    if overlaps:
        raise QualificationError(f"{len(overlaps)} cross-package alias overlaps")
    return rules


def check_rules(config=None, evidence=None, rules_dir=RULES_DIR):
    """Return a list of drift problems (empty when committed rules match)."""
    config = config or load_config()
    evidence = evidence or _strict_json(EVIDENCE_PATH)
    check_evidence_integrity(evidence, config)
    generated = generate_rules(config, evidence)
    problems = []
    committed = sorted(n for n in os.listdir(rules_dir) if n.startswith("package.firmware."))
    if committed != sorted(generated):
        problems.append(f"firmware rule files {committed} != generated {sorted(generated)}")
    for name, text in sorted(generated.items()):
        path = os.path.join(rules_dir, name)
        if not os.path.exists(path):
            continue
        with open(path, "rb") as f:
            if f.read().decode("utf-8") != text:
                problems.append(f"{name} differs from the regenerated rule")
    return problems


def _kernel_args(argv):
    names = ["--modules-alias", "--kernel-release", "--kernel-package", "--kernel-package-version"]
    if len(argv) != 8 or argv[0::2] != names:
        raise QualificationError("usage: " + " ".join(f"{n} VALUE" for n in names))
    return argv[1::2]


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        sys.stderr.write(__doc__)
        return 64
    mode, rest = argv[0], argv[1:]
    try:
        config = load_config()
        if mode == "extract":
            sys.stdout.write(dump(extract(config, *_kernel_args(rest))))
            return 0
        if mode == "check-evidence":
            fresh = dump(extract(config, *_kernel_args(rest)))
            with open(EVIDENCE_PATH, "rb") as f:
                committed = f.read().decode("utf-8")
            if fresh != committed:
                sys.stderr.write("FAIL: fresh module-alias extraction differs from committed evidence\n")
                return 1
            print("PASS: committed module-alias evidence regenerates byte-for-byte")
            return 0
        if mode == "check-rules" and not rest:
            problems = check_rules(config)
            for p in problems:
                sys.stderr.write(f"FAIL: {p}\n")
            if problems:
                return 1
            print("PASS: committed firmware rules regenerate byte-for-byte; no cross-package overlap")
            return 0
        if mode == "write-rules" and not rest:
            for name, text in generate_rules(config, _strict_json(EVIDENCE_PATH)).items():
                with open(os.path.join(RULES_DIR, name), "w", encoding="utf-8") as f:
                    f.write(text)
                print(f"wrote hardware/rules/{name}")
            return 0
    except (QualificationError, OSError, UnicodeDecodeError, KeyError, ValueError) as e:
        sys.stderr.write(f"FAIL: {e}\n")
        return 1
    sys.stderr.write(__doc__)
    return 64


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    sys.exit(main())
