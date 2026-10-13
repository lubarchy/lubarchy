#!/usr/bin/python3
"""LUBARCHY hardware package transaction gate (M1-I; synthetic only).

Exercises hardware/package_transaction.py and hardware/compile-transaction.py
with synthetic inventories (tests/fixtures/hardware/firmware, inventory), the
production rules, a deterministic synthetic package universe
(tests/fixtures/hardware/transaction/universe.json) rendered by a fake
read-only backend in APT's own text formats, and real APT output samples
captured on the Debian builder (apt-samples/). It never contacts a mirror or
NVIDIA, never probes the runner's hardware, never runs apt/dpkg and never
mutates packages.
"""

import sys

sys.dont_write_bytecode = True

import ast  # noqa: E402
import copy  # noqa: E402
import inspect  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import pathlib  # noqa: E402
import re  # noqa: E402
import subprocess  # noqa: E402

TESTS = pathlib.Path(__file__).resolve().parent
REPO = TESTS.parent
HARDWARE = REPO / "hardware"
FX = TESTS / "fixtures" / "hardware"
TFX = FX / "transaction"
SAMPLES = TFX / "apt-samples"
EXPECTED = TFX / "expected"
sys.path.insert(0, str(HARDWARE))
import package_transaction as pt  # noqa: E402
import resolver  # noqa: E402

sys.path.remove(str(HARDWARE))

PRODUCTION_PACKAGES = {"intel-microcode", "amd64-microcode", "firmware-iwlwifi", "firmware-intel-graphics",
                       "firmware-amd-graphics", "firmware-brcm80211", "firmware-realtek", "firmware-atheros",
                       "firmware-mediatek", "bluez-firmware"}
FAMILY_FIXTURES = ["fw-01-intel-iwlwifi", "fw-02-intel-i915", "fw-04-amd-amdgpu", "fw-06-brcmfmac-pci",
                   "fw-07-brcmfmac-usb", "fw-09-realtek-rtw89-pci", "fw-11-atheros-ath10k-pci",
                   "fw-12-atheros-ath3k-usb-bluetooth", "fw-13-mediatek-mt7921e-pci", "fw-15-bluez-bcm203x-usb"]


class Failure(Exception):
    pass


def expect(cond, message):
    if not cond:
        raise Failure(message)


CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


def load(path):
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


def universe():
    return load(TFX / "universe.json")


def inventory(name):
    for d in ("firmware", "inventory"):
        p = FX / d / f"{name}.json"
        if p.exists():
            return load(p)
    raise Failure(f"no fixture {name}")


RULES = resolver.load_production_rules()


# ---------------------------------------------------------------------------
# Fake read-only backend: renders the synthetic universe in APT/dpkg formats.

class FakeBackend:
    def __init__(self, u=None):
        self.u = copy.deepcopy(u or universe())
        self.calls = []

    def architecture(self):
        return self.u["architecture"]

    def installed_text(self):
        return "".join(f"{e['package']}\t{e['architecture']}\t{e['version']}\t{e['status']}\n"
                       for e in self.u["installed"])

    def config_text(self):
        return "\n".join(self.u["config"]) + "\n"

    def auto_text(self):
        return "".join(e["package"] + ("" if e["architecture"] in (self.u["architecture"], "all")
                                       else ":" + e["architecture"]) + "\n"
                       for e in self.u["installed"] if e.get("automatic") and e["status"].endswith(" installed"))

    def indextargets_text(self):
        out = []
        for key in sorted(self.u["repositories"]):
            out.append("\n".join(f"{k}: {v}" for k, v in sorted(self.u["repositories"][key].items())))
        return "\n\n".join(out) + "\n"

    def _installed(self, name):
        for e in self.u["installed"]:
            if e["package"] == name and e["status"].endswith(" installed"):
                return e
        return None

    def _candidate(self, name):
        vs = self.u["packages"].get(name, [])
        best = None
        for v in vs:
            if best is None or pt.version_compare(v["version"], best["version"]) > 0:
                best = v
        return best

    def _version(self, name, ver):
        for v in self.u["packages"].get(name, []):
            if v["version"] == ver:
                return v
        return None

    def policy_text(self, names):
        self.calls.append(("policy", tuple(names)))
        out = []
        for n in names:
            if n not in self.u["packages"] and not self._installed(n):
                continue
            inst = self._installed(n)
            cand = self._candidate(n)
            out.append(f"{n}:")
            out.append(f"  Installed: {inst['version'] if inst else '(none)'}")
            out.append(f"  Candidate: {cand['version'] if cand else '(none)'}")
            out.append("  Version table:")
            versions = sorted({v["version"] for v in self.u["packages"].get(n, [])} |
                              ({inst["version"]} if inst else set()),
                              key=__import__("functools").cmp_to_key(pt.version_compare), reverse=True)
            for ver in versions:
                mark = "***" if inst and inst["version"] == ver else "   "
                out.append(f" {mark} {ver} 500")
                v = self._version(n, ver)
                for r in (v["repositories"] if v else []):
                    out.append(f"        500 {self.u['repositories'][r]['Description']}")
                if inst and inst["version"] == ver:
                    out.append("        100 /var/lib/dpkg/status")
        return "\n".join(out) + "\n"

    def show_text(self, name, version):
        v = self._version(name, version)
        if v is None:
            return ""
        return (f"Package: {name}\nVersion: {version}\nArchitecture: {v['architecture']}\n"
                f"Filename: pool/{name}_{version}_{v['architecture']}.deb\nSize: {v['size']}\nSHA256: {v['sha256']}\n"
                f"Description: synthetic\n\n")

    def simulate_text(self, args):
        self.calls.append(("simulate", tuple(args)))
        recommends = "--no-install-recommends" not in args
        specs = [a for a in args if not a.startswith("-") and a != "install"]
        lines, seen, todo = [], set(), []
        for s in specs:
            spec, _, ver = s.partition("=")
            name, _, qual = spec.partition(":")
            if name not in self.u["packages"]:
                return f"E: Unable to locate package {spec}\n"
            v = self._version(name, ver) if ver else self._candidate(name)
            if v is None:
                return f"E: Version '{ver}' for '{spec}' was not found\n"
            if qual and qual != v["architecture"]:          # stricter than APT 3.0.3 (native/:all interchangeable)
                return f"E: Unable to locate package {spec}\n"
            todo.append((name, v))
        while todo:
            name, v = todo.pop(0)
            if name in seen:
                continue
            seen.add(name)
            for d in v["depends"] + (v["recommends"] if recommends else []):
                dv = self._candidate(d)
                if dv is not None:
                    todo.append((d, dv))
            inst = self._installed(name)
            arch = v["architecture"] if v["architecture"] != "all" else "all"
            if inst and inst["version"] == v["version"]:
                continue
            old = f"[{inst['version']}] " if inst else ""
            lines.append(f"Inst {name} {old}({v['version']} Debian:13.7/stable [{arch}])")
        lines += self.u["extra_simulation_lines"]
        return "NOTE: This is only a simulation!\n" + "\n".join(lines) + "\n"


def compile_fx(name="fw-01-intel-iwlwifi", u=None, rules=None, expected=None, backend=None):
    b = backend or FakeBackend(u)
    return pt.compile_transaction(inventory(name), RULES if rules is None else rules, b, expected), b


def rejects(code, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except pt.TransactionError as e:
        expect(e.code == code, f"expected {code}, got {e.code}: {e.message}")
        return e
    raise Failure(f"expected {code}, but it was accepted")


def ops(tx):
    return {o["package"]: o for o in tx["operations"]}


# ---------------------------------------------------------------------------
# Contract, determinism, fixtures

@check
def schema_is_meta_valid_and_closed():
    schema = load(HARDWARE / "schemas" / "package-transaction-v1.schema.json")
    import jsonschema
    jsonschema.Draft202012Validator.check_schema(schema)
    expect(schema["$id"] == "urn:lubarchy:hardware:package-transaction:v1", "$id")
    expect(schema["properties"]["schema"]["const"] == pt.TRANSACTION_SCHEMA_ID, "schema const")

    def walk(node, path=""):
        if isinstance(node, dict):
            if isinstance(node.get("properties"), dict):
                expect(node.get("additionalProperties") is False, f"open object at {path or '/'}")
            for k, v in node.items():
                walk(v, f"{path}/{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}/{i}")
    walk(schema)
    for prop in schema["properties"]:
        expect(not re.search(r"(time|date|stamp|host|user|command|stdout|stderr|secret|_at$)", prop),
               f"schema field {prop}")
    return "Draft 2020-12 valid; every object closed; no timestamp/command/identity fields"


@check
def existing_schemas_unchanged():
    out = subprocess.run(["git", "-C", str(REPO), "diff", "--quiet", "72ccc8bdb5c7386b282f82b616cf8ed10e7d5b4a", "--",
                          "hardware/schemas/inventory-v1.schema.json", "hardware/schemas/rule-v1.schema.json",
                          "hardware/schemas/plan-v1.schema.json"], capture_output=True)
    if out.returncode == 128:  # shallow clone without the base commit: fall back to content identities
        return "base commit unavailable; schema identities checked by gates 4-6"
    expect(out.returncode == 0, "inventory/rule/plan v1 schemas changed")
    return "inventory-v1, rule-v1 and plan-v1 unchanged since M1-H"


@check
def valid_plan_compiles_deterministically():          # tests 1-4, 43-45
    tx1, _ = compile_fx()
    tx2, _ = compile_fx()
    expect(resolver.canonical_json(tx1) == resolver.canonical_json(tx2), "canonical bytes differ")
    expect(pt.transaction_digest(tx1) == pt.transaction_digest(tx2), "digest differs")
    pt.check_transaction(tx1)
    o = ops(tx1)
    expect(set(o) == {"firmware-iwlwifi", "intel-microcode", "iucode-tool"}, f"closure {sorted(o)}")
    expect(all(re.fullmatch(r"[0-9a-f]{64}", x["sha256"]) and x["size"] > 0 for x in tx1["operations"]), "hashes")
    keys = [(x["package"], x["architecture"]) for x in tx1["operations"]]
    expect(keys == sorted(keys), "operations not sorted")
    expect("timestamp" not in json.dumps(tx1), "timestamp in transaction")
    expect(o["intel-microcode"]["automatic_before"] is None and o["intel-microcode"]["automatic_after"] is False,
           "new direct package must end manual")
    expect(o["firmware-iwlwifi"]["automatic_after"] is False, "new direct package must end manual")
    expect(o["iucode-tool"]["automatic_before"] is None and o["iucode-tool"]["automatic_after"] is True,
           "new dependency must end automatic")
    return f"{pt.transaction_digest(tx1)}; closure {sorted(o)} with SHA-256 and size"


@check
def current_production_families_compile_to_golden_transactions():
    seen = set()
    for name in FAMILY_FIXTURES:
        tx, _ = compile_fx(name)
        got = resolver.canonical_json(tx) + b"\n"
        want = (EXPECTED / f"{name}.json").read_bytes()
        expect(got == want, f"{name}: transaction differs from the reviewed expected transaction")
        seen |= {r["package"] for r in tx["direct_requirements"]}
    expect(seen == PRODUCTION_PACKAGES, f"families covered {sorted(seen)}")
    expect(not any("nvidia" in p for p in seen), "NVIDIA package in a transaction")
    return f"{len(FAMILY_FIXTURES)} fixtures; all 10 production packages; byte-identical to expected/"


# ---------------------------------------------------------------------------
# Authority: package names only from the recomputed plan

@check
def package_names_come_only_from_the_recomputed_plan():   # tests 5-8
    tx, b = compile_fx()
    plan = resolver.resolve(inventory("fw-01-intel-iwlwifi"), RULES)
    want = sorted(p["name"] for p in plan["packages"])
    sims = [c[1] for c in b.calls if c[0] == "simulate"]
    first = [a for a in sims[0] if not a.startswith("-") and a != "install"]
    expect(first == want, f"simulated names {first} != plan {want}")
    params = list(inspect.signature(pt.compile_transaction).parameters)
    expect(params == ["inventory", "rules", "backend", "expected"], f"compile_transaction parameters {params}")
    for bad in ({"packages": ["evil"]}, {"intel-microcode": "1.0"}, {"version": "1"}, {"target_release": "sid"},
                {"apt_options": ["-o", "x"]}):
        rejects("input_malformed", compile_fx, expected=bad)
    tampered = copy.deepcopy(tx)
    tampered["operations"].append(dict(tampered["operations"][0], package="firmware-nvidia-gsp"))
    tampered["operations"].sort(key=lambda o: (o["package"], o["architecture"]))
    rejects("contract_failure", pt.verify_transaction, tampered, inventory("fw-01-intel-iwlwifi"), RULES, FakeBackend())
    consistent = copy.deepcopy(tx)
    consistent["operations"].append(dict(consistent["operations"][0], package="firmware-nvidia-gsp",
                                         relation="dependency", automatic_after=True))
    consistent["operations"].sort(key=lambda o: (o["package"], o["architecture"]))
    rejects("stale_transaction", pt.verify_transaction, consistent, inventory("fw-01-intel-iwlwifi"), RULES,
            FakeBackend())
    return "names == fresh plan; no package/version/selector/option inputs; caller-added package -> rejected/stale"


@check
def cli_accepts_no_package_or_option_input():           # tests 6-8, 39-42
    cli = HARDWARE / "compile-transaction.py"
    env = {"PATH": "/usr/bin:/bin"}
    for argv in (["compile", "intel-microcode"], ["compile", "intel-microcode=1.0"], ["compile", "-t", "trixie"],
                 ["compile", "-o", "APT::Get::AllowUnauthenticated=1"], ["compile", "--rules", "/tmp"],
                 ["compile", "--apt-get", "/tmp/apt-get"], ["apply"], ["install", "x"],
                 ["compile", "--expect-plan-digest", "not-a-digest"], ["verify"], []):
        r = subprocess.run([sys.executable, "-I", str(cli), *argv], capture_output=True, env=env, timeout=60)
        expect(r.returncode == 64 and r.stdout == b"", f"{argv}: exit {r.returncode}")
    tree = ast.parse(cli.read_text(encoding="utf-8"))
    calls = [ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)]
    expect("resolver.load_production_rules" in calls and "resolver.load_rule_directory" not in calls, "rules path")
    backends = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and ast.unparse(n.func) == "pt.SystemBackend"]
    expect(backends and all(not n.args and not n.keywords for n in backends), "CLI backend must take no arguments")
    expect("probe.Probe()" in ast.unparse(tree), "CLI must use the real probe")
    text = cli.read_text(encoding="utf-8")
    for word in ("argparse", "os.environ", "--rules", "--apt", "--sources", "APT_CONFIG", "shell=True", "def apply"):
        expect(word not in text, f"CLI mentions {word}")
    return "package/version/selector/option/rules/binary/apply arguments are usage errors; fixed probe, rules and backend"


@check
def validate_mode_checks_contract_only():
    tx, _ = compile_fx()
    cli = HARDWARE / "compile-transaction.py"
    good = resolver.canonical_json(tx)
    r = subprocess.run([sys.executable, "-I", str(cli), "validate", "--transaction", "-"], input=good,
                       capture_output=True, env={"PATH": "/usr/bin:/bin"}, timeout=60)
    expect(r.returncode == 0 and json.loads(r.stdout)["transaction_digest"] == pt.transaction_digest(tx), "validate")
    bad = copy.deepcopy(tx)
    bad["safety"]["allow_removals"] = True
    r = subprocess.run([sys.executable, "-I", str(cli), "validate", "--transaction", "-"],
                       input=resolver.canonical_json(bad), capture_output=True, env={"PATH": "/usr/bin:/bin"}, timeout=60)
    expect(r.returncode == 2, f"unsafe transaction document accepted ({r.returncode})")
    bad = copy.deepcopy(tx)
    bad["created"] = "2026-10-14T00:00:00Z"
    rejects("contract_failure", pt.check_transaction, bad)
    return "validate exit 0 on a compiled transaction, 2 on a tampered or timestamped one"


# ---------------------------------------------------------------------------
# Staleness

@check
def stale_inputs_are_rejected():                          # tests 9-16
    tx, _ = compile_fx()
    inv = inventory("fw-01-intel-iwlwifi")
    pt.verify_transaction(tx, inv, RULES, FakeBackend())
    rejects("stale_transaction", pt.verify_transaction, tx, inventory("fw-02-intel-i915"), RULES, FakeBackend())
    rules2 = [r for r in RULES if r["id"] != "class.network.other"]
    rejects("stale_transaction", pt.verify_transaction, tx, inv, rules2, FakeBackend())
    rejects("stale_transaction", compile_fx, expected={"plan_digest": "sha256:" + "0" * 64})
    rejects("stale_transaction", compile_fx, expected={"inventory_digest": "sha256:" + "0" * 64})
    rejects("stale_transaction", compile_fx, expected={"rule_set_digest": "sha256:" + "0" * 64})
    cases = {}
    u = universe(); u["installed"].append({"package": "nano", "architecture": "amd64", "version": "8.4-1",
                                           "status": "install ok installed"}); cases["installed state"] = u  # noqa: E702
    u = universe(); u["installed"].append({"package": "nano", "architecture": "amd64", "version": "8.4-1",
                                           "status": "hold ok installed"}); cases["held state"] = u  # noqa: E702
    u = universe(); u["repositories"]["trixie-main"]["Base-URI"] = "https://mirror.example/debian/dists/trixie/"
    u["repositories"]["trixie-main"]["Description"] = "https://mirror.example/debian trixie/main amd64 Packages"
    cases["repository metadata"] = u
    u = universe(); u["packages"]["iucode-tool"][0]["version"] = "2.3.1-4"; cases["package version"] = u  # noqa: E702
    u = universe(); u["packages"]["iucode-tool"][0]["sha256"] = "f" * 64; cases["package hash"] = u  # noqa: E702
    u = universe(); u["packages"]["iucode-tool"][0]["size"] = 56129; cases["package size"] = u  # noqa: E702
    u = universe(); u["config"].append('DPkg::Post-Invoke:: "/usr/local/bin/x";'); cases["hook config"] = u  # noqa: E702
    for label, uu in cases.items():
        rejects("stale_transaction", pt.verify_transaction, tx, inv, RULES, FakeBackend(uu))
    return f"stale on inventory, rule set, plan/inventory/rule digests and {', '.join(cases)}"


# ---------------------------------------------------------------------------
# Plan acceptance and provenance

def _with_rule(effect_packages, conflict=False):
    rules = copy.deepcopy(RULES)
    rules.append({"schema": "lubarchy.hardware.rule/v1", "id": "test.transaction.extra", "description": "test",
                  "priority": 200, "match": [{"cpu": {"vendor": ["GenuineIntel"]}}],
                  "effects": {"packages": effect_packages}})
    return rules


@check
def plan_acceptance_rules():                              # tests 19-24
    rejects("unauthorised_source", compile_fx, rules=_with_rule(
        [{"name": "test-contrib-package", "disposition": "require", "source": "contrib", "reason": "test"}]))
    rejects("plan_rejected", compile_fx, rules=_with_rule(
        [{"name": "intel-microcode", "disposition": "forbid", "reason": "test"}]))
    rejects("plan_rejected", compile_fx, rules=_with_rule(
        [{"name": "test-forbidden", "disposition": "forbid", "reason": "test"}]))
    bad = inventory("fw-01-intel-iwlwifi")
    bad["mac_address"] = "00:00:00:00:00:00"
    rejects("plan_rejected", pt.compile_transaction, bad, RULES, FakeBackend())
    rules = _with_rule([])
    rules[-1]["effects"] = {"warnings": [{"code": "test", "message": "test"}]}
    rejects("plan_rejected", compile_fx, rules=rules)
    base = resolver.resolve(inventory("fw-01-intel-iwlwifi"), RULES)
    pt.accept_plan(base)
    for label, mutate in (
            ("package policy_review_required", lambda p: p["packages"][0].update(source_status="policy_review_required")),
            ("package source outside policy", lambda p: p["packages"][0].update(source="contrib")),
            ("source list policy_review_required", lambda p: p["sources"][0].update(status="policy_review_required")),
            ("widened source policy", lambda p: p["source_policy"].update(authorised=["contrib", "main", "non-free-firmware"]))):
        crafted = copy.deepcopy(base)
        mutate(crafted)
        rejects("unauthorised_source", pt.accept_plan, crafted)
    for label, mutate in (("status invalid", lambda p: p.update(status="invalid")),
                          ("status conflict", lambda p: p.update(status="conflict")),
                          ("conflicts", lambda p: p["conflicts"].append({"kind": "x", "subject": "y", "rules": []})),
                          ("profile", lambda p: p["profiles"].append({"name": "x", "reasons": []})),
                          ("forbid", lambda p: p["packages"][0].update(disposition="forbid")),
                          ("package expression", lambda p: p["packages"][0].update(name="intel-microcode=1.0"))):
        crafted = copy.deepcopy(base)
        mutate(crafted)
        rejects("plan_rejected", pt.accept_plan, crafted)
    tx, _ = compile_fx("fw-07-brcmfmac-usb")
    expect(tx["plan_status"] == "partial" and tx["unresolved"], "partial plan / unresolved")
    plan = resolver.resolve(inventory("fw-07-brcmfmac-usb"), RULES)
    expect(tx["unresolved"] == plan["unresolved"], "unresolved targets not preserved")
    return ("contrib -> unauthorised; each acceptance rule rejects a crafted plan on its own; "
            "conflict/forbid/invalid/warnings -> rejected; partial accepted with unresolved")


@check
def provenance_is_bound_to_authorised_debian_identity():  # tests 17-18, 25-26
    tx, _ = compile_fx()
    o = ops(tx)
    expect(o["intel-microcode"]["relation"] == "direct" and o["iucode-tool"]["relation"] == "dependency", "relations")
    expect(o["intel-microcode"]["repositories"][0]["component"] == "non-free-firmware", "component binding")
    mutations = {
        "wrong component (non-free)": ("trixie-non-free-firmware", "Component", "non-free"),
        "contrib component": ("trixie-main", "Component", "contrib"),
        "wrong origin": ("trixie-non-free-firmware", "Origin", "NVIDIA"),
        "wrong label": ("trixie-main", "Label", "NVIDIA CUDA"),
        "wrong codename (sid)": ("trixie-main", "Codename", "sid"),
        "backports": ("trixie-main", "Codename", "trixie-backports"),
        "untrusted index": ("trixie-main", "Trusted", "no"),
        "foreign keyring": ("trixie-main", "Signed-By", "/etc/apt/keyrings/nvidia.gpg"),
    }
    for label, (repo, field, value) in mutations.items():
        u = universe()
        u["repositories"][repo][field] = value
        rejects("unauthorised_source", compile_fx, u=u)
    u = universe()
    u["packages"]["intel-microcode"][0]["repositories"] = ["trixie-main"]
    rejects("provenance_failure", compile_fx, u=u)                 # plan says non-free-firmware
    u = universe()
    u["packages"]["iucode-tool"][0]["sha256"] = "not-a-hash"
    rejects("provenance_failure", compile_fx, u=u)
    u = universe()
    del u["packages"]["intel-microcode"]
    rejects("provenance_failure", compile_fx, u=u)
    return f"{len(mutations)} repository-identity mutations rejected (dependency included); component, hash, candidate bound"


# ---------------------------------------------------------------------------
# Install-only safety

@check
def install_only_policy():                                # tests 27-34
    u = universe()
    u["installed"] += [{"package": "intel-microcode", "architecture": "amd64", "version": "3.20251111.1~deb13u1",
                        "status": "install ok installed"},
                       {"package": "iucode-tool", "architecture": "amd64", "version": "2.3.1-3",
                        "status": "install ok installed"}]
    tx, _ = compile_fx(u=u)
    expect(ops(tx)["intel-microcode"]["action"] == "already_satisfied", "already_satisfied")
    expect(ops(tx)["firmware-iwlwifi"]["action"] == "install", "install")
    unsafe = {}
    u = universe(); u["installed"].append({"package": "iucode-tool", "architecture": "amd64", "version": "2.3.1-2",  # noqa: E702
                                           "status": "install ok installed"}); unsafe["upgrade"] = u  # noqa: E702
    u = universe(); u["extra_simulation_lines"] = ["Inst libc6 [2.41-12] (2.41-11 Debian:13.7/stable [amd64])"]  # noqa: E702
    unsafe["downgrade"] = u
    u = universe(); u["extra_simulation_lines"] = ["Remv nano [8.4-1]"]; unsafe["removal"] = u  # noqa: E702
    u = universe(); u["extra_simulation_lines"] = ["Purg nano [8.4-1]"]; unsafe["purge"] = u  # noqa: E702
    u = universe(); u["extra_simulation_lines"] = ["Inst dpkg [1.22.21] (1.22.21 Debian:13.7/stable [amd64])"]  # noqa: E702
    unsafe["reinstall"] = u
    u = universe(); u["extra_simulation_lines"] = ["Inst libc6:i386 (2.41-12 Debian:13.7/stable [i386])"]  # noqa: E702
    unsafe["architecture change"] = u
    u = universe(); u["extra_simulation_lines"] = ["Inst libfoo1:i386 (1.0-1 Debian:13.7/stable [i386])"]  # noqa: E702
    unsafe["foreign architecture"] = u
    u = universe(); u["installed"].append({"package": "intel-microcode", "architecture": "amd64",  # noqa: E702
                                           "version": "3.20250812.1~deb13u1", "status": "hold ok installed"})
    unsafe["held package change"] = u
    u = universe(); u["installed"].append({"package": "iucode-tool", "architecture": "amd64", "version": "",  # noqa: E702
                                           "status": "hold ok not-installed"}); unsafe["hold on new package"] = u  # noqa: E702
    u = universe(); u["extra_simulation_lines"] = ["E: Unable to correct problems, you have held broken packages."]  # noqa: E702
    unsafe["unresolvable"] = u
    kinds = {"upgrade": "upgrade", "downgrade": "downgrade", "removal": "removal", "purge": "removal",
             "reinstall": "reinstall", "architecture change": "architecture_change",
             "foreign architecture": "architecture_change", "held package change": "upgrade",
             "hold on new package": "held package", "unresolvable": "could not resolve"}
    for label, uu in unsafe.items():
        e = rejects("unsafe_change_set", compile_fx, u=uu)
        expect(kinds[label] in e.message, f"{label}: rejected for the wrong reason ({e.message})")
    tx, b = compile_fx()
    expect("test-recommended-helper" not in ops(tx), "recommended package selected")
    expect(all("--no-install-recommends" in c[1] and "--no-remove" in c[1] for c in b.calls if c[0] == "simulate"),
           "simulation without --no-install-recommends/--no-remove")
    with_rec = FakeBackend().simulate_text(["install", "intel-microcode"])
    expect("test-recommended-helper" in with_rec, "fixture recommends not exercised")
    return f"already_satisfied/install; {len(unsafe)} unsafe change sets rejected; recommends never selected"


@check
def exact_version_closure_is_verified():               # M1-I amendment tests 3, 8, 11-13
    tx, b = compile_fx()
    sims = [c[1] for c in b.calls if c[0] == "simulate"]
    expect(len(sims) == 2, "expected one resolution and one direct-target simulation")
    targets = [a for a in sims[1] if "=" in a]
    expect(targets == pt.direct_targets(tx["operations"], tx["architecture"]), "simulated targets != direct targets")
    expect(targets == ["firmware-iwlwifi:all=20250410-2", "intel-microcode:amd64=3.20251111.1~deb13u1"],
           f"direct targets {targets}")
    expect(not any(t.startswith("iucode-tool") for t in targets), "dependency emitted as an explicit target")
    expect(tx["safety"]["exact_version_closure_verified"] is True and tx["safety"]["explicit_targets"] == "direct_only",
           "safety flags")

    class Extra(FakeBackend):
        def simulate_text(self, args):
            text = super().simulate_text(args)
            if any("=" in a for a in args):
                text += "Inst test-recommended-helper (1.0-1 Debian:13.7/stable [all])\n"
            return text

    class DependencyVersion(FakeBackend):
        def simulate_text(self, args):
            text = super().simulate_text(args)
            if any("=" in a for a in args):
                text = text.replace("Inst iucode-tool (2.3.1-3 ", "Inst iucode-tool (2.3.1-4 ")
            return text

    class DependencyArch(FakeBackend):
        def simulate_text(self, args):
            text = super().simulate_text(args)
            if any("=" in a for a in args):
                text = text.replace("Inst iucode-tool (2.3.1-3 Debian:13.7/stable [amd64])",
                                    "Inst iucode-tool (2.3.1-3 Debian:13.7/stable [all])")
            return text

    class MissingDependency(FakeBackend):
        def simulate_text(self, args):
            text = super().simulate_text(args)
            if any("=" in a for a in args):
                text = "\n".join(l for l in text.splitlines() if not l.startswith("Inst iucode-tool")) + "\n"
            return text
    for backend in (Extra(), DependencyVersion(), DependencyArch(), MissingDependency()):
        e = rejects("unsafe_change_set", compile_fx, backend=backend)
        expect("does not reproduce the compiled closure" in e.message, f"{type(backend).__name__}: {e.message}")
    return ("direct targets only (name:arch=version); the direct-target simulation reproduces the full closure; "
            "extra, missing, re-versioned or re-architected dependency rejected")


@check
def package_marks_are_bound_and_preserved():           # M1-I amendment tests 1-2, 4-7, 9-10
    inv = inventory("fw-01-intel-iwlwifi")
    tx, _ = compile_fx()
    expect(tx["automatic_package_count"] == 0 and tx["safety"]["package_marks"] == "direct_manual_dependency_automatic",
           "mark summary")
    # existing manually installed dependency stays manual and is not touched
    u = universe()
    u["installed"].append({"package": "iucode-tool", "architecture": "amd64", "version": "2.3.1-3",
                           "status": "install ok installed", "automatic": False})
    tx_dep, _ = compile_fx(u=u)
    expect("iucode-tool" not in ops(tx_dep), "existing manual dependency became an operation")
    expect(not any(t.startswith("iucode-tool") for t in pt.apply_argv(tx_dep)), "existing dependency targeted")
    # mark changes make a prepared transaction stale
    flip = copy.deepcopy(u)
    flip["installed"][-1]["automatic"] = True
    rejects("stale_transaction", pt.verify_transaction, tx_dep, inv, RULES, FakeBackend(flip))
    u_auto = copy.deepcopy(flip)
    tx_auto, _ = compile_fx(u=u_auto)
    back = copy.deepcopy(u_auto)
    back["installed"][-1]["automatic"] = False
    rejects("stale_transaction", pt.verify_transaction, tx_auto, inv, RULES, FakeBackend(back))
    u_apt = universe()
    u_apt["installed"][0]["automatic"] = True                       # manual -> automatic on an unrelated package
    rejects("stale_transaction", pt.verify_transaction, tx, inv, RULES, FakeBackend(u_apt))
    # already-installed direct requirement: manual -> already_satisfied; automatic -> fail closed
    for status_auto, expected in ((False, "already_satisfied"), (True, None)):
        uu = universe()
        uu["installed"].append({"package": "intel-microcode", "architecture": "amd64",
                                "version": "3.20251111.1~deb13u1", "status": "install ok installed",
                                "automatic": status_auto})
        uu["installed"].append({"package": "iucode-tool", "architecture": "amd64", "version": "2.3.1-3",
                                "status": "install ok installed", "automatic": True})   # its installed dependency
        if expected:
            t2, _ = compile_fx(u=uu)
            o = ops(t2)["intel-microcode"]
            expect(o["action"] == expected and o["automatic_before"] is False and o["automatic_after"] is False,
                   "manual direct package must stay manual")
            expect(not any(a.startswith("intel-microcode") for a in pt.apply_argv(t2)), "satisfied package targeted")
        else:
            e = rejects("unsafe_change_set", compile_fx, u=uu)
            expect("marked automatic" in e.message, f"automatic direct package: {e.message}")
    # contract consistency of marks
    for label, mutate in (("dependency forced manual", lambda o: o["iucode-tool"].update(automatic_after=False)),
                          ("direct marked automatic", lambda o: o["intel-microcode"].update(automatic_after=True)),
                          ("new install with prior mark", lambda o: o["iucode-tool"].update(automatic_before=True))):
        bad = copy.deepcopy(tx)
        mutate({x["package"]: x for x in bad["operations"]})
        rejects("contract_failure", pt.check_transaction, bad)
    # architecture binding of explicit targets
    expect(pt.direct_target(ops(tx)["intel-microcode"], "amd64") == "intel-microcode:amd64=3.20251111.1~deb13u1",
           "direct target architecture")
    wrong = dict(ops(tx)["intel-microcode"], architecture="i386")
    rejects("contract_failure", pt.direct_target, wrong, "amd64")
    rejects("contract_failure", pt.direct_target, ops(tx)["iucode-tool"], "amd64")      # dependency never a target
    bad = copy.deepcopy(tx)
    bad["operations"] = [dict(x, architecture="i386") if x["package"] == "intel-microcode" else x for x in bad["operations"]]
    rejects("contract_failure", pt.apply_argv, bad)
    ua = universe()
    ua["packages"]["iucode-tool"][0]["architecture"] = "all"
    rejects("stale_transaction", pt.verify_transaction, tx, inv, RULES, FakeBackend(ua))
    return ("new direct -> manual, new dependency -> automatic; existing marks untouched; mark flips -> stale; "
            "automatic direct requirement fails closed; targets bind architecture")


# ---------------------------------------------------------------------------
# Future apply boundary, environment, shell, locking

@check
def future_apply_argv_and_environment():                  # tests 35-40
    tx, _ = compile_fx()
    os.environ["APT_CONFIG"] = "/tmp/evil.conf"
    os.environ["DEBIAN_FRONTEND"] = "readline"
    try:
        argv = pt.apply_argv(tx)
        env = pt.apply_environment()
        senv = pt.SystemBackend().environment()
    finally:
        del os.environ["APT_CONFIG"], os.environ["DEBIAN_FRONTEND"]
    expect(isinstance(argv, list) and argv[0] == "/usr/bin/apt-get" and argv[1] == "install", "argv head")
    for need in ("--no-install-recommends", "--no-remove"):
        expect(need in argv, f"{need} missing")
    for bad in pt.FORBIDDEN_APT_ARGUMENTS:
        expect(bad not in argv, f"forbidden {bad} in argv")
    expect(argv[len(pt.APPLY_BASE_ARGV):] == pt.direct_targets(tx["operations"], tx["architecture"]),
           "package arguments are not the direct targets")
    expect(all(re.fullmatch(r"[a-z0-9][a-z0-9+.-]+:[a-z0-9-]+=[0-9][A-Za-z0-9.+~:-]*", a)
               for a in argv[len(pt.APPLY_BASE_ARGV):]), "package arguments are not plain name:arch=version")
    expect(not any(a.startswith("iucode-tool") for a in argv), "dependency in the future argv")
    expect(env == {**pt.COMMAND_ENV, "DEBIAN_FRONTEND": "noninteractive"}, f"apply environment {env}")
    expect("APT_CONFIG" not in senv and senv == pt.COMMAND_ENV, "backend inherited the caller environment")
    for bad in ("--allow-downgrades", "--allow-change-held-packages", "--allow-unauthenticated", "--force-yes",
                "--allow-remove-essential", "--fix-broken", "-o", "-c", "-t"):
        expect(bad in pt.FORBIDDEN_APT_ARGUMENTS, f"{bad} not forbidden")
    tampered = copy.deepcopy(tx)
    tampered["operations"][0]["version"] = "1.0 --allow-downgrades"
    rejects("contract_failure", pt.apply_argv, tampered)
    return f"argv {argv[:6]} + {argv[6:]}; env fixed; caller APT_CONFIG/DEBIAN_FRONTEND ignored"


@check
def no_shell_no_mutation_no_lock_bypass():                # tests 35, 41-42
    src = (HARDWARE / "package_transaction.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    consts = {"APT_MARK": "/usr/bin/apt-mark", "APT_GET": "/usr/bin/apt-get", "APT_CACHE": "/usr/bin/apt-cache", "APT_CONFIG_BIN": "/usr/bin/apt-config",
              "DPKG": "/usr/bin/dpkg", "DPKG_QUERY": "/usr/bin/dpkg-query", "TRANSACTION_LOCK": pt.TRANSACTION_LOCK}
    assigned = {t.id: n.value for n in tree.body if isinstance(n, ast.Assign) for t in n.targets
                if isinstance(t, ast.Name) and t.id in consts}
    for name, value in consts.items():
        expect(isinstance(assigned.get(name), ast.Constant) and assigned[name].value == value,
               f"{name} must be the literal {value}")
    expect(isinstance(next(n.value for n in tree.body if isinstance(n, ast.Assign)
                           and any(getattr(t, "id", "") == "COMMAND_ENV" for t in n.targets)), ast.Dict),
           "COMMAND_ENV must be a literal")
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = ast.unparse(node.func)
            expect(fn not in ("os.system", "os.popen", "subprocess.Popen", "subprocess.call", "subprocess.check_output",
                              "os.execv", "os.execvp", "os.unlink", "os.remove", "os.kill", "shutil.rmtree"), f"call {fn}")
            if fn == "subprocess.run":
                kw = {k.arg: ast.unparse(k.value) for k in node.keywords}
                expect(kw.get("shell") == "False", "subprocess.run without shell=False")
    expect("shell=True" not in src, "shell=True")
    for word in ("/var/lib/dpkg/lock", "/var/lib/apt/lists/lock", "/var/cache/apt/archives/lock", "fuser", "pkill"):
        expect(word not in src, f"lock bypass reference {word}")
    expect(pt.TRANSACTION_LOCK.startswith("/run/lubarchy/"), "transaction lock path")
    expect("applying" not in pt.IMPLEMENTED_STATES and not hasattr(pt, "apply_transaction"), "apply implemented")
    real_run = subprocess.run

    def never(*args, **kwargs):
        raise Failure(f"a refused command was executed: {args[0] if args else kwargs}")
    pt.subprocess.run = never
    try:
        for argv in (["/usr/bin/apt-get", "install", "x"], ["/usr/bin/apt-get", "download", "x"],
                     ["/usr/bin/apt-get", "--simulate", "--quiet", "install", "-o", "x"],
                     ["/usr/bin/apt-get", "--simulate", "--quiet", "--allow-downgrades", "install", "x"],
                     ["/usr/bin/apt-get", "--simulate", "--quiet", "remove", "x"],
                     ["/usr/bin/apt-get", "update"], ["/usr/bin/dpkg", "-i", "x.deb"], ["/usr/bin/dpkg", "--purge", "x"],
                     ["/usr/bin/apt-cache", "policy", "-o", "x"], ["/usr/bin/apt-config", "shell"],
                     ["/usr/bin/dpkg-query", "--remove"], ["/usr/bin/apt-mark", "auto", "x"],
                     ["/usr/bin/apt-mark", "manual", "x"], ["/usr/bin/apt-mark", "hold", "x"],
                     ["apt-get", "--simulate"], "apt-get -s install x",
                     ["/bin/sh", "-c", "true"], ["/usr/bin/apt-get", "--simulate", "--quiet", "install", "a\nb"]):
            rejects("contract_failure", pt.run_read_only, argv, pt.COMMAND_ENV)
        for argv in (["/usr/bin/dpkg", "--print-architecture"], ["/usr/bin/apt-get", "indextargets"],
                     ["/usr/bin/apt-mark", "showauto"],
                     ["/usr/bin/apt-get", "--simulate", "--quiet", "--no-install-recommends", "--no-remove",
                      "install", "intel-microcode=1.0"]):
            expect(pt.read_only_operation(argv), f"{argv} should be an allowed read-only operation")
    finally:
        pt.subprocess.run = real_run
    return "subprocess.run(shell=False) only; no lock/kill/unlink; apply not implemented; non-simulating apt-get refused"


@check
def real_apt_output_samples_parse():
    pol = pt.parse_policy((SAMPLES / "policy.txt").read_text(encoding="utf-8"))
    expect(pol["intel-microcode"]["candidate"] == "3.20251111.1~deb13u1" and
           len(pol["intel-microcode"]["versions"]) == 2, "policy parse")
    sim = pt.parse_simulation((SAMPLES / "simulate-install.txt").read_text(encoding="utf-8"))
    expect([(a["op"], a["package"], a["version"], a["architecture"]) for a in sim] ==
           [("Inst", "iucode-tool", "2.3.1-3", "amd64"), ("Inst", "intel-microcode", "3.20251111.1~deb13u1", "amd64")],
           f"simulation parse {sim}")
    direct = pt.parse_simulation((SAMPLES / "simulate-direct-targets.txt").read_text(encoding="utf-8"))
    expect(sorted((a["package"], a["architecture"], a["version"]) for a in direct) ==
           [("intel-microcode", "amd64", "3.20251111.1~deb13u1"), ("iucode-tool", "amd64", "2.3.1-3")],
           "direct-target simulation (intel-microcode:amd64=... only) must reproduce the closure")
    tg = pt.parse_indextargets((SAMPLES / "indextargets.txt").read_text(encoding="utf-8"))
    nff = tg["https://deb.debian.org/debian trixie/non-free-firmware amd64 Packages"]
    expect(pt.authorised_target(nff) and nff["Component"] == "non-free-firmware", "indextargets parse")
    expect(all(pt.authorised_target(t) for t in tg.values()), "a real Debian target is not authorised")
    state, holds = pt.parse_installed((SAMPLES / "dpkg-query.txt").read_text(encoding="utf-8"))
    expect(len(state) == 6 and holds == [], "dpkg-query parse")
    pt.check_config((SAMPLES / "apt-config.txt").read_text(encoding="utf-8"))
    rejects("unauthorised_source", pt.check_config, 'Acquire::AllowInsecureRepositories "true";\n')
    rejects("unauthorised_source", pt.check_config, 'APT::Get::AllowUnauthenticated "1";\n'
            'Acquire::AllowInsecureRepositories "0";\nAcquire::AllowDowngradeToInsecureRepositories "0";\n')
    return f"real builder samples parse: {len(tg)} authorised Debian targets, 2-package closure"


@check
def nvidia_and_source_policy_unchanged():
    expect(tuple(resolver.AUTHORISED_SOURCES) == ("main", "non-free-firmware"), "resolver source policy")
    expect(pt.AUTHORISED_COMPONENTS == ("main", "non-free-firmware"), "transaction components")
    expect(all(r[0] == "Debian" for r in pt.AUTHORISED_RELEASES), "non-Debian release authorised")
    pol = load(HARDWARE / "nvidia" / "apt-policy-v1.json")
    expect(pol["active"] is False, "NVIDIA policy active")
    src = (HARDWARE / "package_transaction.py").read_text(encoding="utf-8").lower()
    expect("nvidia" not in src and "contrib" not in src and "backports" not in src, "transaction code names a source")
    packages = {p["name"] for r in RULES for p in r.get("effects", {}).get("packages", [])}
    expect(packages == PRODUCTION_PACKAGES, f"production package set {sorted(packages)}")
    return "sources main + non-free-firmware (Debian trixie/updates/security); 10 packages; NVIDIA policy inactive"


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
        print(f"FAIL: hardware package transaction ({failures} of {len(CHECKS)} checks failed)", file=sys.stderr)
        return 1
    print(f"PASS: hardware package transaction ({len(CHECKS)} checks)")
    return 0


def write_expected():
    """Explicit regeneration of the reviewed expected transactions (never run by the gate)."""
    EXPECTED.mkdir(exist_ok=True)
    for name in FAMILY_FIXTURES:
        tx, _ = compile_fx(name)
        (EXPECTED / f"{name}.json").write_bytes(resolver.canonical_json(tx) + b"\n")
        print(f"wrote expected/{name}.json {pt.transaction_digest(tx)}")
    return 0


if __name__ == "__main__":
    sys.exit(write_expected() if sys.argv[1:] == ["--write-expected"] else main())
