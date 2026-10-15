#!/usr/bin/python3
"""LUBARCHY post-install hardware validation gate (M1-M; synthetic, non-mutating).

Drives hardware/hardware_validation.py and hardware/validate-hardware.py
with synthetic inputs only:

- inventories from the resolver fixtures (tests/fixtures/hardware/inventory/)
  and devices instantiated from the committed firmware alias evidence (the
  same helpers as the firmware gate), resolved by the production rules;
- synthetic normalized runtime evidence (healthy, then one perturbation per
  case) for the pure evaluator;
- a synthetic sysfs/procfs tree and a fake command runner for the collector.

It never reads the CI runner's hardware, kernel log, packages or services,
never runs a command, needs no root, and writes only inside its temporary
directory.
"""

import sys

sys.dont_write_bytecode = True

import ast  # noqa: E402
import contextlib  # noqa: E402
import copy  # noqa: E402
import hashlib  # noqa: E402
import importlib.util  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import pathlib  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402

TESTS = pathlib.Path(__file__).resolve().parent
REPO = TESTS.parent
HARDWARE = REPO / "hardware"
INVENTORIES = TESTS / "fixtures" / "hardware" / "inventory"
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(HARDWARE))
import hardware_validation as hv  # noqa: E402
import resolver  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate7 = _load("gate7", TESTS / "hardware-firmware.py")
gate10 = _load("gate10", TESTS / "hardware-staging.py")
cli = _load("validate_cli", HARDWARE / "validate-hardware.py")

RESULT_SCHEMA_SHA256 = "9736ff4494b6cc5c593a4bd791275b066fd3b795af13667df0831caf0425b248"
RULES = resolver.load_production_rules()
TMP = None


class Failure(Exception):
    pass


def expect(cond, message):
    if not cond:
        raise Failure(message)


CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


def rejects(code, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except hv.ValidationError as e:
        expect(e.code == code, f"expected {code}, got {e.code}: {e.message}")
        return e
    raise Failure(f"expected {code}, but it was accepted")


# ---------------------------------------------------------------------------
# Synthetic inputs

def fixture(name):
    return json.loads((INVENTORIES / f"{name}.json").read_text(encoding="utf-8"))


EVIDENCE = gate7.evidence()


def family_device(index, module, cls, driver=None):
    """A concrete device for one committed alias of a firmware-family module."""
    alias = sorted(a for a in EVIDENCE["modules"][module]["aliases"] if a.split(":", 1)[0] in ("pci", "usb")
                   and a not in gate7.EXCLUDED_ALIASES)[0]
    dev = gate7.device_for(index, gate7.instantiate(alias, cls))
    if dev["bus"] == "pci":
        dev.update(driver=driver or module, modules=[module])
    else:
        dev["driver"] = "usb"
        dev["interfaces"][0].update(driver=driver or module, modules=[module])
    return dev


def synthetic(devices, vendor="GenuineIntel", virt="none", microcode="0x2c"):
    inv = gate7.inventory(devices, vendor)
    inv["system"]["virtualization"] = {"kind": virt, "technology": "kvm"} if virt == "vm" else {"kind": virt}
    if microcode:
        inv["cpu"]["microcode_revision"] = microcode
    inv["devices"].sort(key=lambda d: d["id"])
    return inv


def plan_of(inv):
    return resolver.check_plan(resolver.resolve(inv, RULES))


def targets_with(plan, name):
    return next((c["targets"] for c in plan["classifications"] if c["name"] == name), [])


BASE_PACKAGES = [{"package": "base-files", "architecture": "amd64", "version": "13.8", "status": "install ok installed",
                  "automatic": False},
                 {"package": "dpkg", "architecture": "amd64", "version": "1.22.21", "status": "install ok installed",
                  "automatic": False}]


def healthy(inv, privilege="root"):
    """All-healthy normalized runtime evidence consistent with the inventory and its plan."""
    plan = plan_of(inv)
    wifi, eth = set(targets_with(plan, "network.wifi")), set(targets_with(plan, "network.ethernet"))
    gpus = {t for c in plan["classifications"] if c["name"].startswith("gpu.") for t in c["targets"]}
    bts = set(targets_with(plan, "bluetooth.controller"))
    devices = {}
    for d in inv["devices"]:
        mods = set(d.get("modules", []))
        for itf in d.get("interfaces", []):
            mods.update(itf.get("modules", []))
            if itf.get("driver"):
                mods.add(itf["driver"].replace("-", "_"))
        devices[d["id"]] = {"present": True, "identity": hv.identity_of(d), "driver": d.get("driver"),
                            "modules": sorted(m for m in mods if hv.MODULE.match(m)),
                            "net": [{"wireless": True}] if d["id"] in wifi else
                            [{"wireless": False}] if d["id"] in eth else [],
                            "drm": 1 if d["id"] in gpus else 0, "bluetooth": 1 if d["id"] in bts else 0}
    pkgs = copy.deepcopy(BASE_PACKAGES) + [
        {"package": p["name"], "architecture": "all", "version": "1.0-1", "status": "install ok installed",
         "automatic": False} for p in plan["packages"]]
    return {"privilege": privilege,
            "boot": {"boot_id_digest": "sha256:" + hashlib.sha256(b"boot-a").hexdigest(), "boot_time": 2000},
            "devices": devices,
            "kernel_log": {"available": True, "firmware_failures": [], "microcode": {"loader_lines": 1, "errors": 0}},
            "system": {"state": "running", "failed_units": []},
            "packages": {"installed": sorted(pkgs, key=lambda e: (e["package"], e["architecture"])),
                         "audit_clean": True, "check": "ok",
                         "install_times": {p["name"]: 1000 for p in plan["packages"]}},
            "storage": {"mounted": True, "fstype": "ext4", "read_only": False, "block_visible": True, "device": None,
                        "fs_errors": 0},
            "apply_journal": None}


def run(inv, ev=None, previous=None):
    return hv.evaluate(inv, RULES, ev if ev is not None else healthy(inv), previous=previous)


def by_id(result):
    return {c["id"]: c for c in result["checks"]}


def status_of(result, cid):
    c = by_id(result).get(cid)
    expect(c is not None, f"check {cid} missing ({sorted(by_id(result))})")
    return c["status"], c["reason"]


# Synthetic machines: one per family, built from the production rules' own evidence.
WIFI = {
    "intel": family_device(1, "iwlwifi", "028000"),
    "realtek": family_device(2, "rtw88_8822ce", "028000"),
    "mediatek": family_device(3, "mt7921e", "028000"),
    "atheros": family_device(4, "ath10k_pci", "028000"),
}
GPU = {"intel": family_device(5, "i915", "030000"), "amd": family_device(6, "amdgpu", "030000")}
BT = family_device(250, "ath3k", "e00101")


def laptop(vendor="GenuineIntel", wifi="intel", gpu="intel", bt=True, virt="none", microcode="0x2c"):
    devs = [copy.deepcopy(WIFI[wifi]), copy.deepcopy(GPU[gpu])] + ([copy.deepcopy(BT)] if bt else [])
    return synthetic(devs, vendor, virt, microcode)


# ---------------------------------------------------------------------------
# Contract and profile

@check
def result_schema_is_strict_meta_valid_and_protected():                        # 40
    import jsonschema
    schema = json.loads((HARDWARE / "schemas" / hv.RESULT_SCHEMA_FILE).read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)

    def closed(node, path):
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                expect(node.get("additionalProperties") is False, f"open object at {path}")
            for k, v in node.items():
                closed(v, f"{path}/{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                closed(v, f"{path}/{i}")
    closed(schema, "")
    sha = hashlib.sha256((HARDWARE / "schemas" / hv.RESULT_SCHEMA_FILE).read_bytes()).hexdigest()
    expect(sha == RESULT_SCHEMA_SHA256, f"validation-result schema changed ({sha})")
    expect(gate10.PROTECTED_SCHEMAS.get(hv.RESULT_SCHEMA_FILE) == sha, "not in the gate-10 protected baseline")
    expect(set(schema["$defs"]["reason"]["enum"]) == set(hv.REASONS), "schema and module reason codes differ")
    return "validation-result v1 meta-valid, closed, protected; reason codes identical to the module"


CANONICAL_MAPPINGS = REPO / "tools" / "hwe" / "firmware-mappings.json"


def profile_problems(profile=None):
    """Every way the validator's embedded profile can drift from the canonical,
    committed sources (production rules, firmware-mappings.json)."""
    profile = profile or hv.PROFILE
    out = []
    classes = {c for r in RULES for c in r["effects"].get("classifications", [])}
    if set(profile["classifications"]) != classes:
        out.append(f"classifications differ: {sorted(classes ^ set(profile['classifications']))}")
    pkg_rules = {r["id"] for r in RULES if r["effects"].get("packages")}
    covered = set(profile["firmware_rules"]) | set(profile["microcode_rules"])
    if pkg_rules != covered:
        out.append(f"package rules not covered: {sorted(pkg_rules ^ covered)}")
    canonical = {f["rule_id"]: sorted(f["modules"])
                 for f in json.loads(CANONICAL_MAPPINGS.read_text(encoding="utf-8"))["families"]}
    if profile["firmware_rules"] != canonical:
        out.append("embedded firmware-family modules differ from tools/hwe/firmware-mappings.json")
    for rid, vendor in profile["microcode_rules"].items():
        rule = next((r for r in RULES if r["id"] == rid), None)
        if rule is None or rule["match"] != [{"cpu": {"vendor": [vendor]}}]:
            out.append(f"{rid}: vendor differs from the rule")
    if profile["classifications"].get("gpu.nvidia") != ["present", "unqualified_driver"]:
        out.append("NVIDIA must stay unqualified in M1-M")
    reqs = profile["firmware_requirements"]
    if set(reqs) != set(profile["firmware_rules"]):
        out.append("firmware requirement policy is not keyed exactly by the firmware families")
    for rid, entries in reqs.items():
        for e in entries:
            if set(e) != {"file", "requirement", "evidence"} or e["requirement"] not in hv.FIRMWARE_REQUIREMENTS or \
                    not re.fullmatch(r"[A-Za-z0-9._+/*?-]{1,128}", e["file"]) or not e["evidence"].strip():
                out.append(f"{rid}: malformed firmware requirement entry")
        files = [e["file"] for e in entries]
        if len(files) != len(set(files)):
            out.append(f"{rid}: duplicate firmware requirement entry")
    return out


@check
def profile_covers_the_production_plan_and_matches_evidence():                  # 7; firmware 13, 14
    problems = profile_problems()
    expect(not problems, f"profile drift: {problems}")
    expect(hv.profile_digest() == resolver.digest(hv.PROFILE), "profile digest")
    # drift in either direction is a failure (the embedded list is derived, never a source of truth)
    for label, mutate in (("module removed", lambda p: p["firmware_rules"]["package.firmware.iwlwifi"].pop()),
                          ("module added", lambda p: p["firmware_rules"]["package.firmware.iwlwifi"].append("zz")),
                          ("family added", lambda p: p["firmware_rules"].update({"package.firmware.x": ["x"]})),
                          ("policy for unknown family", lambda p: p["firmware_requirements"].update(
                              {"package.firmware.x": []})),
                          ("entry without evidence", lambda p: p["firmware_requirements"]["package.firmware.iwlwifi"]
                           .append({"file": "a.bin", "requirement": "required", "evidence": " "})),
                          ("bad requirement", lambda p: p["firmware_requirements"]["package.firmware.iwlwifi"]
                           .append({"file": "a.bin", "requirement": "maybe", "evidence": "x"}))):
        drifted = copy.deepcopy(hv.PROFILE)
        mutate(drifted)
        expect(profile_problems(drifted), f"drift not detected: {label}")
    policy = sum(len(v) for v in hv.PROFILE["firmware_requirements"].values())
    classes = len(hv.PROFILE["classifications"])
    return f"profile covers {classes} classifications and every package rule; firmware-family modules " \
           f"semantically equal the canonical mapping (6 drift kinds detected); firmware requirement policy: " \
           f"{policy} qualified entries (all requests unclassified by default)"


# ---------------------------------------------------------------------------
# Healthy families and status semantics

@check
def healthy_families_pass_with_device_attribution():                           # 1, 16, 21, 36
    seen = []
    for vendor, wifi, gpu in (("GenuineIntel", "intel", "intel"), ("AuthenticAMD", "realtek", "amd"),
                              ("GenuineIntel", "mediatek", "intel"), ("AuthenticAMD", "atheros", "amd")):
        inv = laptop(vendor, wifi, gpu)
        r = run(inv)
        expect(r["status"] == "PASS", f"{vendor}/{wifi}/{gpu}: {r['status']} "
               f"{[(c['id'], c['status'], c['reason']) for c in r['checks'] if c['status'] != 'PASS']}")
        ids = by_id(r)
        w = WIFI[wifi]["id"]
        for cid in (f"device.present:{w}", f"device.driver:{w}", f"device.firmware:{w}", f"net.wireless:{w}",
                    f"gpu.drm:{GPU[gpu]['id']}", f"bluetooth.controller:{BT['id']}",
                    f"cpu.microcode:{'intel-microcode' if vendor == 'GenuineIntel' else 'amd64-microcode'}"):
            expect(ids.get(cid, {}).get("status") == "PASS", f"{cid} not PASS")
        expect(ids[f"device.driver:{w}"]["identity"] == hv.identity_of(next(d for d in inv["devices"] if d["id"] == w)),
               "check not attributed to the inventory identity")
        expect(resolver.canonical_json(run(inv)) == resolver.canonical_json(r), "not deterministic")
        shuffled = copy.deepcopy(inv)
        shuffled["devices"].reverse()
        expect(resolver.canonical_json(run(shuffled, healthy(inv))) == resolver.canonical_json(r),
               "input order changes the result")
        seen.append(f"{vendor}/{wifi}/{gpu}")
    for name in ("05-ethernet-wifi", "06-usb-bluetooth", "07-nvme-storage", "02-amd-cpu-amd-gpu"):
        inv = fixture(name)
        r = run(inv, healthy(inv))
        expect(r["status"] == "PASS", f"{name}: {r['status']}")
        seen.append(name)
    return f"PASS with exact device attribution and canonical determinism: {', '.join(seen)}"


@check
def driver_device_and_firmware_failures_are_attributed():                       # 2, 3, 4, 5, 6, 7
    inv = laptop()
    w, g = WIFI["intel"]["id"], GPU["intel"]["id"]
    cases = []

    def perturb(fn):
        ev = healthy(inv)
        fn(ev)
        return run(inv, ev)
    r = perturb(lambda ev: ev["devices"][w].update(driver="rtw88_8822ce", modules=["rtw88_8822ce"]))
    expect(status_of(r, f"device.driver:{w}") == ("FAIL", "driver_mismatch") and r["status"] == "FAIL", "wrong driver")
    cases.append("wrong driver")
    r = perturb(lambda ev: ev["devices"][w].update(driver=None, modules=[]))
    expect(status_of(r, f"device.driver:{w}") == ("FAIL", "expected_driver_not_bound") and r["status"] == "FAIL",
           "missing driver")
    expect(status_of(r, f"device.firmware:{w}")[0] == "NOT_TESTED" and status_of(r, f"net.wireless:{w}")[0] ==
           "NOT_TESTED", "dependent checks not marked NOT_TESTED")
    cases.append("missing driver")
    r = perturb(lambda ev: ev["devices"][w].update(present=False, identity={}, driver=None, modules=[], net=[]))
    expect(status_of(r, f"device.present:{w}") == ("FAIL", "device_missing") and r["status"] == "FAIL", "missing device")
    cases.append("missing device")
    r = perturb(lambda ev: ev["devices"][w]["identity"].update(device_id="ffff"))
    expect(status_of(r, f"device.present:{w}") == ("FAIL", "device_identity_changed"), "identity change")
    cases.append("identity changed")
    # an unrelated (unresolved, unvalidated) device disappearing does not fail the target
    inv2 = copy.deepcopy(inv)
    stranger = {"id": "pci:0000:09:00.0", "bus": "pci", "locator": "0000:09:00.0", "vendor_id": "f0ff",
                "device_id": "f0ff", "class_id": "ff0000"}
    inv2["devices"] = sorted(inv2["devices"] + [stranger], key=lambda d: d["id"])
    ev = healthy(inv2)
    ev["devices"][stranger["id"]].update(present=False, identity={}, driver=None)
    r = run(inv2, ev)
    expect(r["status"] == "PASS" and stranger["id"] in r["unvalidated_targets"] and
           not any(c["target"] == stranger["id"] for c in r["checks"]), "unrelated device affected the result")
    cases.append("unrelated device ignored")
    r = perturb(lambda ev: ev["kernel_log"]["firmware_failures"].append({"device": w,
                                                                         "file": "iwlwifi-so-a0-gf-a0-89.ucode"}))
    c = by_id(r)[f"device.firmware:{w}"]
    expect((c["status"], c["reason"]) == ("LIMITED", "firmware_requirement_unclassified") and
           r["status"] == "LIMITED" and c["observed"]["unclassified_failed"] == ["iwlwifi-so-a0-gf-a0-89.ucode"],
           "target firmware failure (production policy: unclassified)")
    cases.append("target firmware failure (unclassified -> LIMITED)")
    r = perturb(lambda ev: ev["kernel_log"]["firmware_failures"].extend([
        {"device": g, "file": "i915/foo.bin"}, {"device": None, "file": "regulatory.db"},
        {"device": "pci:0000:09:00.0", "file": "x.bin"}]))
    expect(status_of(r, f"device.firmware:{w}") == ("PASS", "ok"), "unrelated firmware failure attributed to wifi")
    expect(status_of(r, f"device.firmware:{g}") == ("LIMITED", "firmware_requirement_unclassified"),
           "GPU failure not attributed")
    cases.append("unrelated firmware failure ignored for the target")
    r = perturb(lambda ev: ev["devices"][w].update(net=[{"wireless": False}]))
    expect(status_of(r, f"net.wireless:{w}") == ("FAIL", "expected_wireless_interface_missing"), "no wireless")
    r = perturb(lambda ev: ev["devices"][g].update(drm=0))
    expect(status_of(r, f"gpu.drm:{g}") == ("FAIL", "expected_drm_device_missing"), "no DRM node")
    r = perturb(lambda ev: ev["devices"][BT["id"]].update(bluetooth=0))
    expect(status_of(r, f"bluetooth.controller:{BT['id']}") == ("FAIL", "expected_controller_missing"), "no hci")
    r = perturb(lambda ev: ev["devices"][BT["id"]].update(modules=["btusb"]))
    expect(status_of(r, f"device.driver:{BT['id']}") == ("FAIL", "driver_mismatch"), "BT wrong driver")
    cases += ["interface", "drm", "bluetooth controller", "bluetooth driver"]
    return f"{len(cases)} cases: {', '.join(cases)}"


@contextlib.contextmanager
def requirement_policy(entries):
    """TEST-ONLY: temporarily qualify firmware requirements in the profile (production has none)."""
    saved = copy.deepcopy(hv.PROFILE["firmware_requirements"])
    for rid, lst in entries.items():
        hv.PROFILE["firmware_requirements"][rid] = lst
    try:
        yield
    finally:
        hv.PROFILE["firmware_requirements"].clear()
        hv.PROFILE["firmware_requirements"].update(saved)


@check
def firmware_requirement_semantics():                                          # firmware 1-13
    inv = laptop()
    w, g = WIFI["intel"]["id"], GPU["intel"]["id"]
    REQ, OPT, UNK = "iwlwifi-so-a0-gf-a0-89.ucode", "iwl-debug-yoyo.bin", "iwlwifi-unknown.pnvm"
    policy = {"package.firmware.iwlwifi": [
        {"file": "iwlwifi-so-a0-gf-a0-*.ucode", "requirement": "required", "evidence": "TEST-ONLY fixture"},
        {"file": "iwl-debug-yoyo.bin", "requirement": "optional", "evidence": "TEST-ONLY fixture"}]}

    def with_failures(files, device=None, fn=None):
        ev = healthy(inv)
        ev["kernel_log"]["firmware_failures"] = [{"device": device or w, "file": f} for f in files]
        if fn:
            fn(ev)
        return run(inv, ev)
    cases = []
    with requirement_policy(policy):
        r = with_failures([REQ])
        expect(status_of(r, f"device.firmware:{w}") == ("FAIL", "required_firmware_load_failed") and
               r["status"] == "FAIL" and by_id(r)[f"device.firmware:{w}"]["observed"]["required_failed"] == [REQ],
               "known-required failure")
        cases.append("required -> FAIL")
        r = with_failures([OPT])
        expect(status_of(r, f"device.firmware:{w}") == ("LIMITED", "optional_firmware_unavailable") and
               r["status"] == "LIMITED", "known-optional failure")
        cases.append("optional -> LIMITED")
        r = with_failures([UNK])
        expect(status_of(r, f"device.firmware:{w}") == ("LIMITED", "firmware_requirement_unclassified") and
               r["status"] == "LIMITED", "unknown requirement")
        r = with_failures([None])
        expect(status_of(r, f"device.firmware:{w}") == ("LIMITED", "firmware_requirement_unclassified"),
               "unnamed file must be unclassified")
        cases.append("unknown/unnamed -> LIMITED, never PASS or FAIL")
        r = with_failures([OPT, UNK, REQ])
        expect(status_of(r, f"device.firmware:{w}") == ("FAIL", "required_firmware_load_failed") and
               r["status"] == "FAIL", "a required failure was downgraded")
        r = with_failures([OPT, UNK])
        expect(status_of(r, f"device.firmware:{w}") == ("LIMITED", "firmware_requirement_unclassified"),
               "unclassified outranks optional")
        cases.append("required cannot be downgraded")
        r = with_failures([OPT], fn=lambda ev: ev["devices"][w].update(net=[]))
        expect(status_of(r, f"net.wireless:{w}") == ("FAIL", "expected_wireless_interface_missing") and
               r["status"] == "FAIL", "optional failure masked a missing interface")
        cases.append("optional does not mask missing interface")
        r = with_failures([UNK], fn=lambda ev: ev["devices"][w].update(driver="rtw88_8822ce",
                                                                        modules=["rtw88_8822ce"]))
        expect(status_of(r, f"device.driver:{w}") == ("FAIL", "driver_mismatch") and r["status"] == "FAIL",
               "unknown failure masked a wrong driver")
        r = with_failures([UNK], fn=lambda ev: ev["devices"][w].update(driver=None, modules=[]))
        expect(status_of(r, f"device.driver:{w}") == ("FAIL", "expected_driver_not_bound") and r["status"] == "FAIL",
               "unknown failure masked a missing driver")
        cases.append("unknown does not mask driver failures")
        ev = healthy(inv)
        ev["kernel_log"]["firmware_failures"] = [{"device": None, "file": REQ},
                                                 {"device": "pci:0000:09:00.0", "file": REQ}]
        expect(run(inv, ev)["status"] == "PASS", "unattributed or foreign required file affected the target")
        cases.append("unrelated failure ignored")
        r = with_failures([REQ], device=g)
        expect(status_of(r, f"device.firmware:{w}") == ("PASS", "ok") and
               status_of(r, f"device.firmware:{g}") == ("LIMITED", "firmware_requirement_unclassified"),
               "a wrong device's failure, or another family's policy, affected the result")
        cases.append("wrong device / other family cannot affect the target")
        ambiguous = {"package.firmware.iwlwifi": policy["package.firmware.iwlwifi"] + [
            {"file": "iwlwifi-*", "requirement": "optional", "evidence": "TEST-ONLY"}]}
    with requirement_policy(ambiguous):
        rejects("contract_failure", with_failures, [REQ])
    cases.append("ambiguous policy fails closed")
    # the caller cannot classify: no parameter, no evidence field, no CLI input
    ev = healthy(inv)
    ev["kernel_log"]["firmware_failures"] = [{"device": w, "file": REQ, "requirement": "optional"}]
    rejects("malformed_evidence", run, inv, ev)
    import inspect
    expect(list(inspect.signature(hv.evaluate).parameters) == ["inventory", "rules", "ev", "previous"] and
           list(inspect.signature(hv.firmware_requirement).parameters) == ["rules", "file"], "caller-controlled policy")
    cases.append("caller cannot supply a classification")
    # the policy is bound: digest changes, and an existing result becomes stale
    ev = healthy(inv)
    result = run(inv, ev)
    before = hv.profile_digest()
    with requirement_policy(policy):
        expect(hv.profile_digest() != before, "profile digest does not bind the firmware requirement policy")
        f = hv.freshness(result, inv, RULES, ev)
        expect(not f["current"] and f["changed"] == ["profile_digest"], f"policy change not stale: {f}")
    expect(hv.profile_digest() == before and hv.freshness(result, inv, RULES, ev)["current"], "policy restore")
    cases.append("policy change -> profile digest change -> stale")
    return f"{len(cases)} cases: {', '.join(cases)}"


@check
def limited_and_not_tested_are_never_pass():                                    # 8, 15, 31-35
    inv = laptop()
    w = WIFI["intel"]["id"]
    ev = healthy(inv)
    ev["kernel_log"] = {"available": False, "firmware_failures": [], "microcode": {"loader_lines": 0, "errors": 0}}
    r = run(inv, ev)
    expect(status_of(r, f"device.firmware:{w}") == ("LIMITED", "kernel_log_unavailable") and
           status_of(r, "cpu.microcode:intel-microcode") == ("LIMITED", "kernel_log_unavailable") and
           r["status"] == "LIMITED", "kernel log unavailable")
    expect({x["code"] for x in r["limitations"]} >= {"kernel_log_unavailable", "physical_function_not_exercised"},
           "limitations not listed")
    # FAIL outranks LIMITED
    ev["packages"]["audit_clean"] = False
    expect(run(inv, ev)["status"] == "FAIL", "FAIL does not outrank LIMITED")
    # VM microcode: LIMITED even with a perfect kernel log
    vm = laptop(virt="vm")
    r = run(vm)
    expect(status_of(r, "cpu.microcode:intel-microcode") == ("LIMITED", "virtualized_cpu") and r["status"] == "LIMITED",
           "VM microcode")
    ct = laptop(virt="container")
    expect(status_of(run(ct), "cpu.microcode:intel-microcode") == ("LIMITED", "container_platform"), "container")
    # same boot: microcode not yet authoritative
    ev = healthy(inv)
    ev["boot"]["boot_time"] = 500
    r = run(inv, ev)
    expect(r["bindings"]["boot"]["relation"] == "same_boot" and
           status_of(r, "cpu.microcode:intel-microcode") == ("LIMITED", "reboot_required"), "same boot")
    ev = healthy(inv)
    ev["kernel_log"]["microcode"] = {"loader_lines": 1, "errors": 1}
    expect(status_of(run(inv, ev), "cpu.microcode:intel-microcode") == ("FAIL", "microcode_load_failed"), "mc error")
    ev = healthy(inv)
    ev["kernel_log"]["microcode"] = {"loader_lines": 0, "errors": 0}
    expect(status_of(run(inv, ev), "cpu.microcode:intel-microcode") == ("LIMITED", "microcode_state_unreported"),
           "silent loader")
    # NVIDIA: never qualified, whatever is bound
    for name in ("03-nvidia-discrete-gpu", "04-intel-nvidia-hybrid"):
        inv3 = fixture(name)
        plan = plan_of(inv3)
        for t in targets_with(plan, "gpu.nvidia"):
            for drv, mods in (("nouveau", ["nouveau"]), ("nvidia", ["nvidia", "nvidia_drm"])):
                ev = healthy(inv3)
                ev["devices"][t].update(driver=drv, modules=mods)
                r = run(inv3, ev)
                expect(status_of(r, f"device.driver:{t}") == ("NOT_TESTED", "nvidia_not_qualified") and
                       r["status"] != "PASS", f"{name}: NVIDIA {drv} promoted")
                expect(f"gpu.drm:{t}" not in by_id(r), "NVIDIA DRM qualified")
    # aggregation table
    mk = lambda s, c: {"status": s, "criticality": c}  # noqa: E731
    for checks, want in (([mk("PASS", "critical")], "PASS"), ([mk("PASS", "critical"), mk("LIMITED", "critical")], "LIMITED"),
                         ([mk("NOT_TESTED", "advisory")], "LIMITED"), ([mk("FAIL", "advisory")], "LIMITED"),
                         ([mk("FAIL", "critical"), mk("LIMITED", "critical")], "FAIL"),
                         ([mk("FAIL", "critical"), mk("PASS", "advisory")], "FAIL")):
        expect(hv.aggregate(checks) == want, f"aggregate {checks} -> {hv.aggregate(checks)}")
    tampered = copy.deepcopy(run(inv))
    tampered["checks"][0].update(status="LIMITED", reason="observation_unavailable")
    rejects("malformed_result", hv.check_result, tampered)
    return "kernel log unavailable/VM/container/same-boot -> LIMITED; FAIL outranks LIMITED; NVIDIA NOT_TESTED; " \
           "aggregation table; inconsistent status rejected"


@check
def package_and_system_health():                                                # 9-14
    inv = laptop()
    cases = []

    def perturb(fn, cid, want, overall):
        ev = healthy(inv)
        fn(ev)
        r = run(inv, ev)
        expect(status_of(r, cid) == want and r["status"] == overall, f"{cid}: {status_of(r, cid)} {r['status']}")
        cases.append(want[1])
        return r
    perturb(lambda ev: ev["packages"]["installed"].remove(next(e for e in ev["packages"]["installed"]
                                                               if e["package"] == "firmware-iwlwifi")),
            "package.installed:firmware-iwlwifi", ("FAIL", "package_missing"), "FAIL")
    perturb(lambda ev: next(e for e in ev["packages"]["installed"] if e["package"] == "intel-microcode").update(
        status="install ok half-configured"), "package.installed:intel-microcode", ("FAIL", "package_missing"), "FAIL")
    r = perturb(lambda ev: ev.update(apply_journal={"attempt_id": "a" * 16, "transaction_digest": "sha256:" + "1" * 64,
                                                    "packages": {"intel-microcode": "3.2-1"}}),
                "package.installed:intel-microcode", ("FAIL", "package_version_mismatch"), "FAIL")
    expect(r["bindings"]["package_transaction"] == {"attempt_id": "a" * 16, "transaction_digest": "sha256:" + "1" * 64},
           "transaction identity not bound")
    r = perturb(lambda ev: ev.update(apply_journal={"attempt_id": "a" * 16, "transaction_digest": "sha256:" + "1" * 64,
                                                    "packages": {"intel-microcode": "1.0-1"}}),
                "package.installed:intel-microcode", ("PASS", "ok"), "PASS")
    expect(by_id(r)["package.installed:intel-microcode"]["observed"]["version_bound"] is True, "version binding")
    perturb(lambda ev: ev["packages"].update(audit_clean=False), "packages.dpkg_audit", ("FAIL", "dpkg_unhealthy"), "FAIL")
    perturb(lambda ev: ev["packages"].update(check="failed"), "packages.apt_check", ("FAIL", "apt_unhealthy"), "FAIL")
    perturb(lambda ev: ev["packages"].update(check="unavailable"), "packages.apt_check",
            ("LIMITED", "observation_unavailable"), "LIMITED")
    perturb(lambda ev: ev["system"].update(state="degraded", failed_units=["systemd-journald.service"]),
            "system.units.critical", ("FAIL", "system_critical_unit_failed"), "FAIL")
    r = perturb(lambda ev: ev["system"].update(state="degraded", failed_units=["cups.service", "user@*.service"]),
                "system.units.other", ("FAIL", "service_failed"), "LIMITED")
    expect(status_of(r, "system.units.critical") == ("PASS", "ok") and r["summary"]["advisory_failures"] == 1 and
           r["summary"]["critical_failures"] == 0, "unrelated unit not separated")
    perturb(lambda ev: ev["system"].update(state="maintenance"), "system.state", ("FAIL", "system_not_running"), "FAIL")
    perturb(lambda ev: ev["system"].update(state="starting"), "system.state", ("LIMITED", "system_starting"), "LIMITED")
    perturb(lambda ev: ev["storage"].update(read_only=True), "storage.root", ("FAIL", "root_read_only"), "FAIL")
    perturb(lambda ev: ev["storage"].update(block_visible=False), "storage.root",
            ("FAIL", "root_block_device_missing"), "FAIL")
    perturb(lambda ev: ev["storage"].update(fs_errors=3), "storage.root_errors", ("FAIL", "filesystem_errors"),
            "LIMITED")
    ev = healthy(inv, privilege="unprivileged")
    r = run(inv, ev)
    expect("apply_journal_unreadable" in {x["code"] for x in r["limitations"]}, "unprivileged journal limitation")
    return f"{len(cases)} package/system cases: {', '.join(sorted(set(cases)))}"


# ---------------------------------------------------------------------------
# Freshness, reboot and continuity

@check
def bindings_and_staleness():                                                   # 17-20, 38
    inv = laptop()
    ev = healthy(inv)
    r = run(inv, ev)
    b = r["bindings"]
    plan = plan_of(inv)
    expect(b["inventory_digest"] == plan["inventory_digest"] and b["rule_set_digest"] == plan["rule_set_digest"] and
           b["plan_digest"] == resolver.digest(plan) and b["boot"]["relation"] == "post_reboot" and
           b["installed_state_digest"] == hv.installed_state_digest(ev["packages"]["installed"]), "bindings")
    expect(hv.freshness(r, inv, RULES, ev) == {"current": True, "changed": [], "result_status": "PASS"}, "current")

    def stale(inv2, ev2, want):
        f = hv.freshness(r, inv2, RULES, ev2)
        expect(not f["current"] and want <= set(f["changed"]), f"stale {want}: {f}")
    ev2 = copy.deepcopy(ev)
    ev2["boot"]["boot_id_digest"] = "sha256:" + hashlib.sha256(b"boot-b").hexdigest()
    stale(inv, ev2, {"boot_id_digest"})
    inv2 = copy.deepcopy(inv)
    inv2["devices"] = sorted(inv2["devices"] + [{"id": "pci:0000:09:00.0", "bus": "pci", "locator": "0000:09:00.0",
                                                  "vendor_id": "f0ff", "device_id": "f0ff", "class_id": "ff0000"}],
                             key=lambda d: d["id"])
    stale(inv2, healthy(inv2), {"inventory_digest", "plan_digest"})
    inv3 = laptop(wifi="realtek")
    stale(inv3, healthy(inv3), {"plan_digest"})
    ev4 = copy.deepcopy(ev)
    next(e for e in ev4["packages"]["installed"] if e["package"] == "dpkg").update(version="1.22.22")
    stale(inv, ev4, {"installed_state_digest"})
    ev5 = copy.deepcopy(ev)
    next(e for e in ev5["packages"]["installed"] if e["package"] == "base-files").update(automatic=True)
    stale(inv, ev5, {"installed_state_digest"})
    old = copy.deepcopy(hv.PROFILE["critical_units"])
    hv.PROFILE["critical_units"].append("x.service")
    try:
        stale(inv, ev, {"profile_digest"})
    finally:
        hv.PROFILE["critical_units"][:] = old
    tampered = copy.deepcopy(r)
    tampered["bindings"]["plan_digest"] = "sha256:" + "2" * 64
    expect(not hv.freshness(tampered, inv, RULES, ev)["current"], "tampered binding accepted as current")
    return "inventory, rule set, plan, installed state (version and mark), boot and profile bound; any change -> stale"


@check
def post_reboot_continuity():                                                   # 4, 17
    inv = laptop()
    before = run(inv)
    after_ev = healthy(inv)
    after_ev["boot"]["boot_id_digest"] = "sha256:" + hashlib.sha256(b"boot-b").hexdigest()
    w = WIFI["intel"]["id"]
    r = run(inv, after_ev, previous=before)
    expect(r["mode"] == "revalidation" and r["bindings"]["previous"]["boot_changed"] is True and
           status_of(r, f"device.continuity:{w}") == ("PASS", "ok") and r["status"] == "PASS", "continuity PASS")
    # the device is gone after the reboot: the new inventory no longer lists it
    gone = copy.deepcopy(inv)
    gone["devices"] = [d for d in gone["devices"] if d["id"] != w]
    ev = healthy(gone)
    ev["boot"]["boot_id_digest"] = after_ev["boot"]["boot_id_digest"]
    r = run(gone, ev, previous=before)
    expect(status_of(r, f"device.continuity:{w}") == ("FAIL", "device_missing_after_reboot") and r["status"] == "FAIL",
           "device disappearance after reboot not detected")
    r = run(gone, healthy(gone), previous=before)
    expect(status_of(r, f"device.continuity:{w}") == ("FAIL", "device_missing"), "same-boot disappearance")
    other = copy.deepcopy(before)
    other["profile"]["digest"] = "sha256:" + "3" * 64
    rejects("stale_previous", run, inv, healthy(inv), other)
    return "revalidation: continuity PASS; device gone after reboot -> device_missing_after_reboot; " \
           "previous from another profile rejected"


# ---------------------------------------------------------------------------
# Evidence integrity

@check
def malformed_or_ambiguous_evidence_fails_closed():                             # 37, 39
    inv = laptop()
    w = WIFI["intel"]["id"]
    n = 0
    for mutate in (lambda ev: ev.pop("storage"), lambda ev: ev.update(extra=1),
                   lambda ev: ev["devices"][w].update(driver="bad driver!"),
                   lambda ev: ev["devices"][w].update(modules=["b", "a"]),
                   lambda ev: ev["devices"][w].update(drm=-1),
                   lambda ev: ev["devices"][w].update(net=[{"wireless": "yes"}]),
                   lambda ev: ev["devices"].update({"pci:bogus": ev["devices"][w]}),
                   lambda ev: ev["kernel_log"].update(available=False),
                   lambda ev: ev["kernel_log"]["firmware_failures"].append({"device": w, "file": "../../etc/shadow x"}),
                   lambda ev: ev["kernel_log"]["firmware_failures"].append(
                       {"device": w, "file": "a.bin", "line": "wlan0: authenticate with aa:bb:cc:dd:ee:ff"}),
                   lambda ev: ev["packages"]["installed"].append(copy.deepcopy(ev["packages"]["installed"][0])),
                   lambda ev: ev["system"].update(state="happy"),
                   lambda ev: ev["system"].update(failed_units=["rm -rf /"]),
                   lambda ev: ev["boot"].update(boot_id_digest="0123"),
                   lambda ev: ev.update(privilege="admin"),
                   lambda ev: ev["devices"].pop(w)):
        ev = healthy(inv)
        ev["kernel_log"]["firmware_failures"].append({"device": None, "file": None})
        mutate(ev)
        rejects("malformed_evidence", run, inv, ev)
        n += 1
    # two inventory devices resolving to one sysfs device: the collector refuses
    root = build_tree("ambiguous")
    inv2 = tree_inventory()
    dup = copy.deepcopy(inv2["devices"][0])
    dup.update(id="pci:0000:00:14.4", locator="0000:00:14.4")
    os.symlink(os.path.realpath(os.path.join(root, "sys/bus/pci/devices/0000:00:14.3")),
               os.path.join(root, "sys/bus/pci/devices/0000:00:14.4"))
    inv2["devices"] = sorted(inv2["devices"] + [dup], key=lambda d: d["id"])
    rejects("malformed_evidence", tree_runtime(root).collect, inv2, ["intel-microcode"])
    return f"{n + 1} malformed, duplicated, ambiguous or smuggling evidence documents rejected; nothing partially " \
           "trusted"


# ---------------------------------------------------------------------------
# Collector on a synthetic sysfs/procfs tree (privacy, no mutation, fixed commands)

WIFI_DEV = None


def tree_inventory():
    dev = copy.deepcopy(WIFI["intel"])
    dev.update(id="pci:0000:00:14.3", locator="0000:00:14.3", driver="iwlwifi", modules=["iwlwifi"])
    blk = {"id": "pci:0000:00:04.0", "bus": "pci", "locator": "0000:00:04.0", "vendor_id": "1af4",
           "device_id": "1042", "class_id": "010000", "driver": "virtio-pci", "modules": ["virtio_pci"]}
    return synthetic([dev, blk])


KERNEL_LOG = """Linux version 6.12.0-synthetic (debian-kernel@lists.debian.org) #1 SMP
microcode: Current revision: 0x0000002c
iwlwifi 0000:00:14.3: Direct firmware load for iwlwifi-so-a0-gf-a0-89.ucode failed with error -2
iwlwifi 0000:00:14.3: loaded firmware version 83.e8f84e98.0 so-a0-gf-a0-83.ucode op_mode iwlmvm
wlp0s20f3: authenticate with aa:bb:cc:dd:ee:ff (local address=02:11:22:33:44:55)
r8169 0000:03:00.0: Direct firmware load for rtl_nic/rtl8168h-2.fw failed with error -2
systemd[1]: Set hostname to <secret-host>.
usb 1-3: SerialNumber: SECRETSERIAL123
"""


def build_tree(name):
    root = os.path.join(TMP, name)
    sysd = os.path.join(root, "sys")
    for d in ("devices/pci0000:00/0000:00:14.3/net/wlp0s20f3", "devices/pci0000:00/0000:00:04.0/virtio1/block/vda/vda1",
              "bus/pci/drivers/iwlwifi", "bus/pci/drivers/virtio-pci", "module/iwlwifi", "module/virtio_pci",
              "bus/pci/devices", "bus/usb/devices", "class/net", "class/drm", "class/bluetooth", "dev/block",
              "fs/ext4/vda1"):
        os.makedirs(os.path.join(sysd, d), exist_ok=True)
    wifi = os.path.join(sysd, "devices/pci0000:00/0000:00:14.3")
    blk = os.path.join(sysd, "devices/pci0000:00/0000:00:04.0")
    ident = hv.identity_of(WIFI["intel"])
    for attr, val in (("vendor", "0x" + ident["vendor_id"]), ("device", "0x" + ident["device_id"]),
                      ("subsystem_vendor", "0x" + ident["subsystem_vendor_id"]),
                      ("subsystem_device", "0x" + ident["subsystem_device_id"]), ("class", "0x" + ident["class_id"]),
                      ("address", "aa:bb:cc:dd:ee:ff")):
        pathlib.Path(wifi, attr).write_text(val + "\n")
    for attr, val in (("vendor", "0x1af4"), ("device", "0x1042"), ("class", "0x010000")):
        pathlib.Path(blk, attr).write_text(val + "\n")
    os.symlink("../../../bus/pci/drivers/iwlwifi", os.path.join(wifi, "driver"))
    os.symlink("../../../module/iwlwifi", os.path.join(sysd, "bus/pci/drivers/iwlwifi/module"))
    os.symlink("../../../bus/pci/drivers/virtio-pci", os.path.join(blk, "driver"))
    os.symlink("../../../module/virtio_pci", os.path.join(sysd, "bus/pci/drivers/virtio-pci/module"))
    os.symlink("../../../devices/pci0000:00/0000:00:14.3", os.path.join(sysd, "bus/pci/devices/0000:00:14.3"))
    os.symlink("../../../devices/pci0000:00/0000:00:04.0", os.path.join(sysd, "bus/pci/devices/0000:00:04.0"))
    os.makedirs(os.path.join(wifi, "net/wlp0s20f3/wireless"))
    pathlib.Path(wifi, "net/wlp0s20f3/address").write_text("aa:bb:cc:dd:ee:ff\n")
    os.symlink("../../devices/pci0000:00/0000:00:14.3/net/wlp0s20f3", os.path.join(sysd, "class/net/wlp0s20f3"))
    os.symlink("../../../0000:00:14.3", os.path.join(wifi, "net/wlp0s20f3/device"))
    os.makedirs(os.path.join(sysd, "devices/virtual/net/lo"))
    os.symlink("../../devices/virtual/net/lo", os.path.join(sysd, "class/net/lo"))
    os.symlink("../../devices/pci0000:00/0000:00:04.0/virtio1/block/vda/vda1", os.path.join(sysd, "dev/block/254:1"))
    pathlib.Path(sysd, "fs/ext4/vda1/errors_count").write_text("0\n")
    proc = os.path.join(root, "proc")
    os.makedirs(os.path.join(proc, "self"))
    os.makedirs(os.path.join(proc, "sys/kernel/random"))
    pathlib.Path(proc, "self/mountinfo").write_text(
        "22 1 254:1 / / rw,relatime shared:1 - ext4 /dev/vda1 rw,errors=remount-ro\n"
        "23 22 0:21 / /proc rw,nosuid shared:12 - proc proc rw\n")
    pathlib.Path(proc, "sys/kernel/random/boot_id").write_text("6f1c5a0e-0000-4000-8000-123456789abc\n")
    pathlib.Path(proc, "stat").write_text("cpu  1 2 3\nbtime 2000\n")
    info = os.path.join(root, "var/lib/dpkg/info")
    os.makedirs(info)
    for pkg in ("intel-microcode", "firmware-iwlwifi"):
        p = pathlib.Path(info, f"{pkg}.list")
        p.write_text("/.\n")
        os.utime(p, (1000, 1000))
    return root


class FakeRunner:
    def __init__(self, overrides=None):
        self.calls = []
        self.overrides = overrides or {}

    def __call__(self, argv):
        expect(tuple(argv) in hv.COMMANDS, f"collector ran a non-allowlisted command {argv}")
        self.calls.append(list(argv))
        key = argv[1] if argv[0] != hv.APT_GET else "check"
        if key in self.overrides:
            return self.overrides[key]
        if argv[0] == hv.JOURNALCTL:
            return 0, KERNEL_LOG.encode()
        if argv[:2] == [hv.SYSTEMCTL, "is-system-running"]:
            return 1, b"degraded\n"
        if argv[:2] == [hv.SYSTEMCTL, "list-units"]:
            return 0, b"user@1000.service loaded failed failed User Manager for UID 1000\n" \
                      b"cups.service loaded failed failed CUPS\n"
        if argv[0] == hv.DPKG:
            return 0, b""
        if argv[0] == hv.APT_GET:
            return 0, b"Reading package lists...\n"
        if argv[0] == hv.DPKG_QUERY:
            return 0, ("base-files\tamd64\t13.8\tinstall ok installed\n"
                       "intel-microcode\tamd64\t3.20251111.1~deb13u1\tinstall ok installed\n"
                       "firmware-iwlwifi\tall\t20250410-2\tinstall ok installed\n"
                       "iucode-tool\tamd64\t2.3.1-3\tinstall ok installed\n"
                       "old-thing\tamd64\t1\tdeinstall ok config-files\n").encode()
        if argv[0] == hv.APT_MARK:
            return 0, b"iucode-tool\n"
        raise Failure(f"unexpected command {argv}")


def tree_runtime(root, runner=None, euid=1000):
    return hv.RealRuntime(sys_root=os.path.join(root, "sys"), proc_root=os.path.join(root, "proc"), root=root,
                          runner=runner or FakeRunner(), euid=lambda: euid)


def snapshot(root):
    out = {}
    for d, dirs, files in os.walk(root):
        for n in dirs + files:
            p = os.path.join(d, n)
            st = os.lstat(p)
            out[p] = (st.st_mode, st.st_size, st.st_mtime_ns, os.readlink(p) if os.path.islink(p) else None)
    return out


@check
def collector_normalizes_redacts_and_never_mutates():                          # 22, 27, 28
    root = build_tree("collector")
    inv = tree_inventory()
    before = snapshot(root)
    runner = FakeRunner()
    ev = tree_runtime(root, runner).collect(inv, ["firmware-iwlwifi", "intel-microcode"])
    expect(snapshot(root) == before, "collector modified the tree")
    w = "pci:0000:00:14.3"
    expect(ev["devices"][w] == {"present": True, "identity": hv.identity_of(WIFI["intel"]), "driver": "iwlwifi",
                                "modules": ["iwlwifi"], "net": [{"wireless": True}], "drm": 0, "bluetooth": 0},
           f"wifi evidence {ev['devices'][w]}")
    expect(ev["kernel_log"]["firmware_failures"] == [{"device": None, "file": "rtl_nic/rtl8168h-2.fw"},
                                                     {"device": w, "file": "iwlwifi-so-a0-gf-a0-89.ucode"}],
           f"firmware attribution {ev['kernel_log']['firmware_failures']}")
    expect(ev["kernel_log"]["microcode"] == {"loader_lines": 1, "errors": 0}, "microcode loader")
    expect(ev["storage"] == {"mounted": True, "fstype": "ext4", "read_only": False, "block_visible": True,
                             "device": "pci:0000:00:04.0", "fs_errors": 0}, f"storage {ev['storage']}")
    expect(ev["system"] == {"state": "degraded", "failed_units": ["cups.service", "user@*.service"]}, "units")
    expect(ev["packages"]["install_times"] == {"firmware-iwlwifi": 1000, "intel-microcode": 1000} and
           ev["packages"]["check"] == "ok" and ev["packages"]["audit_clean"], "packages")
    expect(next(e for e in ev["packages"]["installed"] if e["package"] == "old-thing")["status"] ==
           "deinstall ok config-files", "installed-state rows differ from the transaction's definition")
    expect(next(e for e in ev["packages"]["installed"] if e["package"] == "iucode-tool")["automatic"], "marks")
    expect(ev["privilege"] == "unprivileged" and ev["apply_journal"] is None, "privilege")
    r = hv.evaluate(inv, RULES, ev)
    blob = resolver.canonical_json({"ev": ev, "result": r}).decode()
    for secret in ("aa:bb:cc:dd:ee:ff", "02:11:22:33:44:55", "secret-host", "SECRETSERIAL", "wlp0s20f3", "1000.service",
                   "6f1c5a0e", "/sys/", "/proc/", "vda1", "debian-kernel@"):
        expect(secret not in blob, f"evidence/result leaks {secret!r}")
    expect(re.search(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", blob) is None, "MAC-shaped value in output")
    expect(status_of(r, f"device.firmware:{w}") == ("LIMITED", "firmware_requirement_unclassified"),
           "collected failure not attributed")
    expect(status_of(r, "system.units.other") == ("FAIL", "service_failed"), "units")
    # kernel log unavailable to an unprivileged user (dmesg_restrict / journal permissions): LIMITED, not weakened
    ev2 = tree_runtime(root, FakeRunner({"--dmesg": (0, b"No journal files were opened due to insufficient "
                                                         b"permissions.\n")})).collect(inv, ["intel-microcode"])
    expect(ev2["kernel_log"] == {"available": False, "firmware_failures": [], "microcode": {"loader_lines": 0,
                                                                                            "errors": 0}}, "kernel log")
    ev3 = tree_runtime(root, FakeRunner({"check": (100, b"E: Could not open lock file - open (13: Permission "
                                                        b"denied)\n")})).collect(inv, [])
    expect(ev3["packages"]["check"] == "unavailable", "apt check permission")
    ev4 = tree_runtime(root, FakeRunner({"check": (100, b"E: Unmet dependencies.\n")})).collect(inv, [])
    expect(ev4["packages"]["check"] == "failed", "apt check failure")
    # sanitized environment, no shell, fixed argv
    calls = []

    class R:
        returncode, stdout, stderr = 0, b"", b""
    real = hv.subprocess.run
    hv.subprocess.run = lambda argv, **kw: (calls.append((argv, kw)), R())[1]
    os.environ.update(LD_PRELOAD="/tmp/evil.so", SYSTEMD_PAGER="sh -c evil", PATH="/tmp/evil:" + os.environ["PATH"])
    try:
        for argv in hv.COMMANDS:
            hv.run_command(list(argv))
    finally:
        hv.subprocess.run = real
        os.environ.pop("LD_PRELOAD")
        os.environ["SYSTEMD_PAGER"] = ""
        os.environ.pop("SYSTEMD_PAGER")
        os.environ["PATH"] = os.environ["PATH"].split(":", 1)[1]
    expect(len(calls) == len(hv.COMMANDS) and all(kw["shell"] is False and kw["env"] == hv.COMMAND_ENV and
                                                   kw["stdin"] == hv.subprocess.DEVNULL for _a, kw in calls),
           "commands not run with shell=False and the literal environment")
    for bad in (["/usr/bin/apt-get", "install", "x"], ["/usr/bin/dpkg", "-i", "x.deb"], ["/bin/sh", "-c", "id"],
                ["/usr/bin/systemctl", "restart", "x"], ["/usr/bin/journalctl", "--vacuum-size=1"],
                [hv.SYSTEMCTL, "is-system-running", "--wait"]):
        rejects("contract_failure", hv.run_command, bad)
    return "normalized (driver, module, net, block, units, marks, install times), redacted (no MAC, serial, " \
           "hostname, interface, uid, boot_id, paths), tree unchanged, fixed argv, shell=False, literal env"


@check
def cli_accepts_no_caller_input():                                              # 23-26
    root = build_tree("cli")
    inv = tree_inventory()

    class P:
        def inventory(self):
            return copy.deepcopy(inv)
    out = io.BytesIO()

    class Buf:
        buffer = out
    saved = sys.stdout
    sys.stdout = Buf()
    try:
        rc = cli.main(["validate"], runtime_factory=lambda: tree_runtime(root), probe_factory=P)
    finally:
        sys.stdout = saved
    doc = json.loads(out.getvalue())
    expect(rc == hv.OVERALL_EXIT[doc["status"]] and doc["schema"] == hv.RESULT_SCHEMA_ID, f"CLI validate rc {rc}")
    for argv in (["validate", "--check", "x"], ["validate", "/etc/passwd"], ["validate", "--driver=iwlwifi"],
                 ["validate", "pci:0000:00:14.3"], ["validate", "--package", "nvidia-driver"], ["validate", "--exec",
                 "sh"], ["evidence", "--root", "/tmp"], ["check-current", "x.json"], [], ["repair"], ["validate", ""]):
        with open(os.devnull, "w") as null, contextlib.redirect_stderr(null):
            expect(cli.main(argv, runtime_factory=lambda: (_ for _ in ()).throw(Failure("ran")),
                            probe_factory=P) == 64, f"CLI accepted {argv}")
    import inspect
    expect(list(inspect.signature(hv.evaluate).parameters) == ["inventory", "rules", "ev", "previous"],
           "evaluate signature grew a caller-controlled parameter")
    return "only validate/revalidate/check-current/evidence; device ids, paths, drivers, packages, checks and " \
           "commands are refused (exit 64) before anything runs"


@check
def module_is_read_only_and_installs_nothing():                                 # 26, 28-31
    for path in (HARDWARE / "hardware_validation.py", HARDWARE / "validate-hardware.py"):
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.add(node.module)
            elif isinstance(node, ast.Call):
                name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                owner = getattr(getattr(node.func, "value", None), "id", None)
                expect(not (owner == "os" and name in ("system", "popen", "execv", "execve", "unlink", "remove",
                                                       "rename", "replace", "rmdir", "mkdir", "makedirs", "chmod",
                                                       "chown", "symlink", "link", "truncate", "utime", "setuid",
                                                       "seteuid", "putenv")),
                       f"{path.name} calls os.{name}()")
                expect(name not in ("Popen", "check_output", "check_call", "call", "rmtree", "write_text",
                                    "write_bytes"), f"{path.name} calls {name}()")
                if name == "open":
                    mode = node.args[1].value if len(node.args) > 1 and isinstance(node.args[1], ast.Constant) else \
                        next((k.value.value for k in node.keywords if k.arg == "mode"), "r")
                    expect(set(str(mode)) <= {"r", "b"}, f"{path.name} opens a file for writing ({mode})")
            elif isinstance(node, ast.keyword) and node.arg == "shell":
                expect(isinstance(node.value, ast.Constant) and node.value.value is False, "shell=True")
        expect(imports <= {"fnmatch", "hashlib", "os", "re", "stat", "subprocess", "resolver", "json", "jsonschema", "sys",
                           "hardware_validation", "probe"},
               f"{path.name} imports {sorted(imports)}")
        for bad in (r'"install"', r'"--configure"', r'"-i"', r"apt-get install", r"source_activation", r"package_apply",
                    r"package_staging", r"\bactivate", r"update-initramfs", r"modprobe", r"/bind\b", r"echo .*>"):
            expect(not re.search(bad, src), f"{path.name} contains {bad}")
    expect(all(c[0] in (hv.SYSTEMCTL, hv.JOURNALCTL, hv.DPKG, hv.DPKG_QUERY, hv.APT_GET, hv.APT_MARK)
               for c in hv.COMMANDS), "command set")
    expect(set(hv.COMMANDS) == {(hv.SYSTEMCTL, "is-system-running"),
                                (hv.SYSTEMCTL, "list-units", "--failed", "--plain", "--no-legend", "--no-pager",
                                 "--full"),
                                (hv.JOURNALCTL, "--dmesg", "--boot=0", "--output=cat", "--no-pager", "--quiet"),
                                (hv.DPKG, "--audit"), (hv.APT_GET, "check", "--quiet"),
                                (hv.DPKG_QUERY, "-W", "-f", "${Package}\\t${Architecture}\\t${Version}\\t${Status}\\n"),
                                (hv.APT_MARK, "showauto")}, "read-only command allowlist changed")
    return "no write/unlink/mkdir/chmod/exec/shell, imports limited, no install/configure/activation/modprobe; " \
           "7 fixed read-only commands"


def main():
    sys.stdout.reconfigure(line_buffering=True)
    from importlib.metadata import version
    print(f"python {sys.version.split()[0]}, jsonschema {version('jsonschema')}")
    global TMP
    failures = 0
    old = os.umask(0o022)
    try:
        with tempfile.TemporaryDirectory(prefix="lubarchy-gate13-") as tmp:
            TMP = os.path.realpath(tmp)
            for fn in CHECKS:
                try:
                    detail = fn()
                except Exception as e:  # noqa: BLE001 - every failure is reported
                    failures += 1
                    print(f"FAIL: {fn.__name__}: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
                    continue
                print(f"ok: {fn.__name__}" + (f": {detail}" if detail else ""))
    finally:
        os.umask(old)
    if failures:
        print(f"FAIL: hardware post-install validation ({failures} of {len(CHECKS)} checks failed)", file=sys.stderr)
        return 1
    print(f"PASS: hardware post-install validation ({len(CHECKS)} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
