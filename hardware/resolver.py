"""LUBARCHY production hardware resolver (M1-C).

inventory v1 + production rule set (rule v1) -> plan v1.

Pure library: it reads an inventory document given by the caller, the
source-controlled rules in hardware/rules/ and the v1 schemas in
hardware/schemas/. It probes no hardware, starts no process, uses no network,
installs and configures nothing, and persists nothing. A plan is a proposal.

Semantics are those of docs/hardware-enablement.md (M1-A contracts). The
test-only reference implementation in tests/ is an independent oracle for
this module and is deliberately not imported here.
"""

import fnmatch
import hashlib
import json
import os
import stat

import jsonschema

HARDWARE_DIR = os.path.dirname(os.path.abspath(__file__))
SCHEMA_DIR = os.path.join(HARDWARE_DIR, "schemas")
PRODUCTION_RULES_DIR = os.path.join(HARDWARE_DIR, "rules")

INVENTORY_SCHEMA_ID = "lubarchy.hardware.inventory/v1"
RULE_SCHEMA_ID = "lubarchy.hardware.rule/v1"
PLAN_SCHEMA_ID = "lubarchy.hardware.plan/v1"
SCHEMA_FILES = {
    "inventory": "inventory-v1.schema.json",
    "rule": "rule-v1.schema.json",
    "plan": "plan-v1.schema.json",
}

# Product source policy (ADR-0001, D-013): Debian main and non-free-firmware,
# in canonical (sorted) order. Resolution records this policy and reports any
# other source as 'policy_review_required'; it never widens it, and it never
# configures APT.
AUTHORISED_SOURCES = ("main", "non-free-firmware")

# Production rule policy (M1-D): classifications and package requirements
# only. Every production package effect must be a 'require' from an
# authorised source. Rule v1 also allows 'forbid', profiles and warnings;
# production rules may not use them yet.
PRODUCTION_EFFECTS = frozenset({"classifications", "packages"})

MAX_DOCUMENT_BYTES = 16 * 1024 * 1024
MAX_RULE_BYTES = 1024 * 1024


class ResolverError(Exception):
    """A document, rule or rule set that cannot be used. Messages carry
    file names and check names only, never rejected values."""

    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Strict JSON and canonical form


def _pairs_without_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate object key")
        out[key] = value
    return out


def _no_float(_text):
    raise ValueError("non-integer number")


def _no_constant(_text):
    raise ValueError("non-standard constant")


def parse_strict(data, code="malformed_json", label="input"):
    """bytes -> JSON value: UTF-8, no duplicate keys, integers only, no NaN."""
    try:
        text = data.decode("utf-8")
        return json.loads(text, object_pairs_hook=_pairs_without_duplicates,
                          parse_float=_no_float, parse_constant=_no_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ResolverError(code, f"{label} is not strict UTF-8 JSON") from None


def _check_canonical(value):
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, list):
        for item in value:
            _check_canonical(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ResolverError("contract_failure", "non-string object key")
            _check_canonical(item)
        return
    raise ResolverError("contract_failure", "value not representable in canonical JSON")


def canonical_json(value):
    """UTF-8, sorted keys, no insignificant whitespace, integers only."""
    _check_canonical(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def digest(value):
    return "sha256:" + hashlib.sha256(canonical_json(value)).hexdigest()


# ---------------------------------------------------------------------------
# Schemas


def _read_regular(path, limit, code, label):
    """Read a regular, non-symlink file without following links."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        raise ResolverError(code, f"{label} cannot be opened as a regular file") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ResolverError(code, f"{label} is not a regular file")
        with os.fdopen(fd, "rb") as f:
            fd = None
            data = f.read(limit + 1)
    finally:
        if fd is not None:
            os.close(fd)
    if len(data) > limit:
        raise ResolverError(code, f"{label} is too large")
    return data


_VALIDATORS = {}


def validator(kind):
    if kind not in _VALIDATORS:
        path = os.path.join(SCHEMA_DIR, SCHEMA_FILES[kind])
        schema = parse_strict(_read_regular(path, MAX_DOCUMENT_BYTES, "schema_error", SCHEMA_FILES[kind]),
                              "schema_error", SCHEMA_FILES[kind])
        jsonschema.Draft202012Validator.check_schema(schema)
        _VALIDATORS[kind] = jsonschema.Draft202012Validator(schema)
    return _VALIDATORS[kind]


def _json_pointer(path):
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in path)


def schema_errors(kind, instance):
    """Sorted unique (JSON Pointer, failed keyword) pairs. A device failing
    the per-bus oneOf reports the errors of the branch its 'bus' selects."""
    found = set()
    for error in validator(kind).iter_errors(instance):
        if error.validator == "oneOf" and isinstance(error.instance, dict):
            branch = {"pci": 0, "usb": 1}.get(error.instance.get("bus"))
            selected = [sub for sub in error.context
                        if branch is not None and sub.relative_schema_path
                        and sub.relative_schema_path[0] == branch]
            if selected:
                found.update((_json_pointer(sub.absolute_path), str(sub.validator)) for sub in selected)
                continue
        found.add((_json_pointer(error.absolute_path), str(error.validator)))
    return sorted(found)


# ---------------------------------------------------------------------------
# Inventory


def inventory_errors(doc):
    """Schema then semantic validation of inventory v1; empty when valid."""
    errors = schema_errors("inventory", doc)
    if errors:
        return errors
    found = set()
    seen = set()
    for index, device in enumerate(doc["devices"]):
        where = f"/devices/{index}"
        if device["id"] in seen:
            found.add((where + "/id", "unique_device_id"))
        seen.add(device["id"])
        if device["id"] != device["bus"] + ":" + device["locator"]:
            found.add((where + "/id", "id_matches_locator"))
        numbers = [itf["number"] for itf in device.get("interfaces", [])]
        if len(set(numbers)) != len(numbers):
            found.add((where + "/interfaces", "unique_interface_number"))
    for index, device in enumerate(doc["devices"]):
        parent = device.get("parent")
        if parent is not None and (parent == device["id"] or parent not in seen):
            found.add((f"/devices/{index}/parent", "parent_exists"))
    return sorted(found)


def normalize_inventory(doc):
    """Canonical order of a valid inventory: devices by id, USB interfaces by
    number, module lists sorted. Values are not rewritten."""
    out = json.loads(json.dumps(doc))
    for device in out["devices"]:
        if "modules" in device:
            device["modules"] = sorted(device["modules"])
        if "interfaces" in device:
            for itf in device["interfaces"]:
                if "modules" in itf:
                    itf["modules"] = sorted(itf["modules"])
            device["interfaces"].sort(key=lambda itf: itf["number"])
    out["devices"].sort(key=lambda device: device["id"])
    return out


# ---------------------------------------------------------------------------
# Rules


def rule_errors(rule):
    errors = schema_errors("rule", rule)
    if errors:
        return errors
    found = set()
    effects = rule["effects"]
    packages = [p["name"] for p in effects.get("packages", [])]
    if len(set(packages)) != len(packages):
        found.add(("/effects/packages", "unique_package_name"))
    profiles = [p["name"] for p in effects.get("profiles", [])]
    if len(set(profiles)) != len(profiles):
        found.add(("/effects/profiles", "unique_profile_name"))
    for index, profile in enumerate(effects.get("profiles", [])):
        if profile["name"] in profile.get("excludes", []):
            found.add((f"/effects/profiles/{index}/excludes", "profile_excludes_itself"))
    return sorted(found)


def _production_policy_violation(rule):
    if set(rule["effects"]) - PRODUCTION_EFFECTS:
        return "has effects other than classifications and packages"
    for package in rule["effects"].get("packages", []):
        if package["disposition"] != "require":
            return "has a package effect that is not 'require'"
        if package["source"] not in AUTHORISED_SOURCES:
            return "requires a package from an unauthorised source"
    return None


def check_rule_set(rules, production_policy=False):
    """Validate a list of rules as one set; any problem rejects the set.
    With production_policy the M1-D production rule policy also applies.
    Returns the rules ordered by id."""
    ids = set()
    for rule in rules:
        rid = rule.get("id") if isinstance(rule, dict) else None
        label = rid if isinstance(rid, str) else "<rule>"
        if rule_errors(rule):
            raise ResolverError("rule_set_rejected", f"rule {label} is not a valid rule v1 document")
        if rid in ids:
            raise ResolverError("rule_set_rejected", f"duplicate rule id {rid}")
        ids.add(rid)
        violation = _production_policy_violation(rule) if production_policy else None
        if violation:
            raise ResolverError("rule_set_rejected", f"rule {rid} {violation}")
    return sorted(rules, key=lambda r: r["id"])


def load_rule_directory(directory, production_policy):
    """Load '<id>.json' rules from one directory, fail-closed.

    Only regular, non-hidden '*.json' files are rules; regular '*.md' files
    are documentation. Anything else (symlinks, hidden entries,
    subdirectories, other files) rejects the whole set, as does any
    unreadable, non-strict or invalid rule, a file name that differs from its
    rule id, or a duplicate id. Subdirectories are never searched."""
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        raise ResolverError("rule_set_rejected", "rule directory cannot be listed") from None
    rules = []
    for name in names:
        path = os.path.join(directory, name)
        try:
            mode = os.lstat(path).st_mode
        except OSError:
            raise ResolverError("rule_set_rejected", f"{name} cannot be inspected") from None
        if name.startswith("."):
            raise ResolverError("rule_set_rejected", f"hidden entry {name} in rule directory")
        if not stat.S_ISREG(mode):
            raise ResolverError("rule_set_rejected", f"{name} is not a regular file")
        if name.endswith(".md"):
            continue
        if not name.endswith(".json"):
            raise ResolverError("rule_set_rejected", f"unexpected file {name} in rule directory")
        rule = parse_strict(_read_regular(path, MAX_RULE_BYTES, "rule_set_rejected", name),
                            "rule_set_rejected", name)
        if rule_errors(rule):
            raise ResolverError("rule_set_rejected", f"{name} is not a valid rule v1 document")
        if name != rule["id"] + ".json":
            raise ResolverError("rule_set_rejected", f"{name} does not match its rule id")
        rules.append(rule)
    return check_rule_set(rules, production_policy)


def load_production_rules():
    """The production rule set: hardware/rules/ under the production policy."""
    return load_rule_directory(PRODUCTION_RULES_DIR, production_policy=True)


def rule_set_digest(rules):
    return digest(sorted(rules, key=lambda r: r["id"]))


def evaluation_order(rules):
    """Priority descending, then rule id ascending. Ordering only."""
    return sorted(rules, key=lambda r: (-r["priority"], r["id"]))


# ---------------------------------------------------------------------------
# Matching (an absent observed field never satisfies a condition)


def _one_of(observed, allowed):
    return observed is not None and observed in allowed


def _glob(observed, patterns):
    return observed is not None and any(fnmatch.fnmatchcase(observed, p) for p in patterns)


def _pci_matches(sel, device):
    if device["bus"] != "pci":
        return False
    for field in ("vendor_id", "device_id", "subsystem_vendor_id", "subsystem_device_id"):
        if field in sel and not _one_of(device.get(field), sel[field]):
            return False
    if "class_id" in sel:
        observed = device.get("class_id")
        if observed is None or not any(observed.startswith(p) for p in sel["class_id"]):
            return False
    return "modalias" not in sel or _glob(device.get("modalias"), sel["modalias"])


_USB_INTERFACE_FIELD = {"interface_class": "class", "interface_subclass": "subclass",
                        "interface_protocol": "protocol"}


def _usb_matches(sel, device):
    if device["bus"] != "usb":
        return False
    for field in ("vendor_id", "product_id", "device_class", "device_subclass", "device_protocol"):
        if field in sel and not _one_of(device.get(field), sel[field]):
            return False
    itf_fields = [f for f in _USB_INTERFACE_FIELD if f in sel]
    if not itf_fields and "modalias" not in sel:
        return True
    return any(
        all(_one_of(itf.get(_USB_INTERFACE_FIELD[f]), sel[f]) for f in itf_fields)
        and ("modalias" not in sel or _glob(itf.get("modalias"), sel["modalias"]))
        for itf in device.get("interfaces", [])
    )


def _cpu_matches(sel, cpu):
    return all(_one_of(cpu.get(field), allowed) for field, allowed in sel.items())


def _platform_matches(sel, system):
    virt = system.get("virtualization") or {}
    facts = {
        "architecture": system.get("architecture"),
        "boot_firmware": system.get("boot_firmware"),
        "virtualization_kind": virt.get("kind"),
        "virtualization_technology": virt.get("technology"),
    }
    return all(_one_of(facts[field], allowed) for field, allowed in sel.items())


def matched_targets(rule, inventory):
    targets = set()
    for selector in rule["match"]:
        ((scope, sel),) = selector.items()
        if scope == "cpu":
            if _cpu_matches(sel, inventory["cpu"]):
                targets.add("cpu")
        elif scope == "platform":
            if _platform_matches(sel, inventory["system"]):
                targets.add("platform")
        elif scope == "pci":
            targets.update(d["id"] for d in inventory["devices"] if _pci_matches(sel, d))
        elif scope == "usb":
            targets.update(d["id"] for d in inventory["devices"] if _usb_matches(sel, d))
    return sorted(targets)


# ---------------------------------------------------------------------------
# Resolution


def _base_plan(rules):
    return {
        "schema": PLAN_SCHEMA_ID,
        "inventory_schema": INVENTORY_SCHEMA_ID,
        "rule_schema": RULE_SCHEMA_ID,
        "rule_set_digest": rule_set_digest(rules),
        "source_policy": {"authorised": sorted(set(AUTHORISED_SOURCES))},
        "matched_rules": [],
        "classifications": [],
        "packages": [],
        "profiles": [],
        "sources": [],
        "warnings": [],
        "unresolved": [],
        "conflicts": [],
    }


def resolve(inventory, rules):
    """Resolve one inventory against an already validated rule set (from
    load_production_rules or check_rule_set). Rules are additive; any
    conflict fails closed; no last-rule-wins; unmatched targets are reported
    as unresolved and receive nothing; non-main sources are reported as
    'policy_review_required'."""
    rules = check_rule_set(rules)
    plan = _base_plan(rules)
    errors = inventory_errors(inventory)
    if errors:
        plan["status"] = "invalid"
        plan["errors"] = [{"path": path, "check": check} for path, check in errors]
        return plan

    inv = normalize_inventory(inventory)
    plan["inventory_digest"] = digest(inv)
    order = evaluation_order(rules)
    position = {rule["id"]: i for i, rule in enumerate(order)}

    classes = {}
    requires = {}  # package -> {source: [rule ids]}
    forbids = {}  # package -> [rule ids]
    package_reasons = {}
    profiles = {}
    covered = set()
    for rule in order:
        targets = matched_targets(rule, inv)
        if not targets:
            continue
        rid = rule["id"]
        covered.update(targets)
        plan["matched_rules"].append({"id": rid, "priority": rule["priority"], "targets": targets})
        effects = rule["effects"]
        for name in effects.get("classifications", []):
            entry = classes.setdefault(name, {"rules": [], "targets": set()})
            entry["rules"].append(rid)
            entry["targets"].update(targets)
        for package in effects.get("packages", []):
            name = package["name"]
            if package["disposition"] == "require":
                requires.setdefault(name, {}).setdefault(package["source"], []).append(rid)
            else:
                forbids.setdefault(name, []).append(rid)
            package_reasons.setdefault(name, []).append({"rule": rid, "reason": package["reason"]})
        for profile in effects.get("profiles", []):
            entry = profiles.setdefault(profile["name"], {"excludes": set(), "reasons": [], "rules": []})
            entry["excludes"].update(profile.get("excludes", []))
            entry["reasons"].append({"rule": rid, "reason": profile["reason"]})
            entry["rules"].append(rid)
        for warning in effects.get("warnings", []):
            plan["warnings"].append({"code": warning["code"], "message": warning["message"], "rule": rid})

    def ordered(rule_ids):
        return sorted(set(rule_ids), key=position.__getitem__)

    conflicts = []
    for name in set(requires) & set(forbids):
        involved = [r for ids in requires[name].values() for r in ids] + forbids[name]
        conflicts.append({"kind": "package_required_and_forbidden", "subject": name, "rules": ordered(involved)})
    for name, by_source in requires.items():
        if len(by_source) > 1:
            involved = [r for ids in by_source.values() for r in ids]
            conflicts.append({"kind": "package_source_mismatch", "subject": name, "rules": ordered(involved)})
    for name, entry in profiles.items():
        excluders = [other for other, e in profiles.items() if name in e["excludes"]]
        if excluders:
            involved = list(entry["rules"]) + [r for other in excluders for r in profiles[other]["rules"]]
            conflicts.append({"kind": "profile_exclusion", "subject": name, "rules": ordered(involved)})
    plan["conflicts"] = sorted(conflicts, key=lambda c: (c["kind"], c["subject"]))

    plan["classifications"] = [
        {"name": name, "rules": entry["rules"], "targets": sorted(entry["targets"])}
        for name, entry in sorted(classes.items())
    ]
    targets = sorted(["cpu"] + [device["id"] for device in inv["devices"]])
    plan["unresolved"] = [{"target": t, "reason": "no_rule_matched"} for t in targets if t not in covered]

    if plan["conflicts"]:
        plan["status"] = "conflict"
        return plan

    def source_status(source):
        return "authorised" if source in AUTHORISED_SOURCES else "policy_review_required"

    by_source = {}
    for name in sorted(set(requires) | set(forbids)):
        if name in requires:
            (source,) = requires[name]
            plan["packages"].append({"name": name, "disposition": "require", "source": source,
                                     "source_status": source_status(source),
                                     "reasons": package_reasons[name]})
            by_source.setdefault(source, []).append(name)
        else:
            plan["packages"].append({"name": name, "disposition": "forbid", "reasons": package_reasons[name]})
    plan["sources"] = [{"source": source, "status": source_status(source), "packages": sorted(names)}
                       for source, names in sorted(by_source.items())]
    for name, entry in sorted(profiles.items()):
        item = {"name": name, "reasons": entry["reasons"]}
        if entry["excludes"]:
            item["excludes"] = sorted(entry["excludes"])
        plan["profiles"].append(item)

    needs_review = any(s["status"] == "policy_review_required" for s in plan["sources"])
    plan["status"] = "partial" if plan["unresolved"] or needs_review else "resolved"
    return plan


def check_plan(plan):
    """A produced plan must satisfy plan v1; anything else is a defect."""
    if schema_errors("plan", plan):
        raise ResolverError("contract_failure", "produced plan does not satisfy plan v1")
    return plan
