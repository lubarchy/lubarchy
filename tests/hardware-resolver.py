#!/usr/bin/python3
"""LUBARCHY M1-C hardware resolver gate (synthetic only).

Tests the production resolver (hardware/resolver.py, hardware/resolve.py)
and the production classification rules (hardware/rules/):

- parity with the independent M1-A reference resolver (tests/hwe_reference.py);
- the production rule trust boundary and the classification-only policy;
- every production classification on synthetic inventories;
- the CLI contract and exit codes;
- source-level side-effect boundaries;
- synthetic probe -> resolver integration.

No host hardware is read, no network is used and nothing needs root.
Temporary files live only in a private temporary directory.
"""

import sys

sys.dont_write_bytecode = True

import ast  # noqa: E402
import contextlib  # noqa: E402
import copy  # noqa: E402
import importlib.util  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import pathlib  # noqa: E402
import random  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402

import hwe_reference as ref  # noqa: E402

TESTS = pathlib.Path(__file__).resolve().parent
HARDWARE = ref.REPO_ROOT / "hardware"
FIXTURES = TESTS / "fixtures" / "hardware"
PRODUCTION_RULES = HARDWARE / "rules"

sys.path.insert(0, str(HARDWARE))
import resolver  # noqa: E402
import resolve  # noqa: E402

sys.path.remove(str(HARDWARE))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe_tests = _load("lubarchy_probe_tests", TESTS / "hardware-probe.py")
probe = probe_tests.probe

EXPECTED_PRODUCTION_RULES = {
    "class.cpu.intel": "cpu.intel",
    "class.cpu.amd": "cpu.amd",
    "class.gpu.intel": "gpu.intel",
    "class.gpu.amd": "gpu.amd",
    "class.gpu.nvidia": "gpu.nvidia",
    "class.network.ethernet": "network.ethernet",
    "class.network.other": "network.other",
    "class.storage.nvme": "storage.nvme",
    "class.storage.sata": "storage.sata",
    "class.platform.vm": "platform.vm",
    "class.platform.container": "platform.container",
    "class.platform.bare-metal": "platform.bare-metal",
}
# M1-D package mappings: the complete production package requirement set.
EXPECTED_PACKAGE_RULES = {
    "package.cpu.intel-microcode": ("GenuineIntel", "intel-microcode"),
    "package.cpu.amd64-microcode": ("AuthenticAMD", "amd64-microcode"),
}
# M1-E device firmware rules (generated from qualified kernel module aliases;
# drift is checked by tests/hardware-firmware.py).
FIRMWARE_PACKAGE_RULES = {
    "package.firmware.amd-graphics": "firmware-amd-graphics",
    "package.firmware.brcm80211": "firmware-brcm80211",
    "package.firmware.intel-graphics": "firmware-intel-graphics",
    "package.firmware.iwlwifi": "firmware-iwlwifi",
}
PRODUCTION_PACKAGES = {"intel-microcode", "amd64-microcode", *FIRMWARE_PACKAGE_RULES.values()}
# Product source policy (ADR-0001). Stated here independently of the
# production constant and injected explicitly into the M1-A reference, which
# keeps accepting arbitrary policies.
PRODUCT_SOURCE_POLICY = ("main", "non-free-firmware")


class Failure(Exception):
    pass


def expect(cond, message):
    if not cond:
        raise Failure(message)


def expect_error(fn, message, code=None):
    try:
        fn()
    except resolver.ResolverError as e:
        expect(code is None or e.code == code, f"{message}: code {e.code}, want {code}")
        return e
    raise Failure(f"{message}: accepted")


CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


@contextlib.contextmanager
def scratch():
    d = tempfile.mkdtemp(prefix="lubarchy-resolver-test-")
    try:
        yield pathlib.Path(d)
    finally:
        shutil.rmtree(d)


def fixture(name):
    folder = "invalid" if name.startswith("10-") else "inventory"
    return ref.load_json(FIXTURES / folder / f"{name}.json")


def production_rules():
    return resolver.load_production_rules()


def test_rules():
    return resolver.load_rule_directory(str(FIXTURES / "rules"), production_policy=False)


def conflict_rules():
    return resolver.load_rule_directory(str(FIXTURES / "rules-conflict"), production_policy=False)


def plan_bytes(plan):
    return resolver.canonical_json(plan)


def classes(plan):
    return {c["name"]: c["targets"] for c in plan["classifications"]}


def rule(rid, match, priority=0, **effects):
    return {"schema": ref.RULE_SCHEMA_ID, "id": rid, "description": "Synthetic.", "priority": priority,
            "match": match, "effects": effects or {"classifications": ["test.match"]}}


def pci(loc, ven, dev, cls, **extra):
    d = {"id": f"pci:{loc}", "bus": "pci", "locator": loc, "vendor_id": ven, "device_id": dev, "class_id": cls}
    d.update(extra)
    return d


def inventory(devices=(), vendor="GenuineIntel", virt=("none", None), firmware="uefi"):
    system = {"architecture": "amd64", "kernel_release": "6.12.0-synthetic-amd64", "secure_boot": "disabled"}
    if firmware:
        system["boot_firmware"] = firmware
    if virt is not None:
        kind, tech = virt
        system["virtualization"] = {"kind": kind} if tech is None else {"kind": kind, "technology": tech}
    return {"schema": ref.INVENTORY_SCHEMA_ID, "system": system,
            "cpu": {"architecture": "x86_64", "vendor": vendor}, "devices": list(devices)}


def produce(inv, rules=None):
    plan = resolver.resolve(inv, production_rules() if rules is None else rules)
    resolver.check_plan(plan)
    return plan


# ---------------------------------------------------------------------------
# Parity with the independent M1-A reference


def _parity_cases():
    fixtures = sorted(p.stem for p in (FIXTURES / "inventory").glob("*.json")) + \
        sorted(p.stem for p in (FIXTURES / "invalid").glob("*.json"))
    sets = {"test": test_rules(), "conflict": conflict_rules(), "production": production_rules()}
    for name in fixtures:
        for set_name, rules in sets.items():
            yield f"{name} x {set_name}", fixture(name), rules


@check
def production_reference_parity():
    n = 0
    for label, inv, rules in _parity_cases():
        mine = plan_bytes(resolver.resolve(inv, rules))
        theirs = ref.canonical_json(ref.resolve(inv, rules, authorised_sources=PRODUCT_SOURCE_POLICY))
        expect(mine == theirs, f"{label}: production plan differs from the M1-A reference")
        n += 1
    rnd = random.Random("lubarchy-m1c-parity")
    base = [fixture(f) for f in ("04-intel-nvidia-hybrid", "06-usb-bluetooth", "09-qemu-kvm-guest")]
    all_rules = test_rules()
    for _ in range(60):
        inv = copy.deepcopy(rnd.choice(base))
        rnd.shuffle(inv["devices"])
        subset = rnd.sample(all_rules, rnd.randint(1, len(all_rules)))
        subset = [dict(r, priority=rnd.randint(-5, 5)) for r in subset]
        expect(plan_bytes(resolver.resolve(inv, subset))
               == ref.canonical_json(ref.resolve(inv, subset, authorised_sources=PRODUCT_SOURCE_POLICY)),
               "random parity case differs")
        n += 1
    return f"{n} inventory x rule-set cases byte-identical between production and the M1-A reference"


@check
def digests_match_reference():
    for name in ("01-intel-cpu-intel-gpu", "06-usb-bluetooth", "12-determinism-shuffled"):
        inv = fixture(name)
        expect(resolver.digest(resolver.normalize_inventory(inv)) == ref.inventory_digest(inv), f"{name} digest")
    for rules in (production_rules(), test_rules()):
        expect(resolver.rule_set_digest(rules) == ref.rule_set_digest(rules), "rule-set digest")
    expect(resolver.canonical_json({"a": 1}) == b'{"a":1}', "canonical form")
    return "inventory and rule-set digests equal the reference helpers"


# ---------------------------------------------------------------------------
# Inputs


@check
def valid_inventory_accepted():
    plan = produce(fixture("01-intel-cpu-intel-gpu"))
    expect(plan["status"] in ("resolved", "partial") and "inventory_digest" in plan, f"status {plan['status']}")
    expect(classes(plan) == {"cpu.intel": ["cpu"], "gpu.intel": ["pci:0000:00:02.0"],
                             "platform.bare-metal": ["platform"]}, f"{classes(plan)}")
    return "fixture 01 resolves to cpu.intel + gpu.intel + platform.bare-metal with the production rules"


@check
def invalid_inventory_bounded():
    inv = fixture("10-forbidden-field")
    plan = produce(inv)
    expect(plan["status"] == "invalid" and "inventory_digest" not in plan, "status/digest")
    expect(plan["errors"] == [{"path": "/devices/1", "check": "additionalProperties"}], f"{plan['errors']}")
    for key in ("matched_rules", "classifications", "packages", "profiles", "sources", "unresolved", "conflicts"):
        expect(plan[key] == [], f"invalid plan has {key}")
    expect(b"02:00:00:00:00:01" not in plan_bytes(plan), "rejected value echoed")
    plan = produce(["not", "an", "inventory"])
    expect(plan["status"] == "invalid" and plan["errors"] == [{"path": "", "check": "type"}], "non-object")
    return "rejected inventories give status invalid with path/check errors only"


@check
def malformed_json_rejected():
    bad = [b"", b"{", b"{} {}", b"\xff\xfe{}", b'{"a": 1.5}', b'{"a": NaN}', b'{"a": Infinity}',
           b'{"a": 1, "a": 2}', b'{"devices": [{"id": "x", "id": "y"}]}', b"[" * 100000]
    for data in bad:
        expect_error(lambda d=data: resolver.parse_strict(d), f"accepted {data[:20]!r}", "malformed_json")
    return f"{len(bad)} malformed inputs (truncated, trailing data, non-UTF-8, floats, NaN, duplicate keys, deep nesting) rejected"


# ---------------------------------------------------------------------------
# Rule trust boundary


def _copy_rules(dst):
    dst.mkdir()
    for p in PRODUCTION_RULES.iterdir():
        shutil.copy2(p, dst / p.name)
    return dst


@check
def rule_trust_boundary():
    load = lambda d: resolver.load_rule_directory(str(d), production_policy=True)  # noqa: E731
    with scratch() as d:
        good = _copy_rules(d / "good")
        expect([r["id"] for r in load(good)]
               == sorted([*EXPECTED_PRODUCTION_RULES, *EXPECTED_PACKAGE_RULES, *FIRMWARE_PACKAGE_RULES]),
               "copy of production rules")
        cases = []

        def case(label, mutate):
            target = _copy_rules(d / f"case{len(cases)}")
            mutate(target)
            cases.append(label)
            expect_error(lambda: load(target), label, "rule_set_rejected")

        case("filename mismatch", lambda t: (t / "class.cpu.amd.json").rename(t / "class.cpu.amd2.json"))
        case("symlink rule", lambda t: (t / "class.cpu.amd.json").unlink() or
             os.symlink(good / "class.cpu.amd.json", t / "class.cpu.amd.json"))
        case("symlink to a non-rule", lambda t: os.symlink("/dev/null", t / "class.x.json"))
        case("symlinked documentation", lambda t: (t / "README.md").unlink() or os.symlink(good / "README.md", t / "README.md"))
        case("hidden rule", lambda t: shutil.copy2(t / "class.cpu.amd.json", t / ".class.cpu.amd.json"))
        case("hidden file", lambda t: (t / ".keep").write_text(""))
        case("subdirectory", lambda t: (t / "extra").mkdir())
        case("directory named like a rule", lambda t: (t / "class.y.json").mkdir())
        case("other file type", lambda t: (t / "notes.txt").write_text("x"))
        case("upper-case extension", lambda t: shutil.copy2(t / "class.cpu.amd.json", t / "class.cpu.amd.JSON"))
        case("duplicate key", lambda t: (t / "class.cpu.amd.json").write_text(
            (t / "class.cpu.amd.json").read_text().replace('"priority": 100', '"priority": 100, "priority": 1')))
        case("float priority", lambda t: (t / "class.cpu.amd.json").write_text(
            (t / "class.cpu.amd.json").read_text().replace('"priority": 100', '"priority": 100.0')))
        case("not UTF-8", lambda t: (t / "class.cpu.amd.json").write_bytes(b"\xff" + (t / "class.cpu.amd.json").read_bytes()))
        case("truncated JSON", lambda t: (t / "class.cpu.amd.json").write_text("{"))
        case("schema-invalid rule", lambda t: (t / "class.cpu.amd.json").write_text(
            (t / "class.cpu.amd.json").read_text().replace('"priority": 100', '"priority": 100, "command": "x"')))
        def package_source(source):
            def mutate(t):
                doc = ref.load_json(good / "package.cpu.amd64-microcode.json")
                doc["effects"]["packages"][0]["source"] = source
                (t / "package.cpu.amd64-microcode.json").write_text(json.dumps(doc))
            return mutate
        for source in ("contrib", "non-free", "backports", "lubarchy"):
            case(f"package from {source}", package_source(source))

        def forbid(t):
            doc = ref.load_json(good / "package.cpu.amd64-microcode.json")
            doc["effects"]["packages"] = [{"name": "amd64-microcode", "disposition": "forbid", "reason": "x"}]
            (t / "package.cpu.amd64-microcode.json").write_text(json.dumps(doc))
        case("package forbid effect", forbid)
        case("profile effect", lambda t: (t / "class.cpu.amd.json").write_text(json.dumps(dict(
            ref.load_json(good / "class.cpu.amd.json"), effects={"classifications": ["cpu.amd"],
                                                                 "profiles": [{"name": "p", "reason": "x"}]}))))
        case("warning effect", lambda t: (t / "class.cpu.amd.json").write_text(json.dumps(dict(
            ref.load_json(good / "class.cpu.amd.json"), effects={"classifications": ["cpu.amd"],
                                                                 "warnings": [{"code": "w", "message": "x"}]}))))
        dup = rule("class.dup", [{"cpu": {"vendor": ["GenuineIntel"]}}], classifications=["cpu.intel"])
        expect_error(lambda: resolver.check_rule_set([dup, copy.deepcopy(dup)]), "duplicate rule id", "rule_set_rejected")
        cases.append("duplicate rule id")
    return f"{len(cases)} untrusted or malformed rule-directory states each reject the whole set"


@check
def deterministic_rule_loading():
    a = production_rules()
    with scratch() as d:
        t = d / "rules"
        t.mkdir()
        for p in sorted(PRODUCTION_RULES.iterdir(), reverse=True):
            shutil.copy2(p, t / p.name)
        b = resolver.load_rule_directory(str(t), production_policy=True)
    expect(a == b and [r["id"] for r in a] == sorted(r["id"] for r in a), "rule loading order")
    expect(resolver.rule_set_digest(a) == resolver.rule_set_digest(list(reversed(a))), "rule-set digest order")
    return f"{len(a)} rules load in id order regardless of creation order; rule-set digest {resolver.rule_set_digest(a)}"


@check
def production_rule_policy():
    files = sorted(p.name for p in PRODUCTION_RULES.iterdir())
    expect(files == sorted([f"{r}.json" for r in [*EXPECTED_PRODUCTION_RULES, *EXPECTED_PACKAGE_RULES,
                                                   *FIRMWARE_PACKAGE_RULES]]
                           + ["README.md", "SOURCES.md"]), f"unexpected production directory content: {files}")
    for rid, package in FIRMWARE_PACKAGE_RULES.items():
        doc = ref.load_json(PRODUCTION_RULES / f"{rid}.json")
        expect([p["name"] for p in doc["effects"]["packages"]] == [package], f"{rid}: package")
        expect(set(doc["effects"]) <= {"classifications", "packages"}, f"{rid}: effects")
        expect(all(list(sel) in (["pci"], ["usb"]) and list(next(iter(sel.values()))) == ["modalias"]
                   for sel in doc["match"]), f"{rid}: must match modalias aliases only")
    for rid, cls in EXPECTED_PRODUCTION_RULES.items():
        doc = ref.load_json(PRODUCTION_RULES / f"{rid}.json")
        expect(doc["effects"] == {"classifications": [cls]}, f"{rid}: effects {doc['effects']}")
        expect(doc["priority"] == 100, f"{rid}: priority")
        expect(set(doc) == {"schema", "id", "description", "priority", "match", "effects"}, f"{rid}: fields")
    packages = set()
    for rid, (vendor, package) in EXPECTED_PACKAGE_RULES.items():
        doc = ref.load_json(PRODUCTION_RULES / f"{rid}.json")
        expect(doc["match"] == [{"cpu": {"vendor": [vendor]}}], f"{rid}: selector {doc['match']}")
        expect(list(doc["effects"]) == ["packages"] and len(doc["effects"]["packages"]) == 1, f"{rid}: effects")
        pkg = doc["effects"]["packages"][0]
        expect(pkg["name"] == package and pkg["disposition"] == "require" and pkg["source"] == "non-free-firmware",
               f"{rid}: package effect {pkg}")
        expect(doc["priority"] == 100 and set(doc) == {"schema", "id", "description", "priority", "match", "effects"},
               f"{rid}: fields")
    for r in production_rules():
        for eff in r["effects"]:
            expect(eff in ("classifications", "packages"), f"{r['id']}: production effect {eff}")
        for pkg in r["effects"].get("packages", []):
            packages.add(pkg["name"])
            expect(pkg["disposition"] == "require", f"{r['id']}: {pkg['disposition']}")
            expect(pkg["source"] == "non-free-firmware", f"{r['id']}: {pkg['name']} from {pkg['source']}")
    expect(packages == PRODUCTION_PACKAGES, f"production package set {sorted(packages)}")
    plan = produce(fixture("04-intel-nvidia-hybrid"))
    expect(plan["profiles"] == [] and plan["warnings"] == [], "production plan proposes profiles or warnings")
    expect(plan["source_policy"] == {"authorised": list(PRODUCT_SOURCE_POLICY)}, "source policy")
    return (f"{len(EXPECTED_PRODUCTION_RULES)} classification rules + 2 CPU + 4 firmware package rules; exact package "
            "set of 6, all 'require' from non-free-firmware; no profile/warning/forbid")


def _packages(plan):
    return {(p["name"], p["disposition"], p.get("source"), p.get("source_status")) for p in plan["packages"]}


@check
def package_and_source_policy():
    expect(resolver.AUTHORISED_SOURCES == PRODUCT_SOURCE_POLICY, f"source policy {resolver.AUTHORISED_SOURCES}")
    for source in ("contrib", "non-free", "backports", "lubarchy"):
        expect(source not in resolver.AUTHORISED_SOURCES, f"{source} authorised")
    intel = produce(inventory(vendor="GenuineIntel"))
    expect(_packages(intel) == {("intel-microcode", "require", "non-free-firmware", "authorised")}, f"intel {intel['packages']}")
    expect(intel["sources"] == [{"source": "non-free-firmware", "status": "authorised", "packages": ["intel-microcode"]}],
           f"intel sources {intel['sources']}")
    expect(classes(intel)["cpu.intel"] == ["cpu"], "Intel classification unchanged")
    amd = produce(inventory(vendor="AuthenticAMD"))
    expect(_packages(amd) == {("amd64-microcode", "require", "non-free-firmware", "authorised")}, f"amd {amd['packages']}")
    expect(classes(amd)["cpu.amd"] == ["cpu"], "AMD classification unchanged")
    for vendor in ("HygonGenuine", "CentaurHauls", "  Shanghai", "genuineintel", "authenticamd", "AuthenticAM",
                   "GenuineIotel", "SyntheticCPU"):
        if vendor != vendor.strip():
            continue
        plan = produce(inventory(vendor=vendor))
        expect(plan["packages"] == [] and plan["sources"] == [], f"{vendor} got {plan['packages']}")
        expect("cpu" in [u["target"] for u in plan["unresolved"]], f"{vendor} CPU not unresolved")
    devices = [pci("0000:00:02.0", "8086", "f001", "030000"), pci("0000:02:00.0", "8086", "f007", "028000"),
               pci("0000:05:00.0", "f0ff", "f0ff", "ff0000")]
    plan = produce(inventory(devices, vendor="SyntheticCPU"))
    expect(plan["packages"] == [], "devices without a qualified firmware alias produced packages")
    # Representation is not authorisation: a non-production rule requiring
    # an unauthorised component is still reported for policy review.
    cpu = [{"cpu": {"vendor": ["GenuineIntel"]}}]
    for source in ("contrib", "non-free", "backports"):
        req = {"packages": [{"name": "lubarchy-test-x", "disposition": "require", "source": source, "reason": "r"}]}
        plan = produce(inventory(), [rule("t.src", cpu, 1, **req)])
        expect(plan["sources"] == [{"source": source, "status": "policy_review_required", "packages": ["lubarchy-test-x"]}]
               and plan["status"] == "partial", f"{source} not flagged")
    return ("policy main + non-free-firmware; Intel -> intel-microcode, AMD -> amd64-microcode (authorised); "
            "Hygon/Centaur/mis-cased/unknown -> nothing; contrib/non-free/backports -> policy_review_required")


# ---------------------------------------------------------------------------
# Semantics (independently of the reference)


@check
def selector_semantics():
    inv = inventory([pci("0000:01:00.0", "10de", "f003", "030200"), pci("0000:00:1f.3", "10de", "f004", "040300")])
    r = lambda match: resolver.matched_targets(rule("t.x", match), resolver.normalize_inventory(inv))  # noqa: E731
    expect(r([{"pci": {"vendor_id": ["10de"], "class_id": ["03"]}}]) == ["pci:0000:01:00.0"], "AND within selector")
    expect(r([{"pci": {"vendor_id": ["10de", "8086"], "class_id": ["04"]}}]) == ["pci:0000:00:1f.3"], "OR within field")
    expect(r([{"pci": {"class_id": ["05"]}}, {"cpu": {"vendor": ["GenuineIntel"]}}]) == ["cpu"], "ANY selector")
    expect(r([{"pci": {"vendor_id": ["10de"], "subsystem_vendor_id": ["10de"]}}]) == [], "missing field matched")
    expect(r([{"pci": {"modalias": ["*"]}}]) == [], "missing modalias matched '*'")
    expect(r([{"platform": {"boot_firmware": ["uefi"]}}]) == ["platform"], "platform")
    no_fw = inventory(firmware=None)
    expect(resolver.matched_targets(rule("t.y", [{"platform": {"boot_firmware": ["uefi", "bios"]}}]), no_fw) == [],
           "missing boot_firmware matched")
    return "AND within a selector, OR within a field, ANY selector, absent fields never match"


@check
def priority_orders_only():
    inv = fixture("04-intel-nvidia-hybrid")
    rules = test_rules()
    a = produce(inv, rules)
    b = produce(inv, [dict(r, priority=-r["priority"]) for r in rules])
    expect([m["id"] for m in a["matched_rules"]] != [m["id"] for m in b["matched_rules"]], "order unchanged")
    strip = lambda p: (p["status"], [(c["name"], c["targets"], sorted(c["rules"])) for c in p["classifications"]],  # noqa: E731
                       [(x["name"], x.get("source")) for x in p["packages"]], p["sources"], p["unresolved"])
    expect(strip(a) == strip(b), "priority changed the resolution")
    return "negating every priority reorders matched_rules but changes no classification, package or source"


@check
def conflicts_fail_closed():
    inv = fixture("03-nvidia-discrete-gpu")
    plan = produce(inv, conflict_rules())
    expect(plan["status"] == "conflict" and plan["packages"] == plan["profiles"] == plan["sources"] == [], "require/forbid")
    nv = [{"pci": {"vendor_id": ["10de"]}}]
    req = lambda src: {"packages": [{"name": "lubarchy-test-x", "disposition": "require", "source": src, "reason": "r"}]}  # noqa: E731
    plan = produce(inv, [rule("t.a", nv, 9, **req("main")), rule("t.b", nv, 1, **req("backports"))])
    expect(plan["status"] == "conflict" and plan["conflicts"][0]["kind"] == "package_source_mismatch", "source mismatch")
    prof = lambda n, ex=None: {"profiles": [dict({"name": n, "reason": "r"}, **({"excludes": ex} if ex else {}))]}  # noqa: E731
    plan = produce(inv, [rule("t.p", nv, 1, **prof("p-a", ["p-b"])), rule("t.q", nv, 1, **prof("p-b"))])
    expect(plan["status"] == "conflict" and plan["conflicts"][0]["kind"] == "profile_exclusion", "profile exclusion")
    expect(plan["profiles"] == [], "conflict plan proposes profiles")
    return "required+forbidden, source mismatch and profile exclusion -> status conflict, nothing proposed"


@check
def unknown_hardware_gets_nothing():
    inv = inventory([pci("0000:05:00.0", "f0ff", "f0ff", "ff0000")], vendor="SyntheticCPU", virt=None)
    for rules in (production_rules(), test_rules()):
        plan = produce(inv, rules)
        expect(plan["status"] == "partial" and plan["classifications"] == [] and plan["packages"] == [], "unknown")
        expect([u["target"] for u in plan["unresolved"]] == ["cpu", "pci:0000:05:00.0"], f"{plan['unresolved']}")
    return "unknown CPU vendor and PCI device stay unresolved and receive nothing"


# ---------------------------------------------------------------------------
# Production classifications


@check
def cpu_classifications():
    expect(classes(produce(inventory(vendor="GenuineIntel")))["cpu.intel"] == ["cpu"], "Intel CPU")
    expect(classes(produce(inventory(vendor="AuthenticAMD")))["cpu.amd"] == ["cpu"], "AMD CPU")
    for vendor in ("HygonGenuine", "CentaurHauls", "genuineintel", "AuthenticAM"):
        plan = produce(inventory(vendor=vendor))
        expect(not any(c.startswith("cpu.") for c in classes(plan)), f"{vendor} classified")
    return "GenuineIntel -> cpu.intel, AuthenticAMD -> cpu.amd; other or mis-cased vendors unclassified"


@check
def gpu_classifications():
    devices = [pci("0000:00:02.0", "8086", "f001", "030000"), pci("0000:03:00.0", "1002", "f002", "030000"),
               pci("0000:01:00.0", "10de", "f003", "030200"), pci("0000:04:00.0", "10de", "f005", "038000"),
               pci("0000:05:00.0", "8086", "f006", "030100")]
    c = classes(produce(inventory(devices)))
    expect(c["gpu.intel"] == ["pci:0000:00:02.0", "pci:0000:05:00.0"], f"intel {c}")
    expect(c["gpu.amd"] == ["pci:0000:03:00.0"], f"amd {c}")
    expect(c["gpu.nvidia"] == ["pci:0000:01:00.0", "pci:0000:04:00.0"], f"nvidia {c}")
    return "display class 03 (VGA, XGA, 3D, other) from 8086/1002/10de -> gpu.intel/amd/nvidia"


@check
def non_display_and_other_vendors_are_not_gpus():
    devices = [pci("0000:00:14.0", "8086", "f008", "0c0330"), pci("0000:00:1f.3", "8086", "f0c0", "040300"),
               pci("0000:01:00.1", "10de", "f010", "040300"), pci("0000:03:00.1", "1002", "f011", "040300"),
               pci("0000:00:01.0", "1234", "1111", "030000"), pci("0000:00:02.0", "1af4", "1050", "030000"),
               pci("0000:00:03.0", "1022", "f012", "030000"), pci("0000:00:04.0", "102b", "0522", "030000")]
    plan = produce(inventory(devices, virt=("vm", "kvm")))
    expect(not any(n.startswith("gpu.") for n in classes(plan)), f"misclassified: {classes(plan)}")
    expect(len([u for u in plan["unresolved"] if u["target"].startswith("pci:")]) == len(devices), "must stay unresolved")
    return "Intel xHCI/audio, NVIDIA/AMD HDMI audio and QEMU/virtio/AMD-1022/Matrox display devices are not gpu.*"


@check
def network_and_storage_classifications():
    devices = [pci("0000:02:00.0", "10ec", "f006", "020000"), pci("0000:03:00.0", "8086", "f007", "028000"),
               pci("0000:04:00.0", "f0a1", "f00a", "010802"), pci("0000:00:17.0", "8086", "f00b", "010601"),
               pci("0000:00:17.1", "8086", "f00c", "010600"), pci("0000:05:00.0", "f0a1", "f00d", "010803"),
               pci("0000:06:00.0", "8086", "f00e", "020100"), pci("0000:07:00.0", "8086", "f00f", "010400")]
    c = classes(produce(inventory(devices)))
    expect(c["network.ethernet"] == ["pci:0000:02:00.0"], f"ethernet {c}")
    expect(c["network.other"] == ["pci:0000:03:00.0"], f"0280 {c}")
    expect(not any("wifi" in n or "wlan" in n or "wireless" in n for n in c), f"0280 called Wi-Fi: {c}")
    expect(c["storage.nvme"] == ["pci:0000:04:00.0"], f"nvme {c}")
    expect(c["storage.sata"] == ["pci:0000:00:17.0", "pci:0000:00:17.1"], f"sata {c}")
    for unmatched in ("pci:0000:05:00.0", "pci:0000:06:00.0", "pci:0000:07:00.0"):
        expect(not any(unmatched in t for t in c.values()), f"{unmatched} classified")
    return "0200 -> ethernet, 0280 -> network.other (never Wi-Fi), 010802 -> nvme, 0106xx -> sata; 010803/0201/0104 unclassified"


@check
def platform_classifications():
    expect(classes(produce(inventory(virt=("vm", "kvm"))))["platform.vm"] == ["platform"], "vm")
    expect(classes(produce(inventory(virt=("container", "systemd-nspawn"))))["platform.container"] == ["platform"],
           "container")
    expect(classes(produce(inventory(virt=("none", None))))["platform.bare-metal"] == ["platform"], "bare metal")
    c = classes(produce(inventory(virt=None)))
    expect(not any(n.startswith("platform.") for n in c), "platform classified without a virtualization observation")
    return "vm, container and explicit kind=none classified; no observation -> no platform classification"


@check
def no_cross_device_hybrid_claim():
    plan = produce(fixture("04-intel-nvidia-hybrid"))
    c = classes(plan)
    gpu_classes = sorted(n for n in c if n.startswith("gpu."))
    expect(gpu_classes == ["gpu.intel", "gpu.nvidia"], f"gpu classes {gpu_classes}")
    expect("gpu.hybrid" not in c, "gpu.hybrid emitted")
    for r in production_rules():
        expect(not any("hybrid" in n for n in r["effects"].get("classifications", [])), f"{r['id']} claims hybrid")
    return f"diagnostic: hybrid fixture has {len(gpu_classes)} GPU classifications ({', '.join(gpu_classes)}); no gpu.hybrid"


# ---------------------------------------------------------------------------
# Determinism


@check
def determinism():
    rnd = random.Random("lubarchy-m1c-determinism")
    rules = production_rules()
    for name in ("02-amd-cpu-amd-gpu", "04-intel-nvidia-hybrid", "09-qemu-kvm-guest", "12-determinism-shuffled"):
        inv = fixture(name)
        want = plan_bytes(produce(inv))
        d = resolver.digest(resolver.normalize_inventory(inv))
        for _ in range(15):
            shuffled = copy.deepcopy(inv)
            rnd.shuffle(shuffled["devices"])
            r = [copy.deepcopy(x) for x in rules]
            rnd.shuffle(r)
            expect(plan_bytes(produce(shuffled, r)) == want, f"{name}: plan bytes changed")
            expect(resolver.digest(resolver.normalize_inventory(shuffled)) == d, f"{name}: digest changed")
    a = plan_bytes(produce(fixture("06-usb-bluetooth")))
    b = plan_bytes(produce(fixture("12-determinism-shuffled")))
    expect(a == b, "shuffled fixture gives different plan")
    return "plan bytes, inventory digest and rule-set digest stable under 15 seeded shuffles x 4 fixtures"


# ---------------------------------------------------------------------------
# CLI


def run_cli(argv, stdin=b""):
    out, err = io.BytesIO(), io.StringIO()
    saved_out, saved_in = sys.stdout, sys.stdin
    sys.stdout = io.TextIOWrapper(out, encoding="utf-8")
    sys.stdin = io.TextIOWrapper(io.BytesIO(stdin), encoding="utf-8")
    try:
        with contextlib.redirect_stderr(err):
            code = resolve.main(argv)
        sys.stdout.flush()
    finally:
        sys.stdout.detach()
        sys.stdout, sys.stdin = saved_out, saved_in
    return code, out.getvalue(), err.getvalue()


@check
def cli_contract():
    path = str(FIXTURES / "inventory" / "04-intel-nvidia-hybrid.json")
    code, out, err = run_cli(["--inventory", path])
    expect(code == 0 and err == "", f"valid: {code} {err!r}")
    expect(out == plan_bytes(produce(fixture("04-intel-nvidia-hybrid"))) + b"\n", "stdout is not one canonical plan")
    data = (FIXTURES / "inventory" / "04-intel-nvidia-hybrid.json").read_bytes()
    expect(run_cli(["--inventory", "-"], data) == (0, out, ""), "stdin input differs")
    code, out2, err = run_cli(["--inventory", str(FIXTURES / "invalid" / "10-forbidden-field.json")])
    expect(code == 2 and err == "" and json.loads(out2)["status"] == "invalid", "invalid inventory: exit 2 with plan")
    with scratch() as d:
        for content in (b"{", b'{"a":1,"a":2}', b"\xff"):
            (d / "bad.json").write_bytes(content)
            code, o, e = run_cli(["--inventory", str(d / "bad.json")])
            expect(code == 3 and o == b"" and e.startswith("resolve: error: malformed_json:"), f"bad JSON: {code} {e!r}")
        code, o, e = run_cli(["--inventory", str(d / "missing.json")])
        expect(code == 3 and o == b"" and "input_unreadable" in e, "missing file")
        broken = _copy_rules(d / "rules")
        (broken / "class.cpu.amd.json").write_text("{")
        saved = resolver.PRODUCTION_RULES_DIR
        try:
            resolver.PRODUCTION_RULES_DIR = str(broken)
            code, o, e = run_cli(["--inventory", path])
            expect(code == 4 and o == b"" and e.startswith("resolve: error: rule_set_rejected:"), f"bad rules {code}")
        finally:
            resolver.PRODUCTION_RULES_DIR = saved
    saved = resolver.load_production_rules
    try:
        resolver.load_production_rules = conflict_rules
        code, o, e = run_cli(["--inventory", str(FIXTURES / "inventory" / "03-nvidia-discrete-gpu.json")])
        expect(code == 0 and e == "" and json.loads(o)["status"] == "conflict", "conflict is a produced plan, exit 0")
    finally:
        resolver.load_production_rules = saved
    # Exit 5 (internal contract failure), reached by substituting the
    # resolver function inside this test process only; production code has
    # no hook for it.
    saved = resolver.resolve
    try:
        for broken in ({"schema": "lubarchy.hardware.plan/v1"},
                       {**produce(fixture("02-amd-cpu-amd-gpu")), "commands": ["apt-get install x"]},
                       {**produce(fixture("02-amd-cpu-amd-gpu")), "status": "resolved", "conflicts": [
                           {"kind": "package_source_mismatch", "subject": "x1", "rules": ["t.a"]}]},
                       {**produce(fixture("02-amd-cpu-amd-gpu")), "inventory_digest": 1.5}):
            resolver.resolve = lambda inv, rules, broken=broken: copy.deepcopy(broken)
            code, o, e = run_cli(["--inventory", path])
            expect(code == 5 and o == b"" and e.startswith("resolve: error: contract_failure:"),
                   f"contract failure: {code} {o[:40]!r} {e!r}")
    finally:
        resolver.resolve = saved
    for argv in ([], ["--inventory"], ["--rules", "x"], ["--inventory", path, "--rules", str(FIXTURES / "rules")],
                 ["--inventory=" + path], ["-i", path], ["--inventory", ""]):
        code, o, e = run_cli(argv)
        expect(code == 64 and o == b"" and e.startswith("usage:"), f"usage {argv}: {code}")
    return ("exit 0 (plan; also for conflict), 2 (invalid plan), 3 (bad input), 4 (bad rules), "
            "5 (contract failure, 4 cases), 64 (usage); stdin '-'")


@check
def cli_has_no_rules_option():
    text = (HARDWARE / "resolve.py").read_text(encoding="utf-8")
    tree = ast.parse(text)
    strings = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    expect(not any("--rule" in s or "rules_dir" in s for s in strings), "CLI mentions a rules option")
    expect(not any(isinstance(n, ast.Attribute) and n.attr in ("load_rule_directory", "PRODUCTION_RULES_DIR")
                   for n in ast.walk(tree)), "CLI selects a rule directory itself")
    expect("argparse" not in text and "getopt" not in text, "CLI uses a general option parser")
    proc = subprocess.run([str(HARDWARE / "resolve.py"), "--inventory", str(FIXTURES / "inventory" / "07-nvme-storage.json"),
                           "--rules", str(FIXTURES / "rules")], capture_output=True, timeout=60)
    expect(proc.returncode == 64 and proc.stdout == b"", "process accepted --rules")
    proc = subprocess.run([str(HARDWARE / "resolve.py"), "--inventory", str(FIXTURES / "inventory" / "07-nvme-storage.json")],
                          capture_output=True, timeout=60)
    expect(proc.returncode == 0 and proc.stderr == b"" and json.loads(proc.stdout)["status"] == "resolved",
           "executable end-to-end run")
    return "only --inventory PATH|- exists; the production rule directory is fixed; executable end-to-end run OK"


# ---------------------------------------------------------------------------
# Side-effect boundary (source inspection)


def _dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


@check
def resolver_source_has_no_side_effects():
    allowed_imports = {"resolver.py": {"fnmatch", "hashlib", "json", "os", "stat", "jsonschema"},
                       "resolve.py": {"sys", "resolver"}}
    for name, allowed in allowed_imports.items():
        text = (HARDWARE / name).read_text(encoding="utf-8")
        tree = ast.parse(text)
        imports = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                imports.update(a.name.split(".")[0] for a in n.names)
            elif isinstance(n, ast.ImportFrom):
                imports.add((n.module or "").split(".")[0])
        expect(imports == allowed, f"{name}: imports {imports}")
        calls = {_dotted(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
        banned_calls = {c for c in calls if c.startswith(("subprocess.", "socket.", "os.system", "os.popen", "os.exec",
                                                          "os.spawn", "os.fork", "os.remove", "os.unlink", "os.rename",
                                                          "os.replace", "os.mkdir", "os.makedirs", "os.chmod",
                                                          "os.chown", "os.symlink", "os.write", "shutil."))
                        or c in ("eval", "exec", "__import__", "compile")}
        expect(not banned_calls, f"{name}: {banned_calls}")
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and _dotted(n.func) == "open":
                expect(len(n.args) > 1 and isinstance(n.args[1], ast.Constant) and n.args[1].value == "rb",
                       f"{name}: open without 'rb'")
            if isinstance(n, ast.Call) and _dotted(n.func) == "os.open":
                flags = {_dotted(x) for x in ast.walk(n.args[1]) if isinstance(x, ast.Attribute)}
                expect(not flags & {"os.O_WRONLY", "os.O_RDWR", "os.O_CREAT", "os.O_TRUNC", "os.O_APPEND"},
                       f"{name}: os.open with write flags")
            if isinstance(n, ast.keyword) and n.arg == "shell":
                raise Failure(f"{name}: shell keyword")
        writes = sorted(c for c in calls if c.endswith((".write", ".write_text", ".write_bytes")))
        expect(writes == ([] if name == "resolver.py" else ["sys.stderr.write", "sys.stdout.buffer.write"]),
               f"{name}: writes {writes}")
        strings = " ".join(n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str))
        for pattern in (r"\bapt(-get|-cache|itude)?\b", r"\bdpkg\b", r"\bmodprobe\b", r"\budevadm\b", r"/sys/",
                        r"/proc/", r"dbus", r"/etc/apt", r"sources\.list", r"https?://", r"packagekit", r"pkcon",
                        r"unattended-upgrade", r"\binstall\b.*-y"):
            expect(not re.search(pattern, strings, re.I), f"{name}: mentions {pattern}")
    return "resolver imports only stdlib + jsonschema; no process, network, APT/dpkg/modprobe/udevadm, sysfs or write"


# ---------------------------------------------------------------------------
# Synthetic probe -> resolver integration


@check
def probe_to_resolver_integration():
    with scratch() as d:
        tree = probe_tests.workstation(d / "sys-tree")
        runner = probe_tests.FakeRunner(vm="kvm")
        p = probe.Probe(sys_root=tree.sys, proc_root=tree.proc, runner=runner, uname=probe_tests.uname)
        inventory_bytes = probe.canonical_json(p.inventory())
    inv = resolver.parse_strict(inventory_bytes)
    expect(resolver.inventory_errors(inv) == [], "probe inventory invalid")
    expect(resolver.canonical_json(resolver.normalize_inventory(inv)) == inventory_bytes, "inventory not canonical")
    plan = produce(inv)
    expect(plan_bytes(plan) == ref.canonical_json(ref.resolve(inv, production_rules(),
                                                             authorised_sources=PRODUCT_SOURCE_POLICY)),
           "integration parity")
    expect(classes(plan) == {"cpu.intel": ["cpu"], "gpu.nvidia": ["pci:0000:01:00.0"], "platform.vm": ["platform"]},
           f"classifications {classes(plan)}")
    unresolved = [u["target"] for u in plan["unresolved"]]
    expect(unresolved == ["pci:0000:00:00.0", "pci:0000:00:01.0", "pci:0000:00:14.0", "pci:0000:00:1f.3",
                          "usb:1-2", "usb:1-2.1", "usb:1-3"], f"unresolved {unresolved}")
    expect(plan["status"] == "partial" and plan["profiles"] == [], "status/profiles")
    expect(_packages(plan) == {("intel-microcode", "require", "non-free-firmware", "authorised")}, f"{plan['packages']}")
    expect(all(c[0] in (probe.DPKG, probe.DETECT_VIRT, probe.UDEVADM) for c in runner.calls), "unexpected command")
    return ("synthetic sysfs -> probe -> canonical inventory -> production plan: 3 classifications, 7 unresolved, "
            "intel-microcode proposed (non-free-firmware, authorised)")


def main():
    sys.stdout.reconfigure(line_buffering=True)
    from importlib.metadata import version
    print(f"python {sys.version.split()[0]}, jsonschema {version('jsonschema')}")
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
        print(f"FAIL: hardware resolver ({failures} of {len(CHECKS)} checks failed)", file=sys.stderr)
        return 1
    print(f"PASS: hardware resolver ({len(CHECKS)} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
