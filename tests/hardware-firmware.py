#!/usr/bin/python3
"""LUBARCHY device firmware mapping gate (M1-E, strengthened in M1-F; synthetic only).

Checks the qualified device-firmware mappings:

- committed firmware evidence (lubarchy.hardware.firmware-evidence/v2) is
  normalized and self-consistent; the M1-E v1 evidence still regenerates
  the M1-E rules byte-for-byte;
- the committed production firmware rules regenerate byte-for-byte from the
  evidence and tools/hwe/firmware-mappings.json (drift fails; nothing is
  rewritten);
- strengthened attribution: every second-wave module's declared firmware
  closure resolves to its family package and to no other; device-specific
  claims by other modules and foreign requests are reviewed or excluded; no
  relevant firmware file is shipped by two packages; no alias overlap
  between rules with different package or classification;
- every committed active alias, instantiated as a concrete device, resolves
  to exactly its family's package and classification;
- deferred families stay inactive; near misses and combo-card Bluetooth
  ownership negatives get nothing;
- production/reference parity on the firmware fixtures.

This gate uses only committed evidence and synthetic fixtures. It never
reads the CI runner's /lib/modules or hardware, and downloads nothing; live
requalification belongs to the Debian builder
(tools/hwe/qualify-firmware-mappings.py check-evidence).
"""

import sys

sys.dont_write_bytecode = True

import ast  # noqa: E402
import copy  # noqa: E402
import importlib.util  # noqa: E402
import pathlib  # noqa: E402
import random  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402

import hwe_reference as ref  # noqa: E402

TESTS = pathlib.Path(__file__).resolve().parent
REPO = ref.REPO_ROOT
HARDWARE = REPO / "hardware"
FIXTURES = TESTS / "fixtures" / "hardware" / "firmware"
V1_EVIDENCE = TESTS / "fixtures" / "hardware" / "firmware-evidence" / "m1e-evidence-v1.json"
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
M1E_FAMILIES = {
    "package.firmware.iwlwifi": ("firmware-iwlwifi", ("iwl3945", "iwl4965", "iwlwifi"), ["network.wifi"]),
    "package.firmware.intel-graphics": ("firmware-intel-graphics", ("i915", "xe"), []),
    "package.firmware.amd-graphics": ("firmware-amd-graphics", ("amdgpu", "radeon"), []),
    "package.firmware.brcm80211": ("firmware-brcm80211", ("brcmfmac",), ["network.wifi"]),
}
M1F_FAMILIES = {
    "package.firmware.realtek-wifi": ("firmware-realtek", (
        "rtl8188ee", "rtl8192ce", "rtl8192cu", "rtl8192de", "rtl8192ee", "rtl8192se", "rtl8723be", "rtl8821ae",
        "rtl8xxxu", "rtw88_8723de", "rtw88_8723du", "rtw88_8821ce", "rtw88_8821cu", "rtw88_8822be", "rtw88_8822bu",
        "rtw88_8822ce", "rtw88_8822cu", "rtw89_8851be", "rtw89_8852ae", "rtw89_8852be", "rtw89_8852bte",
        "rtw89_8852ce", "rtw89_8922ae"), ["network.wifi"]),
    "package.firmware.atheros-wifi": ("firmware-atheros", ("ar5523", "ath10k_pci", "ath6kl_usb", "wil6210"), ["network.wifi"]),
    "package.firmware.atheros-bluetooth": ("firmware-atheros", ("ath3k",), ["bluetooth.controller"]),
    "package.firmware.mediatek-wifi": ("firmware-mediatek", (
        "mt7601u", "mt7615e", "mt7663u", "mt76x0e", "mt76x0u", "mt76x2e", "mt76x2u", "mt7915e", "mt7921e", "mt7921u",
        "mt7925e", "mt7925u", "mt7996e", "rt2800pci", "rt2800usb", "rt61pci", "rt73usb"), ["network.wifi"]),
    "package.firmware.bluez-bcm203x": ("bluez-firmware", ("bcm203x",), ["bluetooth.controller"]),
}
FAMILIES = {**M1E_FAMILIES, **M1F_FAMILIES}
M1E_ALIAS_COUNT = 1722
EXISTING_PACKAGES = {"intel-microcode", "amd64-microcode", "firmware-iwlwifi", "firmware-intel-graphics",
                     "firmware-amd-graphics", "firmware-brcm80211"}
M1F_ALLOWLIST = {"firmware-realtek", "firmware-atheros", "firmware-mediatek", "bluez-firmware"}
NEVER_ACTIVE = {"firmware-nvidia-graphics", "firmware-nvidia-gsp", "firmware-intel-sound", "firmware-sof-signed",
                "firmware-qcom-soc", "firmware-ath9k-htc", "firmware-misc-nonfree", "firmware-b43-installer",
                "raspi-firmware", "nvidia-driver", "nvidia-kernel-dkms", "nvidia-open-kernel-dkms"}
DEFERRED_MODULES = {"brcmsmac", "r8169", "r8152", "r8153_ecm", "btusb", "btrtl", "btmtk", "btintel", "btbcm",
                    "hci_uart", "ath6kl_sdio", "rtw88_8723cs", "rtw88_8723ds", "rtw88_8821cs", "rtw88_8822bs",
                    "rtw88_8822cs", "ath11k_pci", "ath12k", "ath9k_htc", "carl9170", "p54usb", "r8192e_pci"}
EXCLUDED_ALIASES = {"pci:v000010ECd00008192sv*sd*bc*sc*i*", "usb:v050Dp7050d*dc*dsc*dp*ic*isc*ip*in*"}


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
    """target -> set of firmware packages from matched package.firmware rules."""
    rule_pkg = {rid: pkg for rid, (pkg, _, _) in FAMILIES.items()}
    out = {}
    for m in plan["matched_rules"]:
        if m["id"] in rule_pkg:
            for t in m["targets"]:
                out.setdefault(t, set()).add(rule_pkg[m["id"]])
    return out


def classes(plan):
    return {c["name"]: c["targets"] for c in plan["classifications"]}


def family_aliases(ev, modules):
    return sorted({a for m in modules for a in ev["modules"][m]["aliases"]} - EXCLUDED_ALIASES)


def instantiate(alias, cls):
    """Concrete modalias for a committed alias; '*' fields get synthetic
    values (PCI class fields from cls; USB interface 'ff' unless given)."""
    bus, values = qualify.parse_alias(alias)
    keys = qualify.FIELDS[bus]
    filler = {"d": "0000F0F0", "sv": "0000F0E0", "sd": "0000F0E1", "bc": cls[0:2], "sc": cls[2:4], "i": cls[4:6],
              "d_usb": "0001", "dc": "00", "dsc": "00", "dp": "00", "ic": "FF", "isc": "02", "ip": "FF", "in": "00"}
    out = []
    for k, v in zip(keys, values):
        if not v.endswith("*"):
            out.append(v)
            continue
        full = filler["d_usb" if (bus, k) == ("usb", "d") else k]
        prefix = v[:-1]
        out.append(prefix + full[len(prefix):] if full.startswith(prefix) else prefix + "0" * (len(full) - len(prefix)))
    return bus + ":" + "".join(k + v for k, v in zip(keys, out))


def device_for(index, modalias):
    f = qualify.parse_alias(modalias)[1]
    if modalias.startswith("pci:"):
        loc = "0000:%02x:%02x.%d" % (1 + index // 256, (index // 8) % 32, index % 8)
        return {"id": "pci:" + loc, "bus": "pci", "locator": loc, "vendor_id": f[0][-4:].lower(),
                "device_id": f[1][-4:].lower(), "subsystem_vendor_id": f[2][-4:].lower(),
                "subsystem_device_id": f[3][-4:].lower(), "class_id": (f[4] + f[5] + f[6]).lower(),
                "modalias": modalias}
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
# Evidence, migration and regeneration


@check
def evidence_is_normalized_and_qualified():
    ev, cfg = evidence(), config()
    qualify.check_evidence_integrity(ev, cfg)
    expect(ev["schema"] == "lubarchy.hardware.firmware-evidence/v2", f"schema {ev.get('schema')}")
    expect(ev["kernel_release"] == "6.12.111+deb13-amd64", f"kernel {ev['kernel_release']}")
    expect(ev["kernel_package"] == "linux-image-6.12.111+deb13-amd64" and ev["kernel_package_version"] == "6.12.111-1",
           "kernel package provenance")
    v1 = ref.load_json(V1_EVIDENCE)
    for m, entry in v1["modules"].items():
        expect(ev["modules"][m] == entry, f"{m}: M1-E alias evidence changed")
    for m, aliases in v1["excluded"].items():
        expect(ev["excluded"][m] == aliases, f"{m}: M1-E exclusions changed")
    for m, e in ev["modules"].items():
        for a in e["aliases"]:
            expect(a.split(":", 1)[0] in ("pci", "usb"), f"{m}: non-representable alias {a}")
    expect(all(a.startswith("sdio:") for a in ev["excluded"]["brcmfmac"]), "brcmfmac SDIO exclusion")
    expect(ev["excluded"]["brcmsmac"] == ["bcma:m04BFid0812rev11cl*", "bcma:m04BFid0812rev17cl*", "bcma:m04BFid0812rev18cl*"],
           "brcmsmac BCMA exclusion")
    expect(sorted(ev["packages"]) == sorted({f[0] for f in FAMILIES.values()}), f"package facts {sorted(ev['packages'])}")
    for p, facts in ev["packages"].items():
        expect(facts["section"] == "non-free-firmware/kernel" and len(facts["deb_sha256"]) == 64
               and len(facts["manifest_sha256"]) == 64 and facts["files"] > 0, f"{p}: package facts")
    counts = {m: e["count"] for m, e in ev["modules"].items()}
    return (f"v2 evidence, kernel linux-image-6.12.111+deb13-amd64 6.12.111-1; {len(counts)} modules, "
            f"{sum(counts.values())} PCI/USB aliases; M1-E module evidence identical to v1")


@check
def m1e_rules_regenerate_from_v1_evidence():
    cfg = config()
    m1e = copy.deepcopy(cfg)
    m1e["families"] = [f for f in cfg["families"] if f["rule_id"] in M1E_FAMILIES]
    v1 = ref.load_json(V1_EVIDENCE)
    qualify.check_evidence_integrity(v1, m1e)
    rules = qualify.generate_rules(m1e, v1)
    expect(sorted(rules) == sorted(f"{r}.json" for r in M1E_FAMILIES), f"rules {sorted(rules)}")
    for name, text in rules.items():
        expect((HARDWARE / "rules" / name).read_text(encoding="utf-8") == text, f"{name} differs from the M1-E output")
    return "the 4 M1-E rules regenerate byte-for-byte from the committed M1-E v1 evidence (migration safe)"


@check
def committed_rules_regenerate_byte_for_byte():
    problems = qualify.check_rules()
    expect(problems == [], f"problems: {problems}")
    gen = qualify.generate_rules(config(), evidence())
    expect(sorted(gen) == sorted(f"{rid}.json" for rid in FAMILIES), f"generated {sorted(gen)}")
    return f"{len(gen)} firmware rules == regenerated; attribution, collision and overlap checks pass"


@check
def strengthened_attribution():
    ev, cfg = evidence(), config()
    report = []
    for rid, (pkg, modules, _) in M1F_FAMILIES.items():
        family = next(f for f in cfg["families"] if f["rule_id"] == rid)
        expect(family.get("evidence_mode") == "declared-firmware-closure", f"{rid}: evidence mode")
        for m in modules:
            fw = ev["firmware"][m]
            owned = [r for r, o in fw["requests"].items() if pkg in o]
            foreign = [r for r, o in fw["requests"].items() if set(o) - {pkg}]
            expect(owned and not foreign, f"{m}: owned {len(owned)} foreign {foreign}")
            expect(fw["path"].startswith(("kernel/drivers/net/wireless/", "kernel/drivers/bluetooth/")), f"{m}: path {fw['path']}")
            bt = "bluetooth.controller" in M1F_FAMILIES[rid][2]
            expect(fw["path"].startswith("kernel/drivers/bluetooth/") == bt, f"{m}: classification vs module path")
        report.append(f"{pkg}:{len(modules)}")
    for rid, (pkg, modules, _) in M1E_FAMILIES.items():
        for m in modules:
            fw = ev["firmware"][m]
            foreign = [r for r, o in fw["requests"].items() if set(o) - {pkg}]
            expect(all(f"{m}|{r}" in cfg["reviewed_foreign_requests"] for r in foreign), f"{m}: unreviewed foreign {foreign}")
    expect(ev["firmware_file_collisions"] == [], f"firmware file collisions {ev['firmware_file_collisions']}")
    shared = {f"{m}|{o}": (len(v["aliases"]), v["packages"]) for m, d in ev["shared_claims"].items() for o, v in d.items()}
    expect(set(shared) == {"radeon|radeonfb", "rt73usb|p54usb", "rt73usb|rt2500usb", "rtl8192se|r8192e_pci"},
           f"shared claims {shared}")
    expect(qualify.attribution_problems(ev, cfg) == [], "attribution problems")
    return ("every second-wave module's declared firmware closure resolves only to its package (" + ", ".join(report)
            + "); 0 file collisions; shared claims reviewed or excluded: " + ", ".join(sorted(shared)))


@check
def drift_is_detected():
    cfg = config()
    base = evidence()
    with tempfile.TemporaryDirectory() as tmp:
        rules_dir = pathlib.Path(tmp)
        for p in HARDWARE.glob("rules/package.firmware.*.json"):
            shutil.copy2(p, rules_dir / p.name)
        cases = []

        def evidence_case(label, mutate, cfg_mutate=None):
            ev, c = copy.deepcopy(base), copy.deepcopy(cfg)
            mutate(ev)
            if cfg_mutate:
                cfg_mutate(c)
            try:
                problems = qualify.check_rules(c, ev, str(rules_dir))
            except qualify.QualificationError:
                problems = ["rejected"]
            expect(problems, f"undetected drift: {label}")
            cases.append(label)

        def fix(ev, module):
            entry = ev["modules"][module]
            entry["aliases"].sort()
            entry["count"] = len(entry["aliases"])
            entry["digest"] = qualify._digest(entry["aliases"])

        def add(module, alias):
            def f(ev):
                ev["modules"][module]["aliases"].append(alias)
                fix(ev, module)
            return f

        def drop(ev):
            ev["modules"]["rtw88_8822bu"]["aliases"].pop(3)
            fix(ev, "rtw88_8822bu")
        evidence_case("dropped alias", drop)
        evidence_case("foreign alias inserted", add("mt7921e", "pci:v000010ECd00008168sv*sd*bc*sc*i*"))
        evidence_case("SDIO alias", add("ath10k_pci", "sdio:c*v0271d0301*"))
        evidence_case("BCMA alias", add("rtl8192ce", "bcma:m04BFid0812rev11cl*"))
        evidence_case("vendor-wide Realtek alias", add("rtl8xxxu", "usb:v0BDAp*d*dc*dsc*dp*ic*isc*ip*in*"))
        evidence_case("unfixed count/digest", lambda ev: ev["modules"]["ar5523"]["aliases"].pop())

        def wrong_owner(ev):
            r = next(iter(r for r, o in ev["firmware"]["mt7921e"]["requests"].items() if o == ["firmware-mediatek"]))
            ev["firmware"]["mt7921e"]["requests"][r] = ["firmware-realtek"]
        evidence_case("firmware file owned by the wrong package", wrong_owner)

        def second_owner(ev):
            r = next(iter(r for r, o in ev["firmware"]["ath3k"]["requests"].items() if o))
            ev["firmware"]["ath3k"]["requests"][r] = ["firmware-atheros", "firmware-misc-nonfree"]
        evidence_case("firmware file shipped by two packages", second_owner)
        evidence_case("unexplained firmware-path collision",
                      lambda ev: ev["firmware_file_collisions"].append("ath10k/QCA6174/hw3.0/board-2.bin"))

        def unreviewed_claim(ev):
            ev["shared_claims"].setdefault("rt2800usb", {})["carl9170"] = {"aliases": [ev["modules"]["rt2800usb"]["aliases"][0]],
                                                                           "packages": ["firmware-linux-free"]}
        evidence_case("unexplained device claim by another firmware package", unreviewed_claim)
        evidence_case("cross-package alias overlap", add("ath3k", "usb:v0A5Cp2033d*dc*dsc*dp*ic*isc*ip*in*"))
        evidence_case("Bluetooth alias inside a Wi-Fi family", add("mt7921u", "usb:v0489pE027d*dc*dsc*dp*ic*isc*ip*in*"))
        evidence_case("excluded ambiguous alias re-admitted", lambda ev: None,
                      lambda c: c["excluded_aliases"].pop("usb:v050Dp7050d*dc*dsc*dp*ic*isc*ip*in*"))
        evidence_case("package outside the allowlist", lambda ev: None,
                      lambda c: c["families"][0].update(package="firmware-misc-nonfree"))

        rule = rules_dir / "package.firmware.realtek-wifi.json"
        text = rule.read_text()
        rule.write_text(text.replace('"pci:v000010ECd0000B852sv*sd*bc*sc*i*",\n', ""))
        expect(qualify.check_rules(cfg, base, str(rules_dir)), "edited committed rule not detected")
        cases.append("hand-edited committed rule")
        rule.write_text(text)
        (rules_dir / "package.firmware.realtek-ethernet.json").write_text("{}\n")
        expect(qualify.check_rules(cfg, base, str(rules_dir)), "extra firmware rule file not detected")
        cases.append("extra firmware rule file")
    return f"{len(cases)} kinds of drift detected: " + ", ".join(cases)


@check
def alias_and_modinfo_parsers_are_strict():
    good = "# Aliases extracted from modules themselves.\nalias pci:v00008086d00000082sv*sd*bc*sc*i* iwlwifi\nalias usb:v0A5CpBD27d*dc*dsc*dp*ic*isc*ip*in* brcmfmac\nalias pci:v0000104Bd00008130sv*sd*bc*sc*i* BusLogic\n"
    bad = ["alias pci:v00008086d00000082sv*sd*bc*sc*i*\n", "alias  pci:x iwlwifi\n", "alias a b c\n",
           "alas pci:v1 iwlwifi\n", "alias pci:v1 iwl-wifi\n", "alias pci:v1\tiwlwifi\n", "softdep iwlwifi pre: x\n"]
    with tempfile.TemporaryDirectory() as tmp:
        p = pathlib.Path(tmp) / "modules.alias"
        p.write_text(good)
        expect("BusLogic" in qualify.read_module_aliases(str(p)), "good file")
        for line in bad:
            p.write_text(good + line)
            try:
                qualify.read_module_aliases(str(p))
            except qualify.QualificationError:
                continue
            raise Failure(f"malformed line accepted: {line!r}")
        c = pathlib.Path(tmp) / "contents.tsv"
        for row in ("usr/lib/firmware/a.bin\tfirmware-x\n", "usr/share/doc/x\tp\ti\n", "usr/lib/firmware/a.bin\tp\n"):
            c.write_text(row)
            try:
                qualify.read_contents(str(c))
            except qualify.QualificationError:
                continue
            raise Failure(f"malformed Contents row accepted: {row!r}")
        k = pathlib.Path(tmp) / "x.ko"
        for blob in (b"", b"not an elf", b"\x7fELF\x01\x01" + b"\0" * 58):
            k.write_bytes(blob)
            try:
                qualify.read_modinfo(str(k))
            except Exception:  # noqa: BLE001 - any rejection is acceptable here
                continue
            raise Failure("malformed module accepted")
    for alias in ("sdio:c*v02D0d4355*", "bcma:m04BFid0812rev11cl*", "iwl4965", "acpi*:QCOM6390:*",
                  "pci:v00008086d0000008[0-2]sv*sd*bc*sc*i*", "of:N*T*Cqcom"):
        expect(qualify.parse_alias(alias) is None, f"accepted unrepresentable alias {alias}")
    return f"{len(bad)} malformed alias lines, 3 malformed Contents rows and 3 malformed modules rejected; SDIO/BCMA/ACPI/OF unrepresentable"


# ---------------------------------------------------------------------------
# Production rules and policy


@check
def firmware_rules_and_package_set_are_exact():
    names = {p.name for p in HARDWARE.glob("rules/*.json")}
    expect({n for n in names if n.startswith("package.firmware.")} == {f"{r}.json" for r in FAMILIES},
           f"firmware rules {sorted(n for n in names if n.startswith('package.firmware.'))}")
    packages = set()
    for r in production_rules():
        for pkg in r["effects"].get("packages", []):
            packages.add(pkg["name"])
            expect(pkg["source"] == "non-free-firmware" and pkg["disposition"] == "require", f"{r['id']}: {pkg}")
            expect(set(pkg) == {"name", "disposition", "source", "reason"}, f"{r['id']}: extra package fields")
    active_new = packages - EXISTING_PACKAGES
    expect(EXISTING_PACKAGES <= packages, f"existing package lost: {EXISTING_PACKAGES - packages}")
    expect(active_new <= M1F_ALLOWLIST, f"package outside the M1-F allowlist: {active_new - M1F_ALLOWLIST}")
    expect(not packages & NEVER_ACTIVE, f"never-active package present: {packages & NEVER_ACTIVE}")
    rule_modules = set()
    for rid, (pkg, modules, cls) in FAMILIES.items():
        doc = ref.load_json(HARDWARE / "rules" / f"{rid}.json")
        expect([p["name"] for p in doc["effects"]["packages"]] == [pkg], f"{rid}: package")
        expect(doc["effects"].get("classifications", []) == cls, f"{rid}: classifications")
        for sel in doc["match"]:
            ((scope, cond),) = sel.items()
            expect(scope in ("pci", "usb") and list(cond) == ["modalias"], f"{rid}: selector {scope}")
            for a in cond["modalias"]:
                expect(a.startswith(scope + ":") and qualify.parse_alias(a) is not None, f"{rid}: alias {a}")
                expect(a not in EXCLUDED_ALIASES, f"{rid}: excluded alias {a}")
                v = qualify.parse_alias(a)[1]
                expect(v[1] != "*" or v[0] == "00001002", f"{rid}: vendor-wide alias {a}")
        rule_modules |= set(modules)
    expect(not rule_modules & DEFERRED_MODULES, f"deferred module mapped: {rule_modules & DEFERRED_MODULES}")
    expect(set(config()["excluded_aliases"]) == EXCLUDED_ALIASES, "excluded alias set changed")
    return (f"{len(FAMILIES)} firmware rules; production package set = existing 6 + {sorted(active_new)} "
            f"= {len(packages)}; no vendor-wide, excluded, deferred or never-active entry")


def _family_devices(ev, rid):
    pkg, modules, cls = FAMILIES[rid]
    pci_class = "028000" if "network.wifi" in cls else "030000" if not cls else "0d1100"
    return [device_for(i, instantiate(a, pci_class)) for i, a in enumerate(family_aliases(ev, modules))]


@check
def every_active_alias_maps_exactly():
    ev = evidence()
    rules = production_rules()
    counts = {}
    for rid, (pkg, modules, cls) in FAMILIES.items():
        devices = _family_devices(ev, rid)
        inv = inventory(devices)
        expect(ref.inventory_errors(inv) == [], f"{rid}: synthetic inventory invalid: {ref.inventory_errors(inv)[:3]}")
        plan = produce(inv, rules)
        by_target = packages_by_target(plan)
        c = classes(plan)
        ids = {d["id"] for d in devices}
        for d in devices:
            expect(by_target.get(d["id"]) == {pkg}, f"{rid}: {d.get('modalias') or d['interfaces'][0]['modalias']} -> {by_target.get(d['id'])}")
        for name in ("network.wifi", "bluetooth.controller"):
            want = ids if name in cls else set()
            expect(set(c.get(name, [])) & ids == want, f"{rid}: {name} classification")
        counts[rid.split(".", 2)[2]] = len(devices)
    new = sum(n for r, n in counts.items() if f"package.firmware.{r}" in M1F_FAMILIES)
    old = sum(n for r, n in counts.items() if f"package.firmware.{r}" in M1E_FAMILIES)
    expect(old == M1E_ALIAS_COUNT, f"M1-E alias count {old}")
    return f"{old + new} concrete devices ({old} M1-E + {new} M1-F) map to exactly their package and classification: {counts}"


def _fixture_plans():
    return {p.stem: produce(fixture(p.stem)) for p in sorted(FIXTURES.glob("*.json"))}


@check
def fixture_families():
    plans = _fixture_plans()
    expect(len(plans) == 17, f"fixtures {sorted(plans)}")
    want = {
        "fw-01-intel-iwlwifi": {"pci:0000:03:00.0": {"firmware-iwlwifi"}},
        "fw-02-intel-i915": {"pci:0000:00:02.0": {"firmware-intel-graphics"}},
        "fw-03-intel-xe": {"pci:0000:00:02.0": {"firmware-intel-graphics"}},
        "fw-04-amd-amdgpu": {"pci:0000:03:00.0": {"firmware-amd-graphics"}},
        "fw-05-amd-radeon": {"pci:0000:01:00.0": {"firmware-amd-graphics"}},
        "fw-06-brcmfmac-pci": {"pci:0000:02:00.0": {"firmware-brcm80211"}},
        "fw-07-brcmfmac-usb": {"usb:1-4": {"firmware-brcm80211"}},
        "fw-09-realtek-rtw89-pci": {"pci:0000:02:00.0": {"firmware-realtek"}},
        "fw-10-realtek-rtl8xxxu-usb": {"usb:1-3": {"firmware-realtek"}},
        "fw-11-atheros-ath10k-pci": {"pci:0000:03:00.0": {"firmware-atheros"}},
        "fw-12-atheros-ath3k-usb-bluetooth": {"usb:1-5": {"firmware-atheros"}},
        "fw-13-mediatek-mt7921e-pci": {"pci:0000:04:00.0": {"firmware-mediatek"}},
        "fw-14-mediatek-rt2800usb-usb": {"usb:1-6": {"firmware-mediatek"}},
        "fw-15-bluez-bcm203x-usb": {"usb:1-7": {"bluez-firmware"}},
        "fw-17-mediatek-combo-card-bluetooth-negative": {"pci:0000:04:00.0": {"firmware-mediatek"}},
    }
    for name, targets in want.items():
        plan = plans[name]
        expect(packages_by_target(plan) == targets, f"{name}: {packages_by_target(plan)}")
        for p in plan["packages"]:
            expect(p["source"] == "non-free-firmware" and p["source_status"] == "authorised", f"{name}: {p}")
    wifi = {"fw-01-intel-iwlwifi": ["pci:0000:03:00.0"], "fw-07-brcmfmac-usb": ["usb:1-4"],
            "fw-09-realtek-rtw89-pci": ["pci:0000:02:00.0"], "fw-10-realtek-rtl8xxxu-usb": ["usb:1-3"],
            "fw-11-atheros-ath10k-pci": ["pci:0000:03:00.0"], "fw-13-mediatek-mt7921e-pci": ["pci:0000:04:00.0"],
            "fw-14-mediatek-rt2800usb-usb": ["usb:1-6"], "fw-17-mediatek-combo-card-bluetooth-negative": ["pci:0000:04:00.0"]}
    for name, targets in wifi.items():
        expect(classes(plans[name]).get("network.wifi") == targets, f"{name}: network.wifi")
    bt = {"fw-12-atheros-ath3k-usb-bluetooth": ["usb:1-5"], "fw-15-bluez-bcm203x-usb": ["usb:1-7"]}
    for name, plan in plans.items():
        expect(classes(plan).get("bluetooth.controller") == bt.get(name), f"{name}: bluetooth.controller")
        if name in bt:
            expect("network.wifi" not in classes(plan), f"{name}: Bluetooth controller called Wi-Fi")
    combo = plans["fw-17-mediatek-combo-card-bluetooth-negative"]
    expect("usb:1-11" not in packages_by_target(combo) and "usb:1-11" in [u["target"] for u in combo["unresolved"]],
           "combo-card Bluetooth (btusb, deferred) must stay unresolved")
    for name in ("fw-08-near-misses", "fw-16-second-wave-near-misses"):
        near = plans[name]
        expect(near["packages"] == [] and near["sources"] == [], f"{name}: got {near['packages']}")
        expect("network.wifi" not in classes(near) and "bluetooth.controller" not in classes(near), f"{name}: classified")
    c = classes(plans["fw-16-second-wave-near-misses"])
    expect(c.get("network.ethernet") == ["pci:0000:05:00.0"], "Realtek Ethernet stays Ethernet only")
    expect(sorted(c.get("network.other", [])) == ["pci:0000:07:00.0", "pci:0000:08:00.0", "pci:0000:09:00.0"], "0280 near misses")
    return ("15 positive fixtures map to their package; Wi-Fi/Bluetooth classes target-specific; combo-card Bluetooth "
            "unresolved; 18 near misses (incl. Realtek Ethernet/card reader/USB audio/USB Ethernet, ath11k, ambiguous IDs) get nothing")


@check
def classification_boundaries():
    rules = production_rules()
    dev = device_for(0, instantiate("pci:v00008086d00000082sv*sd00001301bc*sc*i*", "088000"))
    gpu_alias = [a for a in evidence()["modules"]["amdgpu"]["aliases"] if "bc*" in a][0]
    dev2 = device_for(1, instantiate(gpu_alias, "048000"))
    plan = produce(inventory([dev, dev2]), rules)
    c = classes(plan)
    expect(packages_by_target(plan) == {dev["id"]: {"firmware-iwlwifi"}, dev2["id"]: {"firmware-amd-graphics"}},
           "firmware mapping by alias, independent of class")
    expect(not any(n.startswith("gpu.") for n in c), f"firmware rule created a GPU class: {c}")
    expect(c.get("network.wifi") == [dev["id"]] and "network.other" not in c, "Wi-Fi only via qualified alias")
    for r in rules:
        cls = r["effects"].get("classifications", [])
        if any(x in ("network.wifi", "bluetooth.controller") for x in cls):
            expect(r["id"].startswith("package.firmware."), f"{r['id']}: Wi-Fi/Bluetooth class outside firmware rules")
    return "Wi-Fi/Bluetooth classes only from qualified firmware-family aliases; firmware rules never create gpu.*"


@check
def unknown_hardware_and_microcode_unchanged():
    plan = produce(inventory([device_for(0, "pci:v0000F0FFd0000F0FFsv0000F0FFsd0000F1FFbcFFsc00i00")]))
    expect(plan["packages"] == [] and [u["target"] for u in plan["unresolved"]] == ["cpu", "pci:0000:01:00.0"],
           "unknown hardware")
    for vendor, pkg in (("GenuineIntel", "intel-microcode"), ("AuthenticAMD", "amd64-microcode")):
        expect([p["name"] for p in produce(inventory([], vendor))["packages"]] == [pkg], f"{vendor} microcode")
    return "unknown device/CPU get nothing; Intel/AMD microcode unchanged"


@check
def parity_and_determinism():
    rules = production_rules()
    rnd = random.Random("lubarchy-m1f")
    n = 0
    for p in sorted(FIXTURES.glob("*.json")):
        inv = ref.load_json(p)
        want = resolver.canonical_json(resolver.resolve(inv, rules))
        expect(want == ref.canonical_json(ref.resolve(inv, rules, authorised_sources=PRODUCT_SOURCE_POLICY)),
               f"{p.stem}: production/reference parity")
        for _ in range(3):
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
        for word in ("modules.alias", "/lib/modules", "qualify-firmware", "firmware-mappings", "module-alias-evidence",
                     "Contents", "modinfo"):
            expect(word not in text, f"{path.name} references {word} (runtime rule generation)")
    tree = ast.parse(TOOL.read_text(encoding="utf-8"))
    imports = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imports |= {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    expect(imports == {"hashlib", "json", "lzma", "os", "re", "struct", "sys"}, f"tool imports {imports}")
    strings = " ".join(n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str))
    for word in ("apt-get", "dpkg -i", "modprobe", "insmod", "subprocess", "urllib", "http://", "https://"):
        expect(word not in strings, f"tool mentions {word}")
    return "resolver/probe never read kernel or package metadata; the qualification tool reads files only (no process, no network)"


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
