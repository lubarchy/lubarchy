#!/usr/bin/python3
"""LUBARCHY M1-A hardware contract gate.

Validates the inventory/rule/plan v1 schemas and exercises the reference
resolver (tests/hwe_reference.py) against the synthetic fixtures under
tests/fixtures/hardware. Deterministic: no network, no root, no hardware
access, no system state; temporary files only in a private temporary
directory. Needs python3 and python3-jsonschema (Debian main).
"""

import sys

sys.dont_write_bytecode = True

import ast  # noqa: E402
import copy  # noqa: E402
import json  # noqa: E402
import pathlib  # noqa: E402
import random  # noqa: E402
import tempfile  # noqa: E402

import hwe_reference as ref  # noqa: E402

TESTS = pathlib.Path(__file__).resolve().parent
FIXTURES = TESTS / "fixtures" / "hardware"
INVENTORY_DIR = FIXTURES / "inventory"
INVALID_DIR = FIXTURES / "invalid"
RULES_DIR = FIXTURES / "rules"
CONFLICT_RULES_DIR = FIXTURES / "rules-conflict"
EXPECTED_DIR = FIXTURES / "expected"
PRODUCTION_RULES_DIR = ref.REPO_ROOT / "hardware" / "rules"

SCHEMA_IDS = {
    "inventory": ("urn:lubarchy:hardware:inventory:v1", ref.INVENTORY_SCHEMA_ID),
    "rule": ("urn:lubarchy:hardware:rule:v1", ref.RULE_SCHEMA_ID),
    "plan": ("urn:lubarchy:hardware:plan:v1", ref.PLAN_SCHEMA_ID),
}

# Valid fixtures and the plan status each must resolve to with the test rules.
FIXTURE_STATUS = {
    "01-intel-cpu-intel-gpu": "partial",
    "02-amd-cpu-amd-gpu": "resolved",
    "03-nvidia-discrete-gpu": "partial",
    "04-intel-nvidia-hybrid": "partial",
    "05-ethernet-wifi": "partial",
    "06-usb-bluetooth": "partial",
    "07-nvme-storage": "resolved",
    "08-unknown-pci-device": "partial",
    "09-qemu-kvm-guest": "resolved",
    "12-determinism-shuffled": "partial",
}

# Identifiers the default inventory must never carry.
FORBIDDEN_FIELDS = [
    "serial", "serial_number", "system_serial", "chassis_serial", "board_serial",
    "product_uuid", "uuid", "disk_serial", "nvme_serial", "wwn", "mac",
    "mac_address", "bluetooth_address", "bdaddr", "machine_id", "hostname",
    "username", "user", "ip_address", "ipv4", "ipv6", "ssid", "password",
    "token", "secret", "credentials",
]
# Generic containers that would allow an unrestricted property dump.
DUMP_FIELDS = ["udev", "properties", "uevent", "attributes", "sysfs", "environment", "raw", "extra"]
FORBIDDEN_TOKENS = {"serial", "uuid", "wwn", "mac", "bdaddr", "machine", "hostname",
                    "username", "user", "ip", "ipv4", "ipv6", "ssid", "password", "token",
                    "secret", "credentials", "properties", "udev", "uevent", "raw"}


class Failure(Exception):
    pass


def expect(cond, message):
    if not cond:
        raise Failure(message)


def expect_raises(exc, fn, message):
    try:
        fn()
    except exc:
        return
    raise Failure(message)


CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


def inventory(name):
    return ref.load_json(INVENTORY_DIR / f"{name}.json")


def test_rules():
    return ref.load_rule_set(RULES_DIR)


def plan_bytes(plan):
    return ref.canonical_json(plan)


def walk_schema(node, path=""):
    if isinstance(node, dict):
        yield path, node
        for key, value in node.items():
            yield from walk_schema(value, f"{path}/{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from walk_schema(value, f"{path}/{i}")


def property_names(schema):
    names = set()
    for _, node in walk_schema(schema):
        if isinstance(node.get("properties"), dict):
            names.update(node["properties"])
    return names


# ---------------------------------------------------------------------------
# Schemas and JSON


@check
def json_files_parse_strictly():
    files = sorted((ref.REPO_ROOT / "hardware").rglob("*.json")) + sorted(FIXTURES.rglob("*.json"))
    expect(files, "no JSON files found")
    for path in files:
        ref.load_json(path)
    return f"{len(files)} files: UTF-8, no duplicate keys, integers only"


@check
def schemas_meta_validate():
    for kind, (urn, ident) in SCHEMA_IDS.items():
        schema = ref.load_schema(ref.SCHEMA_FILES[kind])
        expect(schema.get("$schema") == "https://json-schema.org/draft/2020-12/schema",
               f"{kind}: $schema is not Draft 2020-12")
        expect(schema.get("$id") == urn, f"{kind}: unexpected $id {schema.get('$id')!r}")
        ref.jsonschema.Draft202012Validator.check_schema(schema)
        expect(schema["properties"]["schema"].get("const") == ident, f"{kind}: schema const is not {ident}")
        for path, node in walk_schema(schema):
            if isinstance(node.get("properties"), dict) and "type" in node:
                expect(node.get("additionalProperties") is False,
                       f"{kind}: object at {path or '/'} accepts undeclared fields")
    return "3 schemas valid under the Draft 2020-12 meta-schema; every declared object rejects undeclared fields"


@check
def inventory_vocabulary_excludes_private_identifiers():
    names = property_names(ref.load_schema(ref.SCHEMA_FILES["inventory"]))
    expect(not names & set(FORBIDDEN_FIELDS + DUMP_FIELDS), f"forbidden field declared: {names & set(FORBIDDEN_FIELDS)}")
    for name in names:
        expect(not set(name.split("_")) & FORBIDDEN_TOKENS, f"inventory field {name!r} looks privacy-sensitive")
    return f"{len(names)} declared inventory fields, none privacy-sensitive"


@check
def no_policy_in_inventory_schema():
    names = property_names(ref.load_schema(ref.SCHEMA_FILES["inventory"]))
    for word in ("package", "packages", "profile", "profiles", "source", "required", "microcode_package",
                 "firmware", "install", "command", "timestamp", "time", "collected_at"):
        expect(word not in names, f"inventory schema declares policy/time field {word!r}")
    return "inventory declares observations only (no package, profile, source or time fields)"


# ---------------------------------------------------------------------------
# Inventory validation


@check
def valid_fixtures_pass():
    v = ref.validator("inventory")
    names = sorted(p.stem for p in INVENTORY_DIR.glob("*.json"))
    expect(names == sorted(FIXTURE_STATUS), f"unexpected inventory fixture set: {names}")
    for name in names:
        errs = ref.inventory_errors(inventory(name), v)
        expect(not errs, f"{name}: {errs}")
    return f"{len(names)} valid inventory fixtures"


@check
def invalid_fixture_files_fail():
    cases = {
        "10-forbidden-field": ("/devices/1", "additionalProperties"),
        "10-uppercase-hex": ("/devices/1/vendor_id", "pattern"),
    }
    expect(sorted(p.stem for p in INVALID_DIR.glob("*.json")) == sorted(cases), "unexpected invalid fixture set")
    for name, err in cases.items():
        errs = ref.inventory_errors(ref.load_json(INVALID_DIR / f"{name}.json"))
        expect(errs == [err], f"{name}: expected {[err]}, got {errs}")
    return "10-forbidden-field (mac_address) and 10-uppercase-hex rejected"


def _locations(inv):
    """(label, container, json pointer) for every object level of an inventory."""
    pci_i = next(i for i, d in enumerate(inv["devices"]) if d["bus"] == "pci")
    usb_i = next(i for i, d in enumerate(inv["devices"]) if d["bus"] == "usb")
    return [
        ("top", lambda d: d, ""),
        ("system", lambda d: d["system"], "/system"),
        ("virtualization", lambda d: d["system"]["virtualization"], "/system/virtualization"),
        ("cpu", lambda d: d["cpu"], "/cpu"),
        ("pci-device", lambda d: d["devices"][pci_i], f"/devices/{pci_i}"),
        ("usb-device", lambda d: d["devices"][usb_i], f"/devices/{usb_i}"),
        ("usb-interface", lambda d: d["devices"][usb_i]["interfaces"][0], f"/devices/{usb_i}/interfaces/0"),
    ]


@check
def privacy_fields_rejected():
    base = inventory("06-usb-bluetooth")
    v = ref.validator("inventory")
    count = 0
    for field in FORBIDDEN_FIELDS + DUMP_FIELDS:
        for label, get, pointer in _locations(base):
            doc = copy.deepcopy(base)
            value = {"ID_SERIAL": "SYNTHETIC"} if field in DUMP_FIELDS else "SYNTHETIC-0000"
            get(doc)[field] = value
            errs = ref.inventory_errors(doc, v)
            expect((pointer, "additionalProperties") in errs,
                   f"{field!r} at {label} was not rejected: {errs}")
            count += 1
    return f"{count} injections rejected ({len(FORBIDDEN_FIELDS)} identifiers + {len(DUMP_FIELDS)} dump containers x 7 levels)"


def _mutate(base, fn):
    doc = copy.deepcopy(base)
    fn(doc)
    return doc


@check
def malformed_inventories_rejected():
    hybrid = inventory("04-intel-nvidia-hybrid")
    bt = inventory("06-usb-bluetooth")

    def setp(path, value):
        def fn(doc):
            node = doc
            for key in path[:-1]:
                node = node[key]
            node[path[-1]] = value
        return fn

    def delp(path):
        def fn(doc):
            node = doc
            for key in path[:-1]:
                node = node[key]
            del node[path[-1]]
        return fn

    cases = [
        ("missing schema", hybrid, delp(["schema"]), ("", "required")),
        ("future schema version", hybrid, setp(["schema"], "lubarchy.hardware.inventory/v2"), ("/schema", "const")),
        ("floating schema version", hybrid, setp(["schema"], "lubarchy.hardware.inventory/latest"), ("/schema", "const")),
        ("uppercase PCI vendor", hybrid, setp(["devices", 2, "vendor_id"], "10DE"), ("/devices/2/vendor_id", "pattern")),
        ("5-digit PCI device", hybrid, setp(["devices", 2, "device_id"], "0f005"), ("/devices/2/device_id", "pattern")),
        ("2-digit PCI vendor", hybrid, setp(["devices", 2, "vendor_id"], "de"), ("/devices/2/vendor_id", "pattern")),
        ("0x-prefixed PCI vendor", hybrid, setp(["devices", 2, "vendor_id"], "0x10de"), ("/devices/2/vendor_id", "pattern")),
        ("'unknown' PCI vendor", hybrid, setp(["devices", 2, "vendor_id"], "unknown"), ("/devices/2/vendor_id", "pattern")),
        ("4-digit PCI class", hybrid, setp(["devices", 2, "class_id"], "0302"), ("/devices/2/class_id", "pattern")),
        ("missing PCI class", hybrid, delp(["devices", 2, "class_id"]), ("/devices/2", "required")),
        ("lowercase modalias", hybrid, setp(["devices", 2, "modalias"], hybrid["devices"][2]["modalias"].lower()),
         ("/devices/2/modalias", "pattern")),
        ("uppercase USB product", bt, setp(["devices", 1, "product_id"], "F009"), ("/devices/1/product_id", "pattern")),
        ("uppercase USB interface class", bt, setp(["devices", 1, "interfaces", 0, "class"], "E0"),
         ("/devices/1/interfaces/0/class", "pattern")),
        ("USB root hub locator", bt, setp(["devices", 1, "locator"], "usb1"), ("/devices/1/locator", "pattern")),
        ("unknown bus", hybrid, setp(["devices", 0, "bus"], "platform"), ("/devices/0", "oneOf")),
        ("secure_boot outside enum", hybrid, setp(["system", "secure_boot"], "maybe"), ("/system/secure_boot", "enum")),
        ("secure_boot missing", hybrid, delp(["system", "secure_boot"]), ("/system", "required")),
        ("virtualization technology on bare metal", hybrid, setp(["system", "virtualization", "technology"], "kvm"),
         ("/system/virtualization", "not")),
        ("CPU family as string", hybrid, setp(["cpu", "family"], "6"), ("/cpu/family", "type")),
        ("negative CPU model", hybrid, setp(["cpu", "model"], -1), ("/cpu/model", "minimum")),
        ("microcode with leading zeros", hybrid, setp(["cpu", "microcode_revision"], "0x001f"),
         ("/cpu/microcode_revision", "pattern")),
        ("kernel release with space", hybrid, setp(["system", "kernel_release"], "6.12 custom"),
         ("/system/kernel_release", "pattern")),
        ("id does not match locator", hybrid, setp(["devices", 1, "id"], "pci:0000:00:02.1"),
         ("/devices/1/id", "id_matches_locator")),
        ("duplicate device id", hybrid, lambda d: d["devices"].append(copy.deepcopy(d["devices"][0])),
         ("/devices/3/id", "unique_device_id")),
        ("dangling parent", hybrid, setp(["devices", 2, "parent"], "pci:0000:00:1c.0"),
         ("/devices/2/parent", "parent_exists")),
        ("duplicate USB interface number", bt, setp(["devices", 1, "interfaces", 1, "number"], "00"),
         ("/devices/1/interfaces", "unique_interface_number")),
        ("empty module list", hybrid, setp(["devices", 1, "modules"], []), ("/devices/1/modules", "minItems")),
    ]
    v = ref.validator("inventory")
    for label, base, fn, err in cases:
        errs = ref.inventory_errors(_mutate(base, fn), v)
        expect(err in errs, f"{label}: expected {err}, got {errs}")
    strict = [
        ("duplicate key", '{"schema": "a", "schema": "b"}'),
        ("float", '{"family": 6.0}'),
        ("NaN", '{"family": NaN}'),
        ("Infinity", '{"family": Infinity}'),
    ]
    for label, text in strict:
        expect_raises(ref.ContractError, lambda t=text: ref.loads_strict(t), f"strict loader accepted {label}")
    return f"{len(cases)} malformed inventories and {len(strict)} non-strict JSON inputs rejected"


@check
def hex_identifier_normalization():
    good = [("0x10DE", 4, "10de"), ("10de", 4, "10de"), ("0x030000", 6, "030000"), ("0xE0", 2, "e0"),
            (" 0x8086\n", 4, "8086")]
    for raw, width, out in good:
        expect(ref.normalize_hex_id(raw, width) == out, f"normalize_hex_id({raw!r}) != {out}")
    for raw, width in [("0x10de0", 4), ("", 4), ("0x", 4), ("0xg000", 4), ("unknown", 4)]:
        expect_raises(ref.ContractError, lambda r=raw, w=width: ref.normalize_hex_id(r, w),
                      f"normalize_hex_id accepted {raw!r}")
    return "raw sysfs IDs normalize to fixed-width lowercase; oversize/non-hex rejected"


# ---------------------------------------------------------------------------
# Selector semantics


def _rule(rid, match, priority=0, **effects):
    effects = effects or {"classifications": ["test.match"]}
    return {"schema": ref.RULE_SCHEMA_ID, "id": rid, "description": "Synthetic.", "priority": priority,
            "match": match, "effects": effects}


def _matches(selector, inv):
    rule = _rule("test.sel", [selector])
    expect(not ref.rule_errors(rule), f"selector invalid: {ref.rule_errors(rule)}")
    return ref.rule_targets(rule, ref.normalize_inventory(inv))


@check
def selector_and_semantics():
    inv = inventory("04-intel-nvidia-hybrid")
    expect(_matches({"pci": {"vendor_id": ["10de"], "class_id": ["03"]}}, inv) == ["pci:0000:01:00.0"], "AND match")
    expect(_matches({"pci": {"vendor_id": ["10de"], "class_id": ["04"]}}, inv) == [], "AND must fail on class")
    expect(_matches({"pci": {"vendor_id": ["10de"], "device_id": ["f004"]}}, inv) == [], "AND must fail on device")
    expect(_matches({"cpu": {"vendor": ["GenuineIntel"], "family": [6], "model": [1]}}, inv) == [], "CPU AND")
    expect(_matches({"cpu": {"vendor": ["GenuineIntel"], "family": [6], "model": [250]}}, inv) == ["cpu"], "CPU AND")
    return "every field in one selector must match"


@check
def selector_list_or_semantics():
    inv = inventory("04-intel-nvidia-hybrid")
    got = _matches({"pci": {"vendor_id": ["8086", "10de"], "class_id": ["0300", "0302"]}}, inv)
    expect(got == ["pci:0000:00:02.0", "pci:0000:01:00.0"], f"list OR: {got}")
    expect(_matches({"cpu": {"vendor": ["AuthenticAMD", "GenuineIntel"]}}, inv) == ["cpu"], "CPU list OR")
    return "any value of one field's list may match"


@check
def rule_any_selector_semantics():
    inv = inventory("07-nvme-storage")
    rule = _rule("test.any", [{"pci": {"vendor_id": ["ffff"]}}, {"pci": {"modalias": ["pci:v*d*sv*sd*bc01sc08i02"]}}])
    expect(ref.rule_targets(rule, inv) == ["pci:0000:01:00.0"], "second selector must match")
    rule = _rule("test.none", [{"pci": {"vendor_id": ["ffff"]}}, {"usb": {"vendor_id": ["ffff"]}}])
    expect(ref.rule_targets(rule, inv) == [], "no selector matches")
    return "a rule matches when any selector matches"


@check
def missing_fields_never_match():
    hybrid = inventory("04-intel-nvidia-hybrid")
    # The PCI bridge has no subsystem IDs and no modalias.
    expect(_matches({"pci": {"class_id": ["0604"], "subsystem_vendor_id": ["8086"]}}, hybrid) == [],
           "absent subsystem_vendor_id matched")
    expect(_matches({"pci": {"class_id": ["0604"], "modalias": ["*"]}}, hybrid) == [],
           "absent modalias matched '*'")
    bt = inventory("06-usb-bluetooth")
    no_class = _mutate(bt, lambda d: [d["devices"][1].pop(k) for k in ("device_class", "device_subclass", "device_protocol")])
    expect(_matches({"usb": {"device_class": ["e0"]}}, no_class) == [], "absent USB device_class matched")
    no_itf = _mutate(bt, lambda d: d["devices"][1].pop("interfaces"))
    expect(_matches({"usb": {"interface_class": ["e0"]}}, no_itf) == [], "absent interfaces matched")
    expect(_matches({"usb": {"modalias": ["*"]}}, no_itf) == [], "absent interface modalias matched '*'")
    unknown_cpu = inventory("08-unknown-pci-device")
    expect(_matches({"cpu": {"family": [0]}}, unknown_cpu) == [], "absent CPU family matched")
    no_fw = _mutate(hybrid, lambda d: d["system"].pop("boot_firmware"))
    expect(_matches({"platform": {"boot_firmware": ["uefi", "bios"]}}, no_fw) == [], "absent boot_firmware matched")
    no_virt = _mutate(hybrid, lambda d: d["system"].pop("virtualization"))
    expect(_matches({"platform": {"virtualization_kind": ["none"]}}, no_virt) == [],
           "absent virtualization matched kind 'none'")
    bare = inventory("01-intel-cpu-intel-gpu")
    expect(_matches({"platform": {"virtualization_technology": ["kvm"]}}, bare) == [], "absent technology matched")
    return "absent observed fields satisfy no condition (including '*' globs and 'none')"


@check
def usb_interface_conditions_bind_to_one_interface():
    bt = inventory("06-usb-bluetooth")

    def split(d):
        itfs = d["devices"][1]["interfaces"]
        itfs[0].update({"class": "e0", "subclass": "01", "protocol": "00"})
        itfs[1].update({"class": "ff", "subclass": "01", "protocol": "01"})
        for itf in itfs:
            itf.pop("modalias")
    mixed = _mutate(bt, split)
    expect(not ref.inventory_errors(mixed), "mutated USB fixture must stay valid")
    expect(_matches({"usb": {"interface_class": ["e0"], "interface_protocol": ["01"]}}, mixed) == [],
           "conditions satisfied only by different interfaces must not match")
    expect(_matches({"usb": {"interface_class": ["e0"], "interface_protocol": ["00"]}}, mixed) == ["usb:1-10"],
           "conditions satisfied by one interface must match")
    expect(_matches({"usb": {"vendor_id": ["8087"], "interface_class": ["ff"]}}, mixed) == ["usb:1-10"],
           "device and interface conditions combine")
    return "interface_* and modalias conditions must hold on the same interface"


@check
def glob_semantics_are_shell_style():
    inv = inventory("04-intel-nvidia-hybrid")
    nv = ["pci:0000:01:00.0"]
    cases = [
        ("pci:v000010DEd*", nv),
        ("pci:v000010ded*", []),            # case-sensitive
        ("pci:v000010DEd0000F00?sv*", nv),  # '?' is one character
        ("pci:v000010DEd0000F00[45]sv*", nv),
        ("pci:v000010DEd0000F00[!5]sv*", []),
        ("pci:v.*", []),                    # not a regular expression
        ("pci:v000010DE", []),              # anchored: whole string must match
    ]
    for pattern, want in cases:
        got = _matches({"pci": {"modalias": [pattern]}}, inv)
        expect(got == want, f"glob {pattern!r}: {got} != {want}")
    return f"{len(cases)} glob cases: anchored, case-sensitive, '*', '?', '[...]', no regex"


@check
def class_id_prefix_semantics():
    inv = inventory("04-intel-nvidia-hybrid")
    nv = ["pci:0000:01:00.0"]
    for prefix, want in [("03", ["pci:0000:00:02.0", "pci:0000:01:00.0"]), ("0302", nv), ("030200", nv),
                         ("0300", ["pci:0000:00:02.0"]), ("030201", []), ("02", [])]:
        got = _matches({"pci": {"class_id": [prefix]}}, inv)
        expect(got == want, f"class_id {prefix}: {got}")
    return "2/4/6-digit class_id selects base class / subclass / full code"


@check
def snapshot_locator_is_not_identity():
    sel = ref.load_schema(ref.SCHEMA_FILES["rule"])["$defs"]
    for scope in ("pciSelector", "usbSelector"):
        props = set(sel[scope]["properties"])
        expect(not props & {"id", "locator", "parent"}, f"{scope} can select on snapshot locators")
    rules = test_rules()
    base = inventory("01-intel-cpu-intel-gpu")

    def relocate(d):
        dev = d["devices"][0]
        dev["id"], dev["locator"] = "pci:0000:00:04.0", "0000:00:04.0"
    moved = _mutate(base, relocate)
    a, b = ref.resolve(base, rules), ref.resolve(moved, rules)
    expect(a["inventory_digest"] != b["inventory_digest"], "different snapshots must have different digests")
    def strip(p):
        return [c["name"] for c in p["classifications"]], p["packages"], p["profiles"], p["sources"], p["status"]
    expect(strip(a) == strip(b), "relocated device must resolve identically")
    return "selectors cannot name locators; moving a device changes the snapshot digest but not the resolution"


# ---------------------------------------------------------------------------
# Rules


@check
def rules_are_declarative_only():
    rules = test_rules()
    base = next(r for r in rules if r["id"] == "test.cpu.intel")

    def at(path, key, value):
        def fn(r):
            node = r
            for k in path:
                node = node[k]
            node[key] = value
        return fn

    pkg_rule = next(r for r in rules if "packages" in r["effects"])
    cases = [
        ("top-level command", base, at([], "command", "rm -rf /")),
        ("top-level exec", base, at([], "exec", ["/bin/sh", "-c", "true"])),
        ("top-level script", base, at([], "script", "true")),
        ("effects commands", base, at(["effects"], "commands", ["apt-get install x"])),
        ("effects run", base, at(["effects"], "run", "true")),
        ("effects apt_sources", base, at(["effects"], "apt_sources", ["deb http://example.invalid/ x main"])),
        ("package install_command", pkg_rule, at(["effects", "packages", 0], "install_command", "apt-get install x")),
        ("package postinst", pkg_rule, at(["effects", "packages", 0], "postinst", "true")),
        ("package shell", pkg_rule, at(["effects", "packages", 0], "shell", "true")),
        ("selector regex field", base, at(["match", 0, "cpu"], "regex", ".*")),
        ("unknown selector scope", base, at(["match", 0], "dmi", {"product_name": ["x"]})),
        ("source unstable", pkg_rule, at(["effects", "packages", 0], "source", "unstable")),
        ("source testing", pkg_rule, at(["effects", "packages", 0], "source", "testing")),
        ("source experimental", pkg_rule, at(["effects", "packages", 0], "source", "experimental")),
        ("source as URL", pkg_rule, at(["effects", "packages", 0], "source", "http://example.invalid/debian")),
        ("forbid with source", pkg_rule, at(["effects", "packages", 0], "disposition", "forbid")),
        ("priority as string", base, at([], "priority", "high")),
        ("priority out of range", base, at([], "priority", 1001)),
        ("multi-line description", base, at([], "description", "line one\nline two")),
        ("empty match", base, at([], "match", [])),
        ("empty selector", base, at([], "match", [{}])),
        ("two scopes in one selector", base, at([], "match", [{"cpu": {"vendor": ["x"]}, "platform": {"architecture": ["amd64"]}}])),
        ("empty scope conditions", base, at([], "match", [{"pci": {}}])),
        ("uppercase hex in selector", base, at([], "match", [{"pci": {"vendor_id": ["10DE"]}}])),
        ("odd-length class prefix", base, at([], "match", [{"pci": {"class_id": ["030"]}}])),
        ("empty effects", base, at([], "effects", {})),
        ("floating rule schema", base, at([], "schema", "lubarchy.hardware.rule/latest")),
        ("uppercase rule id", base, at([], "id", "Test.CPU")),
    ]
    v = ref.validator("rule")
    for label, rule, fn in cases:
        errs = ref.rule_errors(_mutate(rule, fn), v)
        expect(errs, f"rule schema accepted: {label}")

    def require_without_source(r):
        del r["effects"]["packages"][0]["source"]
    expect(ref.rule_errors(_mutate(pkg_rule, require_without_source), v), "require without source accepted")

    def dup_pkg(r):
        r["effects"]["packages"].append(copy.deepcopy(r["effects"]["packages"][0]))
    expect(("/effects/packages", "unique_package_name") in ref.rule_errors(_mutate(pkg_rule, dup_pkg), v),
           "duplicate package within a rule accepted")
    expect_raises(ref.ContractError, lambda: ref.check_rule_set(rules + [copy.deepcopy(rules[0])]),
                  "duplicate rule id accepted")
    with tempfile.TemporaryDirectory() as tmp:
        bad = pathlib.Path(tmp) / "test.wrong-name.json"
        bad.write_text(json.dumps(base), encoding="utf-8")
        expect_raises(ref.ContractError, lambda: ref.load_rule_set(tmp), "rule file name mismatch accepted")
    return f"{len(cases) + 4} non-declarative or malformed rules rejected"


@check
def test_rules_isolated_from_production():
    rules = test_rules()
    expect(len(rules) == 14, f"expected 14 test rules, got {len(rules)}")
    for rule in rules + ref.load_rule_set(CONFLICT_RULES_DIR):
        expect(rule["id"].startswith("test."), f"{rule['id']}: test rule id must start with 'test.'")
        expect(rule["description"].startswith("TEST-ONLY"), f"{rule['id']}: description must say TEST-ONLY")
        for pkg in rule["effects"].get("packages", []):
            expect(pkg["name"].startswith("lubarchy-test-"), f"{rule['id']}: non-synthetic package {pkg['name']}")
    production = sorted(p.name for p in PRODUCTION_RULES_DIR.iterdir())
    expect(production == ["README.md"], f"hardware/rules must hold no rules in M1-A: {production}")
    return "14 + 2 test rules are TEST-ONLY with synthetic packages; hardware/rules has no rules"


# ---------------------------------------------------------------------------
# Resolution


@check
def unknown_hardware_yields_no_speculative_packages():
    rules = test_rules()
    plan = ref.resolve(inventory("08-unknown-pci-device"), rules)
    expect(plan["status"] == "partial", f"status {plan['status']}")
    for key in ("matched_rules", "classifications", "packages", "profiles", "sources", "warnings", "conflicts"):
        expect(plan[key] == [], f"unknown hardware produced {key}: {plan[key]}")
    expect(plan["unresolved"] == [{"target": "cpu", "reason": "no_rule_matched"},
                                  {"target": "pci:0000:05:00.0", "reason": "no_rule_matched"}],
           f"unresolved: {plan['unresolved']}")
    known = inventory("02-amd-cpu-amd-gpu")
    plus = _mutate(known, lambda d: d["devices"].append(copy.deepcopy(inventory("08-unknown-pci-device")["devices"][0])))
    a, b = ref.resolve(known, rules), ref.resolve(plus, rules)
    expect(a["status"] == "resolved" and b["status"] == "partial", "unknown device must make a resolved plan partial")
    for key in ("matched_rules", "classifications", "packages", "profiles", "sources", "warnings"):
        expect(a[key] == b[key], f"adding an unknown device changed {key}")
    expect(b["unresolved"] == [{"target": "pci:0000:05:00.0", "reason": "no_rule_matched"}], "unknown device not reported")
    return "unknown CPU/device stay visible as unresolved and add no package, profile or source"


@check
def priority_orders_but_never_overrides():
    rules = test_rules()
    inv = inventory("04-intel-nvidia-hybrid")
    plan = ref.resolve(inv, rules)
    order = [(m["priority"], m["id"]) for m in plan["matched_rules"]]
    expect(order == sorted(order, key=lambda x: (-x[0], x[1])), f"matched_rules not in evaluation order: {order}")
    flipped = [dict(r, priority=-r["priority"]) for r in rules]
    other = ref.resolve(inv, flipped)
    other_order = [(m["priority"], m["id"]) for m in other["matched_rules"]]
    expect(other_order == sorted(other_order, key=lambda x: (-x[0], x[1])) and other_order != order,
           "negated priorities must reorder the matched rules")

    def summary(p):
        return (p["status"],
                [(c["name"], c["targets"], sorted(c["rules"])) for c in p["classifications"]],
                [(x["name"], x["disposition"], x.get("source"), x.get("source_status"),
                  sorted(r["rule"] for r in x["reasons"])) for x in p["packages"]],
                [(x["name"], sorted(r["rule"] for r in x["reasons"])) for x in p["profiles"]],
                p["sources"], p["unresolved"], sorted(map(json.dumps, p["warnings"])))
    expect(summary(plan) == summary(other), "priority changed resolution content")
    tie = [_rule("test.b", [{"cpu": {"vendor": ["GenuineIntel"]}}]), _rule("test.a", [{"cpu": {"vendor": ["GenuineIntel"]}}])]
    expect([m["id"] for m in ref.resolve(inv, tie)["matched_rules"]] == ["test.a", "test.b"], "tie not broken by id")
    return "evaluation order = priority desc, id asc; negating every priority leaves the resolution unchanged"


@check
def conflicts_fail_closed():
    conflict_rules = ref.load_rule_set(CONFLICT_RULES_DIR)
    inv = inventory("03-nvidia-discrete-gpu")
    plan = ref.resolve(inv, conflict_rules)
    expect(plan["status"] == "conflict", f"status {plan['status']}")
    expect(plan["packages"] == [] and plan["profiles"] == [] and plan["sources"] == [], "conflict plan proposes effects")
    expect(plan["conflicts"] == [{"kind": "package_required_and_forbidden", "subject": "lubarchy-test-firmware-d",
                                  "rules": ["test.conflict.forbid-d", "test.conflict.require-d"]}],
           f"conflicts: {plan['conflicts']}")
    for prio in (-1000, 0, 1000):
        swapped = [dict(r, priority=prio) if r["id"].endswith("forbid-d") else r for r in conflict_rules]
        expect(ref.resolve(inv, swapped)["status"] == "conflict", f"priority {prio} let one rule win")

    nv = [{"pci": {"vendor_id": ["10de"]}}]
    pkg = lambda src: {"packages": [{"name": "lubarchy-test-firmware-x", "disposition": "require", "source": src,
                                     "reason": "Synthetic."}]}
    mismatch = ref.resolve(inv, [_rule("test.src-main", nv, 5, **pkg("main")),
                                 _rule("test.src-backports", nv, 1, **pkg("backports"))])
    expect(mismatch["status"] == "conflict" and mismatch["conflicts"][0]["kind"] == "package_source_mismatch"
           and mismatch["packages"] == [], f"source mismatch: {mismatch['conflicts']}")
    prof = lambda name, excl=None: {"profiles": [dict({"name": name, "reason": "Synthetic."}, **({"excludes": excl} if excl else {}))]}
    excl = ref.resolve(inv, [_rule("test.prof-a", nv, 1, **prof("test-profile-a", ["test-profile-b"])),
                             _rule("test.prof-b", nv, 1, **prof("test-profile-b"))])
    expect(excl["status"] == "conflict" and excl["conflicts"] == [
        {"kind": "profile_exclusion", "subject": "test-profile-b", "rules": ["test.prof-a", "test.prof-b"]}]
        and excl["profiles"] == [], f"profile exclusion: {excl['conflicts']}")
    same = ref.resolve(inv, [_rule("test.same-1", nv, 1, **pkg("main")), _rule("test.same-2", nv, 2, **pkg("main"))])
    expect(same["status"] != "conflict" and len(same["packages"]) == 1 and len(same["packages"][0]["reasons"]) == 2,
           "identical requirements from two rules must merge, not conflict")
    return "required+forbidden, source mismatch and profile exclusion all fail closed at any priority; equal requirements merge"


@check
def source_requirement_is_represented_not_enabled():
    rules = test_rules()
    inv = inventory("03-nvidia-discrete-gpu")
    plan = ref.resolve(inv, rules)
    expect(plan["source_policy"] == {"authorised": ["main"]}, f"policy {plan['source_policy']}")
    expect(plan["sources"] == [{"source": "non-free-firmware", "status": "policy_review_required",
                                "packages": ["lubarchy-test-firmware-a", "lubarchy-test-firmware-d"]}],
           f"sources {plan['sources']}")
    expect(plan["status"] == "partial", "non-authorised source must keep the plan partial")
    for pkg in plan["packages"]:
        expect(pkg["source_status"] == "policy_review_required", f"{pkg['name']} not flagged")
    widened = ref.resolve(inv, rules, authorised_sources=("main", "non-free-firmware"))
    expect(widened["sources"][0]["status"] == "authorised", "policy input not honoured")
    # The reference implementation cannot touch the system: no process, network or OS modules.
    tree = ast.parse((TESTS / "hwe_reference.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    expect(imported == {"fnmatch", "hashlib", "json", "pathlib", "jsonschema"}, f"unexpected imports: {imported}")
    writes = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "open"
              and not (len(n.args) > 1 and isinstance(n.args[1], ast.Constant) and n.args[1].value == "rb")]
    expect(not writes, "reference implementation opens files for anything but reading")
    return "non-free-firmware reported as policy_review_required under a main-only policy; resolver has no system access"


@check
def plans_validate_and_reject_side_effects():
    rules = test_rules()
    v = ref.validator("plan")
    plans = {name: ref.resolve(inventory(name), rules) for name in FIXTURE_STATUS}
    plans["11-conflict"] = ref.resolve(inventory("03-nvidia-discrete-gpu"), ref.load_rule_set(CONFLICT_RULES_DIR))
    plans["10-forbidden-field"] = ref.resolve(ref.load_json(INVALID_DIR / "10-forbidden-field.json"), rules)
    for name, plan in plans.items():
        errs = sorted(e.message for e in v.iter_errors(plan))
        expect(not errs, f"{name}: plan invalid: {errs}")
        want = FIXTURE_STATUS.get(name, {"11-conflict": "conflict", "10-forbidden-field": "invalid"}.get(name))
        expect(plan["status"] == want, f"{name}: status {plan['status']} != {want}")
    invalid = plans["10-forbidden-field"]
    expect(invalid["errors"] == [{"path": "/devices/1", "check": "additionalProperties"}], f"{invalid['errors']}")
    expect("02:00:00:00:00:01" not in plan_bytes(invalid).decode(), "invalid plan echoes the rejected value")

    resolved = plans["02-amd-cpu-amd-gpu"]
    partial = plans["03-nvidia-discrete-gpu"]
    negatives = [
        ("commands field", resolved, lambda p: p.update(commands=["apt-get install x"])),
        ("package command", resolved, lambda p: p["packages"][0].update(command="apt-get install x")),
        ("transaction status", resolved, lambda p: p.update(status="installed")),
        ("apt source line", resolved, lambda p: p["sources"][0].update(line="deb http://example.invalid/ x main")),
        ("resolved with policy review", partial, lambda p: p.update(status="resolved")),
        ("conflict with packages", resolved, lambda p: p.update(status="conflict", conflicts=[
            {"kind": "package_source_mismatch", "subject": "x1", "rules": ["test.a"]}])),
        ("resolved with conflicts", resolved, lambda p: p.update(conflicts=[
            {"kind": "package_source_mismatch", "subject": "x1", "rules": ["test.a"]}])),
        ("resolved with unresolved", resolved, lambda p: p.update(unresolved=[{"target": "cpu", "reason": "no_rule_matched"}])),
        ("invalid with digest", invalid, lambda p: p.update(inventory_digest=resolved["inventory_digest"])),
        ("valid status without digest", resolved, lambda p: p.pop("inventory_digest")),
        ("require without source_status", resolved, lambda p: p["packages"][0].pop("source_status")),
        ("floating plan schema", resolved, lambda p: p.update(schema="lubarchy.hardware.plan/latest")),
        ("timestamp", resolved, lambda p: p.update(generated_at="2026-10-12T00:00:00Z")),
    ]
    for label, plan, fn in negatives:
        expect(list(v.iter_errors(_mutate(plan, fn))), f"plan schema accepted: {label}")
    return f"{len(plans)} plans valid; {len(negatives)} plans with side effects, commands or inconsistent status rejected"


# ---------------------------------------------------------------------------
# Determinism


@check
def canonical_json_definition():
    expect(ref.canonical_json({"b": 1, "a": [2, 1], "c": {"z": True, "y": None}})
           == b'{"a":[2,1],"b":1,"c":{"y":null,"z":true}}', "key order / whitespace")
    expect(ref.canonical_json({"m": "Café \"q\" \\ \n"}) == '{"m":"Café \\"q\\" \\\\ \\n"}'.encode("utf-8"),
           "string escaping / UTF-8")
    expect(ref.sha256_digest(ref.canonical_json({"a": 1}))
           == "sha256:015abd7f5cc57a2dd94b7590f04ad8084273905ee33ec5cebeae62276a97f862", "known-answer digest")
    expect_raises(ref.ContractError, lambda: ref.canonical_json({"a": 1.5}), "float accepted")
    return "sorted keys, no whitespace, UTF-8, integers only; known-answer SHA-256 matches"


def _shuffle(doc, rnd):
    """Equivalent inventory with shuffled arrays and object key order."""
    def keys(o):
        if isinstance(o, dict):
            items = list(o.items())
            rnd.shuffle(items)
            return {k: keys(v) for k, v in items}
        if isinstance(o, list):
            return [keys(v) for v in o]
        return o
    d = copy.deepcopy(doc)
    rnd.shuffle(d["devices"])
    for dev in d["devices"]:
        if "modules" in dev:
            rnd.shuffle(dev["modules"])
        if "interfaces" in dev:
            rnd.shuffle(dev["interfaces"])
    return keys(d)


@check
def inventory_digest_is_deterministic():
    lines = []
    for name in sorted(FIXTURE_STATUS):
        inv = inventory(name)
        digest = ref.inventory_digest(inv)
        expect(digest == ref.inventory_digest(copy.deepcopy(inv)), f"{name}: digest not repeatable")
        expect(digest == ref.inventory_digest(ref.normalize_inventory(inv)), f"{name}: normalization not idempotent")
        rnd = random.Random(f"lubarchy-m1a-{name}")
        for _ in range(25):
            expect(ref.inventory_digest(_shuffle(inv, rnd)) == digest, f"{name}: shuffled digest differs")
        lines.append(f"    {name}: {digest}")
    shuffled = (INVENTORY_DIR / "12-determinism-shuffled.json").read_bytes()
    original = (INVENTORY_DIR / "06-usb-bluetooth.json").read_bytes()
    expect(shuffled != original, "shuffled fixture must differ byte-wise from its original")
    expect(ref.inventory_digest(ref.loads_strict(shuffled.decode())) == ref.inventory_digest(ref.loads_strict(original.decode())),
           "12-determinism-shuffled digest differs from 06-usb-bluetooth")
    print("\n".join(lines))
    return f"{len(FIXTURE_STATUS)} fixtures x 25 seeded shuffles; 12-determinism-shuffled == 06-usb-bluetooth"


@check
def plans_match_expected_bytes():
    rules = test_rules()
    cases = {name: (inventory(name), rules) for name in FIXTURE_STATUS}
    cases["11-conflict"] = (inventory("03-nvidia-discrete-gpu"), ref.load_rule_set(CONFLICT_RULES_DIR))
    cases["10-forbidden-field"] = (ref.load_json(INVALID_DIR / "10-forbidden-field.json"), rules)
    lines = []
    for name, (inv, rule_set) in sorted(cases.items()):
        expected_name = "06-usb-bluetooth" if name == "12-determinism-shuffled" else name
        expected = ref.canonical_json(ref.load_json(EXPECTED_DIR / f"{expected_name}.plan.json"))
        got = plan_bytes(ref.resolve(inv, rule_set))
        expect(got == expected, f"{name}: plan differs from expected/{expected_name}.plan.json")
        lines.append(f"    {name}: plan {ref.sha256_digest(got)}")
    expect(sorted(p.name for p in EXPECTED_DIR.glob("*.json"))
           == sorted(f"{n}.plan.json" for n in cases if n != "12-determinism-shuffled"), "unexpected expected-plan set")
    print("\n".join(lines))
    return f"{len(cases)} plans byte-identical to their expected canonical JSON"


@check
def reordered_inputs_give_identical_plan_bytes():
    rules = test_rules()
    for name in sorted(FIXTURE_STATUS):
        inv = inventory(name)
        want = plan_bytes(ref.resolve(inv, rules))
        rnd = random.Random(f"lubarchy-m1a-plan-{name}")
        for _ in range(10):
            shuffled_rules = [_shuffle_keys(r, rnd) for r in rules]
            rnd.shuffle(shuffled_rules)
            got = plan_bytes(ref.resolve(_shuffle(inv, rnd), shuffled_rules))
            expect(got == want, f"{name}: reordered inputs changed the plan bytes")
    a = plan_bytes(ref.resolve(inventory("06-usb-bluetooth"), rules))
    b = plan_bytes(ref.resolve(inventory("12-determinism-shuffled"), list(reversed(rules))))
    expect(a == b, "12-determinism-shuffled plan differs from 06-usb-bluetooth")
    return f"{len(FIXTURE_STATUS)} fixtures x 10 shuffles of inventory and rule order: identical plan bytes"


def _shuffle_keys(o, rnd):
    if isinstance(o, dict):
        items = list(o.items())
        rnd.shuffle(items)
        return {k: _shuffle_keys(v, rnd) for k, v in items}
    if isinstance(o, list):
        return [_shuffle_keys(v, rnd) for v in o]
    return o


@check
def hybrid_gpu_topology_is_represented():
    inv = ref.normalize_inventory(inventory("04-intel-nvidia-hybrid"))
    gpus = [d for d in inv["devices"] if d["bus"] == "pci" and d["class_id"].startswith("03")]
    expect(sorted(d["vendor_id"] for d in gpus) == ["10de", "8086"], "hybrid fixture needs Intel + NVIDIA GPUs")
    nvidia = next(d for d in gpus if d["vendor_id"] == "10de")
    expect(nvidia["class_id"] == "030200" and nvidia["parent"] == "pci:0000:00:01.0", "dGPU behind a bridge as 3D controller")
    plan = ref.resolve(inventory("04-intel-nvidia-hybrid"), test_rules())
    cls = {c["name"]: c["targets"] for c in plan["classifications"]}
    expect(cls.get("gpu.intel") == ["pci:0000:00:02.0"] and cls.get("gpu.nvidia") == ["pci:0000:01:00.0"],
           f"classifications {cls}")
    expect({p["name"] for p in plan["profiles"]} == {"test-graphics-nvidia", "test-graphics-open"}, "profiles")
    # Rule v1 selectors match one target each; a cross-device 'gpu.hybrid'
    # classification is not expressible in v1 (see docs/hardware-enablement.md).
    expect("gpu.hybrid" not in cls, "gpu.hybrid must not be invented by the resolver")
    return "Intel iGPU + NVIDIA 3D controller behind a bridge; both classified, no invented gpu.hybrid"


def main():
    sys.stdout.reconfigure(line_buffering=True)
    print(f"python {sys.version.split()[0]}, jsonschema {_jsonschema_version()}")
    failures = 0
    for fn in CHECKS:
        try:
            detail = fn()
        except Exception as e:  # noqa: BLE001 - every failure is reported
            failures += 1
            print(f"FAIL: {fn.__name__}: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            continue
        print(f"ok: {fn.__name__}" + (f": {detail}" if detail else ""))
    if failures:
        print(f"FAIL: hardware contracts ({failures} of {len(CHECKS)} checks failed)", file=sys.stderr)
        return 1
    print(f"PASS: hardware contracts ({len(CHECKS)} checks)")
    return 0


def _jsonschema_version():
    from importlib.metadata import version
    return version("jsonschema")


if __name__ == "__main__":
    sys.exit(main())
