"""LUBARCHY M1-A reference implementation of the hardware contracts.

Pure functions that define, executably, the semantics documented in
docs/hardware-enablement.md for inventory v1, rule v1 and plan v1:
strict JSON loading, validation, normalization, canonical JSON, digests and
rule resolution.

This is test tooling for the contracts. It probes no hardware, reads no
system state and installs or configures nothing; a plan is only a proposal.
It is not the production hardware-enablement component.
"""

import fnmatch
import hashlib
import json
import pathlib

import jsonschema

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEMA_DIR = REPO_ROOT / "hardware" / "schemas"

INVENTORY_SCHEMA_ID = "lubarchy.hardware.inventory/v1"
RULE_SCHEMA_ID = "lubarchy.hardware.rule/v1"
PLAN_SCHEMA_ID = "lubarchy.hardware.plan/v1"

# The current installation policy authorises Debian main only (M0 baseline).
# Resolution records this policy; it never widens it.
DEFAULT_AUTHORISED_SOURCES = ("main",)


class ContractError(Exception):
    """A document or rule set violates a contract."""


# ---------------------------------------------------------------------------
# Strict JSON loading and canonical JSON


def _reject_duplicate_keys(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ContractError(f"duplicate JSON object key: {key!r}")
        obj[key] = value
    return obj


def _reject_float(text):
    raise ContractError(f"non-integer number not allowed: {text}")


def _reject_constant(text):
    raise ContractError(f"non-standard JSON constant not allowed: {text}")


def loads_strict(text):
    """Parse JSON, rejecting duplicate keys, non-integer numbers, NaN/Infinity."""
    return json.loads(
        text,
        object_pairs_hook=_reject_duplicate_keys,
        parse_float=_reject_float,
        parse_constant=_reject_constant,
    )


def load_json(path):
    with open(path, "rb") as f:
        data = f.read()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ContractError(f"{path}: not UTF-8") from e
    return loads_strict(text)


def _check_canonical_value(value):
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return
    if isinstance(value, float):
        raise ContractError("floats are not allowed in canonical JSON")
    if isinstance(value, list):
        for item in value:
            _check_canonical_value(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ContractError("object keys must be strings")
            _check_canonical_value(item)
        return
    raise ContractError(f"unsupported type in canonical JSON: {type(value).__name__}")


def canonical_json(value):
    """Canonical JSON bytes: UTF-8, object keys sorted, no insignificant
    whitespace, integers only. Array order is significant and must already be
    canonical (see normalize_inventory). For documents valid under the v1
    schemas (ASCII keys, integers only) this matches RFC 8785 (JCS)."""
    _check_canonical_value(value)
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def sha256_digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Schemas


def load_schema(name):
    return load_json(SCHEMA_DIR / name)


SCHEMA_FILES = {
    "inventory": "inventory-v1.schema.json",
    "rule": "rule-v1.schema.json",
    "plan": "plan-v1.schema.json",
}


def validator(kind):
    schema = load_schema(SCHEMA_FILES[kind])
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


def _pointer(path):
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in path)


def _schema_errors(v, instance):
    """(path, check) pairs for every schema violation. For a device that
    fails the per-bus oneOf, the errors of the branch selected by its 'bus'
    are reported instead of the bare oneOf failure."""
    found = []
    for err in v.iter_errors(instance):
        if err.validator == "oneOf" and isinstance(err.instance, dict):
            branch = {"pci": 0, "usb": 1}.get(err.instance.get("bus"))
            sub = [
                c for c in err.context
                if branch is not None and c.relative_schema_path and c.relative_schema_path[0] == branch
            ]
            if sub:
                for c in sub:
                    found.append((_pointer(c.absolute_path), str(c.validator)))
                continue
        found.append((_pointer(err.absolute_path), str(err.validator)))
    return sorted(set(found))


# ---------------------------------------------------------------------------
# Inventory


def inventory_errors(doc, v=None):
    """Schema and semantic validation. Returns sorted (path, check) pairs;
    empty means valid. Semantic checks cover what JSON Schema cannot express:
    unique device ids, id/locator agreement, parent references, unique USB
    interface numbers."""
    v = v or validator("inventory")
    errors = _schema_errors(v, doc)
    if errors:
        return errors
    ids = {}
    for i, dev in enumerate(doc["devices"]):
        path = f"/devices/{i}"
        if dev["id"] in ids:
            errors.append((path + "/id", "unique_device_id"))
        ids[dev["id"]] = i
        if dev["id"] != f'{dev["bus"]}:{dev["locator"]}':
            errors.append((path + "/id", "id_matches_locator"))
        numbers = [itf["number"] for itf in dev.get("interfaces", [])]
        if len(numbers) != len(set(numbers)):
            errors.append((path + "/interfaces", "unique_interface_number"))
    for i, dev in enumerate(doc["devices"]):
        parent = dev.get("parent")
        if parent is not None and (parent not in ids or parent == dev["id"]):
            errors.append((f"/devices/{i}/parent", "parent_exists"))
    return sorted(set(errors))


def normalize_inventory(doc):
    """Return the canonical form of a valid inventory: devices ordered by id,
    USB interfaces by number, module lists sorted. Object key order is fixed
    later by canonical_json. Values are not rewritten: the schema already
    requires normalized (lowercase, fixed-width) identifiers."""
    out = json.loads(json.dumps(doc))
    for dev in out["devices"]:
        if "modules" in dev:
            dev["modules"] = sorted(dev["modules"])
        if "interfaces" in dev:
            for itf in dev["interfaces"]:
                if "modules" in itf:
                    itf["modules"] = sorted(itf["modules"])
            dev["interfaces"] = sorted(dev["interfaces"], key=lambda itf: itf["number"])
    out["devices"] = sorted(out["devices"], key=lambda dev: dev["id"])
    return out


def inventory_digest(doc):
    return sha256_digest(canonical_json(normalize_inventory(doc)))


def normalize_hex_id(raw, width):
    """Normalize a raw sysfs identifier such as '0x10DE' to fixed-width
    lowercase hex ('10de'). Reference for future probes; rejects values
    that do not fit, instead of inventing or truncating digits."""
    text = raw.strip().lower()
    if text.startswith("0x"):
        text = text[2:]
    if not text or len(text) > width or any(c not in "0123456789abcdef" for c in text):
        raise ContractError(f"not a {width}-digit hex identifier: {raw!r}")
    return text.rjust(width, "0")


# ---------------------------------------------------------------------------
# Rules


def rule_errors(rule, v=None):
    v = v or validator("rule")
    errors = _schema_errors(v, rule)
    if errors:
        return errors
    names = [p["name"] for p in rule["effects"].get("packages", [])]
    if len(names) != len(set(names)):
        errors.append(("/effects/packages", "unique_package_name"))
    profiles = [p["name"] for p in rule["effects"].get("profiles", [])]
    if len(profiles) != len(set(profiles)):
        errors.append(("/effects/profiles", "unique_profile_name"))
    for i, prof in enumerate(rule["effects"].get("profiles", [])):
        if prof["name"] in prof.get("excludes", []):
            errors.append((f"/effects/profiles/{i}/excludes", "profile_excludes_itself"))
    return sorted(set(errors))


def load_rule_set(directory):
    """Load every '*.json' rule in a directory. Each file must be a valid
    rule named '<id>.json'; rule ids must be unique. Any error rejects the
    whole rule set. Returned rules are ordered by id."""
    directory = pathlib.Path(directory)
    v = validator("rule")
    rules = []
    for path in sorted(directory.glob("*.json")):
        rule = load_json(path)
        errs = rule_errors(rule, v)
        if errs:
            raise ContractError(f"{path.name}: invalid rule: {errs}")
        if path.name != rule["id"] + ".json":
            raise ContractError(f"{path.name}: file name does not match rule id {rule['id']!r}")
        rules.append(rule)
    return check_rule_set(rules)


def check_rule_set(rules):
    v = validator("rule")
    seen = set()
    for rule in rules:
        errs = rule_errors(rule, v)
        if errs:
            raise ContractError(f"{rule.get('id')!r}: invalid rule: {errs}")
        if rule["id"] in seen:
            raise ContractError(f"duplicate rule id: {rule['id']!r}")
        seen.add(rule["id"])
    return sorted(rules, key=lambda r: r["id"])


def rule_set_digest(rules):
    return sha256_digest(canonical_json(sorted(rules, key=lambda r: r["id"])))


def evaluation_order(rules):
    """Higher priority first, then ascending rule id. Ordering only."""
    return sorted(rules, key=lambda r: (-r["priority"], r["id"]))


# ---------------------------------------------------------------------------
# Matching. An absent observed field never satisfies a condition on it.


def _field_in(observed, allowed):
    return observed is not None and observed in allowed


def _glob_any(observed, patterns):
    return observed is not None and any(fnmatch.fnmatchcase(observed, p) for p in patterns)


def match_pci(sel, dev):
    if dev["bus"] != "pci":
        return False
    for field in ("vendor_id", "device_id", "subsystem_vendor_id", "subsystem_device_id"):
        if field in sel and not _field_in(dev.get(field), sel[field]):
            return False
    if "class_id" in sel:
        cls = dev.get("class_id")
        if cls is None or not any(cls.startswith(prefix) for prefix in sel["class_id"]):
            return False
    if "modalias" in sel and not _glob_any(dev.get("modalias"), sel["modalias"]):
        return False
    return True


_USB_DEVICE_FIELDS = ("vendor_id", "product_id", "device_class", "device_subclass", "device_protocol")
_USB_INTERFACE_FIELDS = {
    "interface_class": "class",
    "interface_subclass": "subclass",
    "interface_protocol": "protocol",
}


def match_usb(sel, dev):
    if dev["bus"] != "usb":
        return False
    for field in _USB_DEVICE_FIELDS:
        if field in sel and not _field_in(dev.get(field), sel[field]):
            return False
    wanted = [f for f in _USB_INTERFACE_FIELDS if f in sel]
    if not wanted and "modalias" not in sel:
        return True
    for itf in dev.get("interfaces", []):
        if all(_field_in(itf.get(_USB_INTERFACE_FIELDS[f]), sel[f]) for f in wanted) and (
            "modalias" not in sel or _glob_any(itf.get("modalias"), sel["modalias"])
        ):
            return True
    return False


def match_cpu(sel, cpu):
    return all(_field_in(cpu.get(field), sel[field]) for field in sel)


def match_platform(sel, system):
    virt = system.get("virtualization", {})
    observed = {
        "architecture": system.get("architecture"),
        "boot_firmware": system.get("boot_firmware"),
        "virtualization_kind": virt.get("kind"),
        "virtualization_technology": virt.get("technology"),
    }
    return all(_field_in(observed[field], sel[field]) for field in sel)


def rule_targets(rule, inv):
    """Targets ('cpu', 'platform', device ids) matched by any selector."""
    targets = set()
    for selector in rule["match"]:
        (scope, sel), = selector.items()
        if scope == "cpu":
            if match_cpu(sel, inv["cpu"]):
                targets.add("cpu")
        elif scope == "platform":
            if match_platform(sel, inv["system"]):
                targets.add("platform")
        else:
            matcher = match_pci if scope == "pci" else match_usb
            for dev in inv["devices"]:
                if matcher(sel, dev):
                    targets.add(dev["id"])
    return sorted(targets)


# ---------------------------------------------------------------------------
# Resolution


def _empty_plan(authorised):
    return {
        "schema": PLAN_SCHEMA_ID,
        "inventory_schema": INVENTORY_SCHEMA_ID,
        "rule_schema": RULE_SCHEMA_ID,
        "source_policy": {"authorised": sorted(set(authorised))},
        "matched_rules": [],
        "classifications": [],
        "packages": [],
        "profiles": [],
        "sources": [],
        "warnings": [],
        "unresolved": [],
        "conflicts": [],
    }


def resolve(inventory, rules, authorised_sources=DEFAULT_AUTHORISED_SOURCES):
    """Resolve one inventory against one rule set into a plan v1 document.

    Rules are additive. Conflicting effects fail closed (status 'conflict',
    no package, profile or source proposed); there is no last-rule-wins.
    Targets that no rule matches are reported as unresolved and receive
    nothing. Sources outside authorised_sources are reported as
    'policy_review_required'; nothing is enabled."""
    rules = check_rule_set(rules)
    authorised = set(authorised_sources)
    plan = _empty_plan(authorised)
    plan["rule_set_digest"] = rule_set_digest(rules)

    errors = inventory_errors(inventory)
    if errors:
        plan["status"] = "invalid"
        plan["errors"] = [{"path": p, "check": c} for p, c in errors]
        return plan

    inv = normalize_inventory(inventory)
    plan["inventory_digest"] = sha256_digest(canonical_json(inv))

    classifications = {}
    required = {}  # package -> {source -> [rule ids]}
    forbidden = {}  # package -> [rule ids]
    reasons = {}  # package -> [{rule, reason}]
    profiles = {}  # profile -> {"excludes": set, "reasons": [...], "rules": [...]}
    covered = set()

    for rule in evaluation_order(rules):
        targets = rule_targets(rule, inv)
        if not targets:
            continue
        covered.update(targets)
        rid = rule["id"]
        plan["matched_rules"].append({"id": rid, "priority": rule["priority"], "targets": targets})
        effects = rule["effects"]
        for name in effects.get("classifications", []):
            entry = classifications.setdefault(name, {"rules": [], "targets": set()})
            entry["rules"].append(rid)
            entry["targets"].update(targets)
        for pkg in effects.get("packages", []):
            name = pkg["name"]
            if pkg["disposition"] == "require":
                required.setdefault(name, {}).setdefault(pkg["source"], []).append(rid)
            else:
                forbidden.setdefault(name, []).append(rid)
            reasons.setdefault(name, []).append({"rule": rid, "reason": pkg["reason"]})
        for prof in effects.get("profiles", []):
            entry = profiles.setdefault(prof["name"], {"excludes": set(), "reasons": [], "rules": []})
            entry["excludes"].update(prof.get("excludes", []))
            entry["reasons"].append({"rule": rid, "reason": prof["reason"]})
            entry["rules"].append(rid)
        for warn in effects.get("warnings", []):
            plan["warnings"].append({"code": warn["code"], "message": warn["message"], "rule": rid})

    order = {r["id"]: i for i, r in enumerate(evaluation_order(rules))}

    def in_eval_order(ids):
        return sorted(set(ids), key=order.__getitem__)

    conflicts = []
    for name in sorted(set(required) & set(forbidden)):
        rids = [r for ids in required[name].values() for r in ids] + forbidden[name]
        conflicts.append({"kind": "package_required_and_forbidden", "subject": name, "rules": in_eval_order(rids)})
    for name in sorted(required):
        if len(required[name]) > 1:
            rids = [r for ids in required[name].values() for r in ids]
            conflicts.append({"kind": "package_source_mismatch", "subject": name, "rules": in_eval_order(rids)})
    for name in sorted(profiles):
        excluders = [p for p in sorted(profiles) if name in profiles[p]["excludes"]]
        if excluders:
            rids = list(profiles[name]["rules"])
            for p in excluders:
                rids.extend(profiles[p]["rules"])
            conflicts.append({"kind": "profile_exclusion", "subject": name, "rules": in_eval_order(rids)})
    plan["conflicts"] = sorted(conflicts, key=lambda c: (c["kind"], c["subject"]))

    plan["classifications"] = [
        {"name": name, "rules": entry["rules"], "targets": sorted(entry["targets"])}
        for name, entry in sorted(classifications.items())
    ]

    all_targets = ["cpu"] + [dev["id"] for dev in inv["devices"]]
    plan["unresolved"] = [
        {"target": t, "reason": "no_rule_matched"} for t in sorted(all_targets) if t not in covered
    ]

    if plan["conflicts"]:
        plan["status"] = "conflict"
        return plan

    def source_status(source):
        return "authorised" if source in authorised else "policy_review_required"

    packages = []
    sources = {}
    for name in sorted(set(required) | set(forbidden)):
        if name in required:
            (source,) = required[name].keys()
            packages.append({
                "name": name,
                "disposition": "require",
                "source": source,
                "source_status": source_status(source),
                "reasons": reasons[name],
            })
            sources.setdefault(source, []).append(name)
        else:
            packages.append({"name": name, "disposition": "forbid", "reasons": reasons[name]})
    plan["packages"] = packages
    plan["sources"] = [
        {"source": s, "status": source_status(s), "packages": sorted(pkgs)}
        for s, pkgs in sorted(sources.items())
    ]
    plan["profiles"] = []
    for name, entry in sorted(profiles.items()):
        item = {"name": name, "reasons": entry["reasons"]}
        if entry["excludes"]:
            item["excludes"] = sorted(entry["excludes"])
        plan["profiles"].append(item)

    review = any(s["status"] == "policy_review_required" for s in plan["sources"])
    plan["status"] = "partial" if plan["unresolved"] or review else "resolved"
    return plan
