#!/usr/bin/python3
"""LUBARCHY M1-E device firmware mapping gate (synthetic only).

Checks the qualified device-firmware mappings:

- committed module-alias evidence is normalized and self-consistent;
- the committed production firmware rules regenerate byte-for-byte from that
  evidence and tools/hwe/firmware-mappings.json, with no cross-package
  overlap (drift fails; nothing is rewritten);
- every committed alias, instantiated as a concrete device, resolves to
  exactly its family's package through the production resolver;
- near-miss devices get nothing; deferred families stay inactive;
- Wi-Fi and graphics classification boundaries;
- production/reference parity on the firmware fixtures.

The evidence used here is the committed, normalized extraction from the
qualified Debian kernel. This gate never reads the CI runner's
/lib/modules; live requalification belongs to the Debian builder
(tools/hwe/qualify-firmware-mappings.py check-evidence).
"""

import sys

sys.dont_write_bytecode = True

import ast  # noqa: E402
import copy  # noqa: E402
import importlib.util  # noqa: E402
import json  # noqa: E402
import pathlib  # noqa: E402
import random  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402

import hwe_reference as ref  # noqa: E402

TESTS = pathlib.Path(__file__).resolve().parent
REPO = ref.REPO_ROOT
HARDWARE = REPO / "hardware"
FIXTURES = TESTS / "fixtures" / "hardware" / "firmware"
TOOL = REPO / "tools" / "hwe" / "qualify-firmware-mappings.py"

sys.path.insert(0, str(HARDWARE))
import resolver  # noqa: E402

sys.path.remove(str(HARDWARE))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


qualify = _load("lubarchy_qualify_firmware", TOOL)

PRODUCT_SOURCE_POLICY = ("main", "non-free-firmware")
FAMILIES = {
    "package.firmware.iwlwifi": ("firmware-iwlwifi", ("iwl3945", "iwl4965", "iwlwifi"), ["network.wifi"]),
    "package.firmware.intel-graphics": ("firmware-intel-graphics", ("i915", "xe"), []),
    "package.firmware.amd-graphics": ("firmware-amd-graphics", ("amdgpu", "radeon"), []),
    "package.firmware.brcm80211": ("firmware-brcm80211", ("brcmfmac",), ["network.wifi"]),
}
EXPECTED_COUNTS = {"iwlwifi": 444, "iwl3945": 6, "iwl4965": 2, "i915": 365, "xe": 115, "amdgpu": 311,
                   "radeon": 699, "brcmfmac": 36, "brcmsmac": 0}
PRODUCTION_PACKAGES = {"intel-microcode", "amd64-microcode", "firmware-iwlwifi", "firmware-intel-graphics",
                       "firmware-amd-graphics", "firmware-brcm80211"}
DEFERRED_PACKAGES = {"firmware-realtek", "firmware-atheros", "bluez-firmware", "firmware-nvidia-graphics",
                     "firmware-nvidia-gsp", "firmware-intel-sound", "firmware-sof-signed", "firmware-mediatek",
                     "firmware-qcom-soc", "firmware-ath9k-htc", "nvidia-driver", "nvidia-kernel-dkms",
                     "nvidia-open-kernel-dkms", "firmware-b43-installer", "firmware-misc-nonfree"}


class Failure(Exception):
    pass


def expect(cond, message):
    if not cond:
        raise Failure(message)


CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


def evidence():
    return qualify._strict_json(qualify.EVIDENCE_PATH)


def config():
    return qualify.load_config()


def production_rules():
    return resolver.load_production_rules()


def produce(inv, rules=None):
    return resolver.check_plan(resolver.resolve(inv, production_rules() if rules is None else rules))


def fixture(name):
    return ref.load_json(FIXTURES / f"{name}.json")


def packages_by_target(plan):
    """target -> set of firmware packages, from matched package.firmware rules."""
    rule_pkg = {rid: pkg for rid, (pkg, _, _) in FAMILIES.items()}
    out = {}
    for m in plan["matched_rules"]:
        if m["id"] in rule_pkg:
            for t in m["targets"]:
                out.setdefault(t, set()).add(rule_pkg[m["id"]])
    return out


def classes(plan):
    return {c["name"]: c["targets"] for c in plan["classifications"]}


def instantiate(alias, cls):
    """Concrete modalias string for a committed alias; '*' fields get
    synthetic values (class fields from cls)."""
    bus, values = qualify.parse_alias(alias)
    keys = qualify.FIELDS[bus]
    filler = {"d": "0000F0F0", "sv": "0000F0E0", "sd": "0000F0E1", "bc": cls[0:2], "sc": cls[2:4], "i": cls[4:6],
              "d_usb": "0001", "dc": "00", "dsc": "00", "dp": "00", "ic": "FF", "isc": "02", "ip": "FF", "in": "00"}
    out = []
    for k, v in zip(keys, values):
        if not v.endswith("*"):
            out.append(v)
            continue
        key = "d_usb" if (bus, k) == ("usb", "d") else k
        full = filler[key]
        prefix = v[:-1]
        out.append(prefix + full[len(prefix):] if full.startswith(prefix) else prefix + "0" * (len(full) - len(prefix)))
    return bus + ":" + "".join(k + v for k, v in zip(keys, out))


def device_for(index, modalias):
    if modalias.startswith("pci:"):
        f = qualify.parse_alias(modalias)[1]
        loc = "0000:%02x:%02x.%d" % (1 + index // 256, (index // 8) % 32, index % 8)
        return {"id": "pci:" + loc, "bus": "pci", "locator": loc, "vendor_id": f[0][-4:].lower(),
                "device_id": f[1][-4:].lower(), "subsystem_vendor_id": f[2][-4:].lower(),
                "subsystem_device_id": f[3][-4:].lower(), "class_id": (f[4] + f[5] + f[6]).lower(),
                "modalias": modalias}
    f = qualify.parse_alias(modalias)[1]
    loc = "%d-%d" % (1 + index // 100, 1 + index % 100)
    return {"id": "usb:" + loc, "bus": "usb", "locator": loc, "vendor_id": f[0].lower(), "product_id": f[1].lower(),
            "interfaces": [{"number": "00", "class": f[6].lower(), "subclass": f[7].lower(), "protocol": f[8].lower(),
                            "modalias": modalias}]}


def inventory(devices, vendor="SyntheticCPU"):
    return {"schema": ref.INVENTORY_SCHEMA_ID,
            "system": {"architecture": "amd64", "kernel_release": "6.12.0-synthetic-amd64", "secure_boot": "disabled",
                       "boot_firmware": "uefi", "virtualization": {"kind": "none"}},
            "cpu": {"architecture": "x86_64", "vendor": vendor}, "devices": devices}


# ---------------------------------------------------------------------------
# Evidence and regeneration


@check
def evidence_is_normalized_and_qualified():
    ev, cfg = evidence(), config()
    qualify.check_evidence_integrity(ev, cfg)
    expect(ev["kernel_release"] == "6.12.111+deb13-amd64", f"kernel {ev['kernel_release']}")
    expect(ev["kernel_package"] == "linux-image-6.12.111+deb13-amd64" and ev["kernel_package_version"] == "6.12.111-1",
           "kernel package provenance")
    counts = {m: e["count"] for m, e in ev["modules"].items()}
    expect(counts == EXPECTED_COUNTS, f"alias counts {counts}")
    for m, e in ev["modules"].items():
        for a in e["aliases"]:
            expect(a.split(":", 1)[0] in ("pci", "usb"), f"{m}: non-representable alias {a}")
    expect(sorted(ev["excluded"]) == ["brcmfmac", "brcmsmac", "iwl4965"], f"excluded {sorted(ev['excluded'])}")
    expect(all(a.startswith("sdio:") for a in ev["excluded"]["brcmfmac"]) and len(ev["excluded"]["brcmfmac"]) == 23,
           "brcmfmac SDIO exclusion")
    expect(ev["excluded"]["brcmsmac"] == ["bcma:m04BFid0812rev11cl*", "bcma:m04BFid0812rev17cl*", "bcma:m04BFid0812rev18cl*"],
           "brcmsmac BCMA exclusion")
    expect(ev["excluded"]["iwl4965"] == ["iwl4965"], "iwl4965 non-bus alias exclusion")
    expect(cfg["excluded_modules"].keys() == {"brcmsmac"}, "brcmsmac must be the only excluded module")
    return f"kernel linux-image-6.12.111+deb13-amd64 6.12.111-1; {sum(counts.values())} PCI/USB aliases in 8 modules; SDIO/BCMA/non-bus excluded"


@check
def committed_rules_regenerate_byte_for_byte():
    problems = qualify.check_rules()
    expect(problems == [], f"drift: {problems}")
    gen = qualify.generate_rules(config(), evidence())
    expect(sorted(gen) == sorted(f"{rid}.json" for rid in FAMILIES), f"generated {sorted(gen)}")
    return "4 firmware rules == regenerated from committed evidence and configuration"


@check
def drift_is_detected():
    cfg = config()
    base = evidence()
    with tempfile.TemporaryDirectory() as tmp:
        rules_dir = pathlib.Path(tmp)
        for p in HARDWARE.glob("rules/package.firmware.*.json"):
            shutil.copy2(p, rules_dir / p.name)
        cases = []

        def evidence_case(label, mutate, error_ok=True):
            ev = copy.deepcopy(base)
            mutate(ev)
            try:
                problems = qualify.check_rules(cfg, ev, str(rules_dir))
            except qualify.QualificationError:
                problems = ["rejected"]
            expect(problems, f"undetected evidence drift: {label}")
            cases.append(label)

        def fix(ev, module):
            entry = ev["modules"][module]
            entry["count"] = len(entry["aliases"])
            entry["digest"] = qualify._digest(entry["aliases"])

        def drop(ev):
            ev["modules"]["iwlwifi"]["aliases"].pop(10)
            fix(ev, "iwlwifi")
        evidence_case("dropped alias", drop)

        def add_wrong_module(ev):
            ev["modules"]["iwlwifi"]["aliases"] = sorted(ev["modules"]["iwlwifi"]["aliases"]
                                                         + ["pci:v000010ECd0000C821sv*sd*bc*sc*i*"])
            fix(ev, "iwlwifi")
        evidence_case("alias from another module", add_wrong_module)
        evidence_case("unfixed count/digest", lambda ev: ev["modules"]["xe"]["aliases"].pop())

        def lowercase(ev):
            a = ev["modules"]["radeon"]["aliases"]
            i = next(n for n, x in enumerate(a) if any(c in "ABCDEF" for c in x.split("sv")[0]))
            expect(a[i].lower() != a[i], "case mutation must change the alias")
            a[i] = a[i].lower()
            a.sort()
            fix(ev, "radeon")
        evidence_case("changed alias case", lowercase)

        def duplicate(ev):
            a = ev["modules"]["amdgpu"]["aliases"]
            a.insert(1, a[0])
            fix(ev, "amdgpu")
        evidence_case("duplicate alias", duplicate)

        def sdio(ev):
            ev["modules"]["brcmfmac"]["aliases"] = sorted(ev["modules"]["brcmfmac"]["aliases"] + ["sdio:c*v02D0d4355*"])
            fix(ev, "brcmfmac")
        evidence_case("SDIO alias", sdio)

        def brcmsmac(ev):
            ev["modules"]["brcmsmac"]["aliases"] = ["pci:v000014E4d00004727sv*sd*bc*sc*i*"]
            fix(ev, "brcmsmac")
        evidence_case("alias for the excluded brcmsmac", brcmsmac)

        def broaden(ev):
            ev["modules"]["i915"]["aliases"] = sorted(ev["modules"]["i915"]["aliases"] + ["pci:v00008086d*sv*sd*bc*sc*i*"])
            fix(ev, "i915")
        evidence_case("vendor-wide Intel alias (cross-package overlap)", broaden)

        rule = rules_dir / "package.firmware.brcm80211.json"
        text = rule.read_text()
        rule.write_text(text.replace('"pci:v000014E4d00004354sv*sd*bc02sc80i*",\n', ""))
        expect(qualify.check_rules(cfg, base, str(rules_dir)), "edited committed rule not detected")
        cases.append("hand-edited committed rule")
        rule.write_text(text)
        (rules_dir / "package.firmware.realtek.json").write_text("{}\n")
        expect(qualify.check_rules(cfg, base, str(rules_dir)), "extra firmware rule file not detected")
        cases.append("extra firmware rule file")
    return f"{len(cases)} kinds of drift detected: " + ", ".join(cases)


@check
def alias_file_parser_is_strict():
    good = "# Aliases extracted from modules themselves.\nalias pci:v00008086d00000082sv*sd*bc*sc*i* iwlwifi\nalias usb:v0A5CpBD27d*dc*dsc*dp*ic*isc*ip*in* brcmfmac\nalias pci:v0000104Bd00008130sv*sd*bc*sc*i* BusLogic\n"
    bad = ["alias pci:v00008086d00000082sv*sd*bc*sc*i*\n", "alias  pci:x iwlwifi\n", "alias a b c\n",
           "alas pci:v1 iwlwifi\n", "alias pci:v1 iwl-wifi\n", "alias pci:v1\tiwlwifi\n", "softdep iwlwifi pre: x\n"]
    with tempfile.TemporaryDirectory() as tmp:
        p = pathlib.Path(tmp) / "modules.alias"
        p.write_text(good)
        table = qualify.read_module_aliases(str(p))
        expect(table["iwlwifi"] == {"pci:v00008086d00000082sv*sd*bc*sc*i*"} and "BusLogic" in table, "good file")
        for line in bad:
            p.write_text(good + line)
            try:
                qualify.read_module_aliases(str(p))
            except qualify.QualificationError:
                continue
            raise Failure(f"malformed line accepted: {line!r}")
    for alias in ("pci:v00008086d00000082sv*sd*bc*sc*i*", "usb:v0A5CpBD27d*dc*dsc*dp*ic*isc*ip*in*",
                  "pci:v00001002d*sv*sd*bc03sc00i00*"):
        expect(qualify.parse_alias(alias) is not None, f"rejected {alias}")
    for alias in ("sdio:c*v02D0d4355*", "bcma:m04BFid0812rev11cl*", "iwl4965", "pci:v00008086d00000082",
                  "pci:v00008086d0000008[0-2]sv*sd*bc*sc*i*", "pci:v0000808 6d*sv*sd*bc*sc*i*", "of:N*T*Cqcom"):
        expect(qualify.parse_alias(alias) is None, f"accepted unrepresentable alias {alias}")
    return f"{len(bad)} malformed alias lines rejected; SDIO, BCMA, non-bus and bracket aliases unrepresentable"


@check
def no_cross_package_overlap():
    ev, cfg = evidence(), config()
    fams = [(f["package"], sorted({a for m in f["modules"] for a in ev["modules"][m]["aliases"]})) for f in cfg["families"]]
    expect(qualify.overlap_report(fams) == [], "cross-package overlap")
    within = {}
    for f in cfg["families"]:
        if len(f["modules"]) > 1:
            sets = [set(ev["modules"][m]["aliases"]) for m in f["modules"]]
            within[f["package"]] = len(set.intersection(*sets)) if len(sets) == 2 else 0
    expect(qualify.aliases_overlap("pci:v00008086d*sv*sd*bc*sc*i*", "pci:v00008086d00000082sv*sd00001301bc*sc*i*"),
           "overlap detector missed a wildcard overlap")
    expect(not qualify.aliases_overlap("pci:v00001002d*sv*sd*bc03sc00i00*", "pci:v00001002d*sv*sd*bc04sc03i00*"),
           "overlap detector reported disjoint classes")
    return f"no cross-package overlap; same-package shared aliases: {within}"


# ---------------------------------------------------------------------------
# Production rules and policy


@check
def firmware_rules_are_exact():
    names = {p.name for p in HARDWARE.glob("rules/*.json")}
    expect({n for n in names if n.startswith("package.firmware.")} == {f"{r}.json" for r in FAMILIES},
           f"firmware rules {sorted(n for n in names if n.startswith('package.firmware.'))}")
    packages = set()
    for r in production_rules():
        for pkg in r["effects"].get("packages", []):
            packages.add(pkg["name"])
            expect(pkg["source"] == "non-free-firmware" and pkg["disposition"] == "require", f"{r['id']}: {pkg}")
            expect(set(pkg) == {"name", "disposition", "source", "reason"}, f"{r['id']}: extra package fields")
            expect(not any(c in pkg["name"] for c in "=<>:"), f"{r['id']}: version-like package name")
    expect(packages == PRODUCTION_PACKAGES, f"production package set {sorted(packages)}")
    expect(not packages & DEFERRED_PACKAGES, "deferred package active")
    for rid, (pkg, modules, cls) in FAMILIES.items():
        doc = ref.load_json(HARDWARE / "rules" / f"{rid}.json")
        expect([p["name"] for p in doc["effects"]["packages"]] == [pkg], f"{rid}: package")
        expect(doc["effects"].get("classifications", []) == cls, f"{rid}: classifications")
        for sel in doc["match"]:
            ((scope, cond),) = sel.items()
            expect(scope in ("pci", "usb") and list(cond) == ["modalias"], f"{rid}: selector {scope} {list(cond)}")
            for a in cond["modalias"]:
                expect(a.startswith(scope + ":"), f"{rid}: alias {a} under {scope}")
                expect(qualify.parse_alias(a) is not None, f"{rid}: unrepresentable {a}")
        text = json.dumps(doc)
        expect("sdio:" not in text and "bcma:" not in text, f"{rid}: SDIO/BCMA alias")
    return "4 firmware rules (modalias selectors only); exact production package set of 6; all non-free-firmware require; no pin"


@check
def every_committed_alias_maps_to_exactly_its_package():
    ev = evidence()
    rules = production_rules()
    total = 0
    for rid, (pkg, modules, cls) in FAMILIES.items():
        aliases = sorted({a for m in modules for a in ev["modules"][m]["aliases"]})
        fam_class = "028000" if cls else "030000"
        devices = [device_for(i, instantiate(a, fam_class)) for i, a in enumerate(aliases)]
        inv = inventory(devices)
        expect(ref.inventory_errors(inv) == [], f"{rid}: synthetic inventory invalid: {ref.inventory_errors(inv)[:3]}")
        plan = produce(inv, rules)
        by_target = packages_by_target(plan)
        for d in devices:
            expect(by_target.get(d["id"]) == {pkg}, f"{rid}: {d.get('modalias') or d['interfaces'][0]['modalias']} -> {by_target.get(d['id'])}")
        if cls:
            expect(set(classes(plan).get("network.wifi", [])) == {d["id"] for d in devices}, f"{rid}: Wi-Fi classification")
        total += len(devices)
    return f"{total} concrete devices (one per committed alias) each map to exactly their own family's package"


def _fixture_plans():
    return {p.stem: produce(fixture(p.stem)) for p in sorted(FIXTURES.glob("*.json"))}


@check
def fixture_families():
    plans = _fixture_plans()
    expect(sorted(plans) == ["fw-01-intel-iwlwifi", "fw-02-intel-i915", "fw-03-intel-xe", "fw-04-amd-amdgpu",
                             "fw-05-amd-radeon", "fw-06-brcmfmac-pci", "fw-07-brcmfmac-usb", "fw-08-near-misses"],
           f"fixtures {sorted(plans)}")
    want = {
        "fw-01-intel-iwlwifi": ({"pci:0000:03:00.0": {"firmware-iwlwifi"}}, "intel-microcode"),
        "fw-02-intel-i915": ({"pci:0000:00:02.0": {"firmware-intel-graphics"}}, "intel-microcode"),
        "fw-03-intel-xe": ({"pci:0000:00:02.0": {"firmware-intel-graphics"}}, "intel-microcode"),
        "fw-04-amd-amdgpu": ({"pci:0000:03:00.0": {"firmware-amd-graphics"}}, "amd64-microcode"),
        "fw-05-amd-radeon": ({"pci:0000:01:00.0": {"firmware-amd-graphics"}}, "amd64-microcode"),
        "fw-06-brcmfmac-pci": ({"pci:0000:02:00.0": {"firmware-brcm80211"}}, "intel-microcode"),
        "fw-07-brcmfmac-usb": ({"usb:1-4": {"firmware-brcm80211"}}, "intel-microcode"),
    }
    for name, (targets, microcode) in want.items():
        plan = plans[name]
        expect(packages_by_target(plan) == targets, f"{name}: {packages_by_target(plan)}")
        expect({p["name"] for p in plan["packages"]} == {microcode} | set().union(*targets.values()), f"{name}: packages")
        for p in plan["packages"]:
            expect(p["source"] == "non-free-firmware" and p["source_status"] == "authorised", f"{name}: {p}")
    c = classes(plans["fw-01-intel-iwlwifi"])
    expect(c["network.wifi"] == ["pci:0000:03:00.0"] and c["network.other"] == ["pci:0000:03:00.0"], "iwlwifi classes")
    expect(classes(plans["fw-07-brcmfmac-usb"])["network.wifi"] == ["usb:1-4"], "USB brcmfmac Wi-Fi")
    for name in ("fw-02-intel-i915", "fw-03-intel-xe"):
        expect(classes(plans[name])["gpu.intel"] == ["pci:0000:00:02.0"], f"{name}: gpu.intel")
    near = plans["fw-08-near-misses"]
    expect(near["packages"] == [] and near["sources"] == [], f"near misses got {near['packages']}")
    c = classes(near)
    expect("network.wifi" not in c, "near miss classified as Wi-Fi")
    expect(sorted(c["network.other"]) == ["pci:0000:03:00.0", "pci:0000:06:00.0", "pci:0000:08:00.0", "pci:0000:09:00.0"],
           f"0280 near misses {c.get('network.other')}")
    expect(c["gpu.intel"] == ["pci:0000:00:02.0"] and c["gpu.nvidia"] == ["pci:0000:0a:00.0"], "near-miss GPUs")
    return "7 qualified fixtures map to their package (+ CPU microcode); 9 near misses (Intel/AMD/Broadcom/BCMA/Realtek/Atheros/NVIDIA) get nothing"


@check
def classification_boundaries():
    rules = production_rules()
    # A firmware alias match must not create a GPU class: i915 alias on a non-display class.
    dev = device_for(0, instantiate("pci:v00008086d00000082sv*sd00001301bc*sc*i*", "088000"))
    gpu_alias = [a for a in evidence()["modules"]["amdgpu"]["aliases"] if "bc*" in a][0]
    dev2 = device_for(1, instantiate(gpu_alias, "048000"))
    plan = produce(inventory([dev, dev2]), rules)
    c = classes(plan)
    expect(packages_by_target(plan) == {dev["id"]: {"firmware-iwlwifi"}, dev2["id"]: {"firmware-amd-graphics"}},
           "firmware mapping by alias, independent of class")
    expect(not any(n.startswith("gpu.") for n in c), f"firmware rule created a GPU class: {c}")
    expect(c.get("network.wifi") == [dev["id"]] and "network.other" not in c, "Wi-Fi only via qualified alias")
    expect(not any("wifi" in cl for r in rules if not r["id"].startswith("package.firmware.")
                   for cl in r["effects"].get("classifications", [])), "a non-firmware rule emits Wi-Fi")
    return "Wi-Fi only from qualified iwlwifi/brcmfmac aliases; firmware rules never create gpu.*; 0280 stays network.other"


@check
def unknown_hardware_and_microcode_unchanged():
    inv = inventory([device_for(0, "pci:v0000F0FFd0000F0FFsv0000F0FFsd0000F1FFbcFFsc00i00")])
    plan = produce(inv)
    expect(plan["packages"] == [] and [u["target"] for u in plan["unresolved"]] == ["cpu", "pci:0000:01:00.0"],
           "unknown hardware")
    for vendor, pkg in (("GenuineIntel", "intel-microcode"), ("AuthenticAMD", "amd64-microcode")):
        plan = produce(inventory([], vendor))
        expect([p["name"] for p in plan["packages"]] == [pkg], f"{vendor} microcode")
    return "unknown device/CPU get nothing; Intel/AMD microcode unchanged"


@check
def parity_and_determinism():
    rules = production_rules()
    rnd = random.Random("lubarchy-m1e")
    n = 0
    for p in sorted(FIXTURES.glob("*.json")):
        inv = ref.load_json(p)
        want = resolver.canonical_json(resolver.resolve(inv, rules))
        expect(want == ref.canonical_json(ref.resolve(inv, rules, authorised_sources=PRODUCT_SOURCE_POLICY)),
               f"{p.stem}: production/reference parity")
        for _ in range(5):
            shuffled = copy.deepcopy(inv)
            rnd.shuffle(shuffled["devices"])
            r = list(rules)
            rnd.shuffle(r)
            expect(resolver.canonical_json(resolver.resolve(shuffled, r)) == want, f"{p.stem}: plan bytes changed")
        n += 1
    d1 = resolver.rule_set_digest(rules)
    expect(d1 == resolver.rule_set_digest(list(reversed(rules))) == ref.rule_set_digest(rules), "rule-set digest")
    return f"{n} firmware fixtures: production == reference bytes, stable under shuffles; rule-set digest {d1}"


@check
def no_runtime_rule_generation_or_install():
    for path in (HARDWARE / "resolver.py", HARDWARE / "resolve.py", HARDWARE / "probe.py"):
        text = path.read_text(encoding="utf-8")
        for word in ("modules.alias", "/lib/modules", "qualify-firmware", "firmware-mappings", "module-alias-evidence"):
            expect(word not in text, f"{path.name} references {word} (runtime rule generation)")
    tree = ast.parse(TOOL.read_text(encoding="utf-8"))
    imports = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imports |= {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    expect(imports == {"hashlib", "json", "os", "re", "sys"}, f"tool imports {imports}")
    strings = " ".join(n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str))
    for word in ("apt-get", "dpkg -i", "modprobe", "insmod", "subprocess"):
        expect(word not in strings, f"tool mentions {word}")
    return "resolver/probe never read kernel alias metadata; the qualification tool starts no process"


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
        print(f"FAIL: hardware firmware ({failures} of {len(CHECKS)} checks failed)", file=sys.stderr)
        return 1
    print(f"PASS: hardware firmware ({len(CHECKS)} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
