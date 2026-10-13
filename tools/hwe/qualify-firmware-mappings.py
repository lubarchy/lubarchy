#!/usr/bin/python3
"""LUBARCHY device firmware mapping qualification (M1-E, strengthened in M1-F).

qualified Debian kernel (module aliases + package-owned module metadata)
+ authenticated Debian package evidence -> deterministic tooling ->
reviewed, committed production rules (hardware/rules/package.firmware.*.json).

Modes (nothing is written unless 'write-rules' is given explicitly):

  extract KERNEL-ARGS PACKAGE-ARGS
      Print normalized firmware evidence (lubarchy.hardware.firmware-evidence/v2)
      to stdout.
  check-evidence KERNEL-ARGS PACKAGE-ARGS
      Compare a fresh extraction with the committed evidence; exit 1 on drift.
  check-rules
      Generate rules from the committed evidence and configuration, check
      evidence integrity, rule-level overlap and firmware attribution, and
      compare with the committed rules; exit 1 on any problem. This is what
      the static gate runs.
  write-rules
      Explicit generation mode: write the generated rule files.

KERNEL-ARGS:  --modules-alias PATH --kernel-release R --kernel-package P
              --kernel-package-version V --module-dir DIR
PACKAGE-ARGS: --contents TSV --packages JSON
  TSV:  'usr/lib/firmware/<path>' TAB package TAB index, from authenticated
        Debian Contents indices.
  JSON: {package: {version, section, deb_sha256, manifest_sha256, files}}
        from authenticated package payload inspection.

The production resolver never reads kernel or package metadata; mappings
change only by requalifying, regenerating, reviewing and committing. This
tool reads files only; it starts no process and uses no network.
"""

import hashlib
import json
import lzma
import os
import re
import struct
import sys

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(TOOL_DIR))
CONFIG_PATH = os.path.join(TOOL_DIR, "firmware-mappings.json")
EVIDENCE_PATH = os.path.join(TOOL_DIR, "module-alias-evidence.json")
RULES_DIR = os.path.join(REPO_ROOT, "hardware", "rules")

EVIDENCE_SCHEMA = "lubarchy.hardware.firmware-evidence/v2"
RULE_SCHEMA_ID = "lubarchy.hardware.rule/v1"
FIRMWARE_SOURCE = "non-free-firmware"
FIRMWARE_PREFIX = "usr/lib/firmware/"
MODULE_NAME = re.compile(r"[a-z0-9_]{1,64}")  # configured (mapped/excluded) modules
ALIAS_FILE_MODULE = re.compile(r"[A-Za-z0-9_]{1,64}")  # any module in modules.alias (e.g. BusLogic)
EVIDENCE_MODES = ("package-description", "declared-firmware-closure")
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
        if family.get("evidence_mode", "package-description") not in EVIDENCE_MODES:
            raise QualificationError(f"{family['rule_id']}: unknown evidence mode")
        if family["package"] not in config["allowed_packages"]:
            raise QualificationError(f"{family['rule_id']}: package {family['package']} not allowed")
        for module in family["modules"]:
            if not MODULE_NAME.fullmatch(module) or module in seen:
                raise QualificationError(f"bad or repeated module {module!r}")
            seen.add(module)
    if seen & set(config.get("excluded_modules", {})):
        raise QualificationError("a module is both mapped and excluded")
    for alias in config.get("excluded_aliases", {}):
        if parse_alias(alias) is None:
            raise QualificationError(f"excluded alias {alias!r} is not representable")
    return config


def family_mode(family):
    return family.get("evidence_mode", "package-description")


# ---------------------------------------------------------------------------
# Aliases


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


def _device_key(alias):
    """(bus, vendor, device/product) when both are concrete, else None."""
    parsed = parse_alias(alias)
    if parsed is None or "*" in parsed[1][0] or "*" in parsed[1][1]:
        return None
    return parsed[0], parsed[1][0], parsed[1][1]


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


def _digest(values):
    return "sha256:" + hashlib.sha256(("\n".join(values) + "\n").encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Package-owned module metadata (.modinfo ELF section)


def read_modinfo(path):
    """{key: [values]} from the .modinfo section of a (compressed) module."""
    with open(path, "rb") as f:
        data = f.read()
    if path.endswith(".xz"):
        data = lzma.decompress(data)
    elif not path.endswith(".ko"):
        raise QualificationError(f"unsupported module compression: {os.path.basename(path)}")
    if data[:4] != b"\x7fELF" or data[4] != 2 or data[5] != 1:
        raise QualificationError(f"{os.path.basename(path)}: not a little-endian ELF64 module")
    shoff, = struct.unpack_from("<Q", data, 0x28)
    shentsize, shnum, shstrndx = struct.unpack_from("<HHH", data, 0x3A)
    sections = [struct.unpack_from("<IIQQQQIIQQ", data, shoff + i * shentsize) for i in range(shnum)]
    strtab = sections[shstrndx]
    names = data[strtab[4]:strtab[4] + strtab[5]]
    info = {}
    for sh in sections:
        name = names[sh[0]:names.index(b"\0", sh[0])]
        if name == b".modinfo":
            for entry in data[sh[4]:sh[4] + sh[5]].split(b"\0"):
                if b"=" in entry:
                    key, value = entry.decode("utf-8").split("=", 1)
                    info.setdefault(key, []).append(value)
            return info
    raise QualificationError(f"{os.path.basename(path)}: no .modinfo section")


def module_index(module_dir):
    """module name -> path relative to module_dir (kernel/...)."""
    index = {}
    for root, _, files in os.walk(os.path.join(module_dir, "kernel")):
        for f in sorted(files):
            m = re.fullmatch(r"(.+)\.ko(\.xz)?", f)
            if m:
                index[m.group(1).replace("-", "_")] = os.path.relpath(os.path.join(root, f), module_dir)
    return index


def _closure(module, index, module_dir, cache):
    order, todo = [], [module]
    while todo:
        m = todo.pop()
        if m in order:
            continue
        order.append(m)
        if m not in cache:
            cache[m] = read_modinfo(os.path.join(module_dir, index[m]))
        deps = [d.replace("-", "_") for v in cache[m].get("depends", []) for d in v.split(",") if d]
        todo.extend(sorted(deps, reverse=True))
    return sorted(order)


def read_contents(path):
    owners = {}
    with open(path, "rb") as f:
        for number, line in enumerate(f.read().decode("utf-8").splitlines(), 1):
            parts = line.split("\t")
            if len(parts) != 3 or not parts[0].startswith(FIRMWARE_PREFIX):
                raise QualificationError(f"malformed Contents row {number}")
            owners.setdefault(parts[0][len(FIRMWARE_PREFIX):], set()).add(parts[1])
    return owners


def _owners_of(request, owners):
    found = set()
    for suffix in ("", ".zst", ".xz"):
        found |= owners.get(request + suffix, set())
    return sorted(found)


# ---------------------------------------------------------------------------
# Evidence


def extract(config, modules_alias, kernel_release, kernel_package, kernel_package_version,
            module_dir, contents_path, packages_path):
    """Normalized v2 evidence for the configured modules."""
    table = read_module_aliases(modules_alias)
    index = module_index(module_dir)
    owners = read_contents(contents_path)
    packages = _strict_json(packages_path)
    cache = {}
    modules, excluded, firmware = {}, {}, {}
    mapped = {m for f in config["families"] for m in f["modules"]}
    wanted = sorted(mapped | set(config.get("excluded_modules", {})))
    for module in wanted:
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
        if module in mapped:
            closure = _closure(module, index, module_dir, cache)
            requests = sorted({r for m in closure for r in cache[m].get("firmware", [])})
            firmware[module] = {"path": index[module], "closure": closure,
                                "requests": {r: _owners_of(r, owners) for r in requests}}
    # Device-specific claims on mapped aliases by modules outside the family.
    family_of = {m: f for f in config["families"] for m in f["modules"]}
    keyed = {}
    for m, aliases in table.items():
        for a in aliases:
            k = _device_key(a)
            if k:
                keyed.setdefault(k, set()).add((m, a))
    shared = {}
    for module in sorted(mapped):
        family = family_of[module]
        for a in modules[module]["aliases"]:
            k = _device_key(a)
            if not k:
                continue
            for other, oa in sorted(keyed.get(k, ())):
                if other in family["modules"] or not aliases_overlap(a, oa):
                    continue
                if other in family_of:
                    other_packages = [family_of[other]["package"]]
                elif other in index:
                    cl = _closure(other, index, module_dir, cache)
                    other_packages = sorted({p for m in cl for r in cache[m].get("firmware", [])
                                             for p in _owners_of(r, owners)})
                else:
                    other_packages = ["?builtin-or-missing"]
                entry = shared.setdefault(module, {}).setdefault(other, {"aliases": [], "packages": other_packages})
                if a not in entry["aliases"]:
                    entry["aliases"].append(a)
    for module in shared.values():
        for entry in module.values():
            entry["aliases"].sort()
    relevant = sorted({f["package"] for f in config["families"]})
    collisions = sorted(path for path, pkgs in owners.items() if len(pkgs) > 1 and pkgs & set(relevant))
    return {
        "schema": EVIDENCE_SCHEMA,
        "description": "Normalized firmware mapping evidence: PCI/USB kernel module aliases, declared firmware requests of each mapped module and its dependency closure with their authenticated Debian owners, device-specific claims by other modules, and authenticated package payload facts. Generated by tools/hwe/qualify-firmware-mappings.py extract; qualification provenance only.",
        "kernel_release": kernel_release,
        "kernel_package": kernel_package,
        "kernel_package_version": kernel_package_version,
        "source": f"/lib/modules/{kernel_release}/modules.alias",
        "modules": modules,
        "excluded": excluded,
        "firmware": firmware,
        "shared_claims": shared,
        "packages": {p: packages[p] for p in relevant},
        "contents_digest": "sha256:" + hashlib.sha256(open(contents_path, "rb").read()).hexdigest(),
        "firmware_file_collisions": collisions,
    }


def dump(value):
    return json.dumps(value, indent=2, sort_keys=False, ensure_ascii=True) + "\n"


def check_evidence_integrity(evidence, config):
    """Committed evidence must be normalized and self-consistent (v1 or v2)."""
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
    if evidence.get("schema") == EVIDENCE_SCHEMA:
        mapped = sorted(m for f in config["families"] for m in f["modules"])
        if sorted(evidence["firmware"]) != mapped:
            raise QualificationError("firmware provenance does not cover exactly the mapped modules")
    elif "schema" in evidence:
        raise QualificationError(f"unknown evidence schema {evidence['schema']!r}")


def attribution_problems(evidence, config):
    """Strengthened M1-F rule for 'declared-firmware-closure' families: every
    module's declared firmware closure resolves to the family package (at
    least one request) and to no other package; device-specific claims by
    other modules are same-package, reviewed or excluded; no relevant
    firmware file is shipped by two packages."""
    problems = []
    if evidence.get("schema") != EVIDENCE_SCHEMA:
        return problems
    excluded_aliases = set(config.get("excluded_aliases", {}))
    reviewed = config.get("reviewed_shared_claims", {})
    reviewed_foreign = config.get("reviewed_foreign_requests", {})
    for family in config["families"]:
        pkg = family["package"]
        for module in family["modules"]:
            fw = evidence["firmware"][module]
            owned = [r for r, o in fw["requests"].items() if pkg in o]
            foreign = sorted(r for r, o in fw["requests"].items() if set(o) - {pkg})
            if family_mode(family) == "declared-firmware-closure":
                if not owned:
                    problems.append(f"{module}: no declared firmware request is shipped by {pkg}")
                if foreign:
                    problems.append(f"{module}: declared firmware also resolves to another package: {foreign[:3]}")
            else:
                unreviewed = [r for r in foreign if f"{module}|{r}" not in reviewed_foreign]
                if unreviewed:
                    problems.append(f"{module}: unreviewed request(s) shipped by another package: {unreviewed[:3]}")
            for other, entry in evidence["shared_claims"].get(module, {}).items():
                if entry["packages"] == [pkg]:
                    continue
                if all(a in excluded_aliases for a in entry["aliases"]):
                    continue
                if f"{module}|{other}" not in reviewed:
                    problems.append(f"{module}: aliases also claimed by {other} ({entry['packages']}) without review")
    if evidence["firmware_file_collisions"]:
        problems.append(f"firmware files shipped by two packages: {evidence['firmware_file_collisions'][:3]}")
    return problems


def overlap_report(families):
    """Overlaps between rules whose package or classifications differ
    (must be empty). families: [(key, aliases)]."""
    found = []
    for i, (key_a, aliases_a) in enumerate(families):
        for key_b, aliases_b in families[i + 1:]:
            if key_a == key_b:
                continue
            index = {}
            for b in aliases_b:
                k = _device_key(b)
                index.setdefault(k[1:] if k else None, []).append(b)
            for a in aliases_a:
                k = _device_key(a)
                pool = aliases_b if k is None else index.get(k[1:], []) + index.get(None, [])
                for b in pool:
                    if aliases_overlap(a, b):
                        found.append((key_a, a, key_b, b))
    return found


def generate_rules(config, evidence):
    rules = {}
    families = []
    excluded_aliases = set(config.get("excluded_aliases", {}))
    for family in config["families"]:
        aliases = sorted({a for m in family["modules"] for a in evidence["modules"][m]["aliases"]} - excluded_aliases)
        families.append(((family["package"], tuple(family["classifications"])), aliases))
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
        raise QualificationError(f"{len(overlaps)} alias overlaps between rules with different package or classification")
    return rules


def check_rules(config=None, evidence=None, rules_dir=RULES_DIR):
    """Return a list of problems (empty when committed rules match)."""
    config = config or load_config()
    evidence = evidence or _strict_json(EVIDENCE_PATH)
    check_evidence_integrity(evidence, config)
    problems = attribution_problems(evidence, config)
    generated = generate_rules(config, evidence)
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


ARG_NAMES = ["--modules-alias", "--kernel-release", "--kernel-package", "--kernel-package-version",
             "--module-dir", "--contents", "--packages"]


def _extract_args(argv):
    if len(argv) != 2 * len(ARG_NAMES) or argv[0::2] != ARG_NAMES:
        raise QualificationError("usage: " + " ".join(f"{n} VALUE" for n in ARG_NAMES))
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
            sys.stdout.write(dump(extract(config, *_extract_args(rest))))
            return 0
        if mode == "check-evidence":
            fresh = dump(extract(config, *_extract_args(rest)))
            with open(EVIDENCE_PATH, "rb") as f:
                committed = f.read().decode("utf-8")
            if fresh != committed:
                sys.stderr.write("FAIL: fresh extraction differs from committed evidence\n")
                return 1
            print("PASS: committed firmware evidence regenerates byte-for-byte")
            return 0
        if mode == "check-rules" and not rest:
            problems = check_rules(config)
            for p in problems:
                sys.stderr.write(f"FAIL: {p}\n")
            if problems:
                return 1
            print("PASS: committed firmware rules regenerate byte-for-byte; attribution and overlap checks pass")
            return 0
        if mode == "write-rules" and not rest:
            evidence = _strict_json(EVIDENCE_PATH)
            check_evidence_integrity(evidence, config)
            problems = attribution_problems(evidence, config)
            if problems:
                raise QualificationError("; ".join(problems))
            for name, text in generate_rules(config, evidence).items():
                with open(os.path.join(RULES_DIR, name), "w", encoding="utf-8") as f:
                    f.write(text)
                print(f"wrote hardware/rules/{name}")
            return 0
    except (QualificationError, OSError, UnicodeDecodeError, KeyError, ValueError, lzma.LZMAError, struct.error) as e:
        sys.stderr.write(f"FAIL: {e}\n")
        return 1
    sys.stderr.write(__doc__)
    return 64


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    sys.exit(main())
