#!/usr/bin/python3
"""LUBARCHY hardware-enablement orchestration gate (M1-O; synthetic, non-mutating).

Drives hardware/hardware_orchestration.py and hardware/manage-hardware.py on
the gate-12 fake host (a temporary root): the REAL apply executor, the REAL
bounded recovery, the REAL validator and the REAL GC run against a fake
package manager and a synthetic runtime collector whose boot identity the
gate controls. Interruptions are simulated by an exception nothing can catch,
so journals stay exactly where a real SIGKILL leaves them. It never touches
the runner's /var/lib, /run, APT, dpkg, hardware or boot state, needs no root,
contacts no network and never reboots anything.
"""

import sys

sys.dont_write_bytecode = True

import ast  # noqa: E402
import contextlib  # noqa: E402
import copy  # noqa: E402
import fcntl  # noqa: E402
import hashlib  # noqa: E402
import importlib.util  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import pathlib  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import stat  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402

TESTS = pathlib.Path(__file__).resolve().parent
REPO = TESTS.parent
HARDWARE = REPO / "hardware"
sys.path.insert(0, str(HARDWARE))
import hardware_orchestration as ho  # noqa: E402
import hardware_validation as hv  # noqa: E402
import package_apply as pa  # noqa: E402
import package_gc as gc  # noqa: E402
import package_recovery as pr  # noqa: E402
import package_staging as ps  # noqa: E402
import package_transaction as pt  # noqa: E402
import resolver  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate12 = _load("gate12", TESTS / "hardware-recovery.py")
# The M1-N-qualified APT ownership model (gate 14): the APT sandbox's `_apt:root 0700` partial/,
# an object-lifetime metadata overlay on GC's single read, and a root-owned fake /etc/passwd.
gate14 = _load("gate14", TESTS / "hardware-gc.py")
gate11 = gate12.gate11
gate10 = gate11.gate10
gate9 = gate11.gate9
sys.path.remove(str(HARDWARE))

JOURNAL_SCHEMA_SHA256 = "cd6df17ba0446aa6cb968d1c19b5dc97fe87eca39d132746c964512d039b09d6"
INSTALLED = "install ok installed"
Killed = gate12.Killed


class Failure(Exception):
    pass


def expect(cond, message):
    if not cond:
        raise Failure(message)


CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


def rejects(reason, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except ho.OrchestrationError as e:
        expect(e.reason == reason, f"expected {reason}, got {e.code}/{e.reason}: {e.message}")
        return e
    raise Failure(f"expected {reason}, but it was accepted")


@contextlib.contextmanager
def patched(obj, name, value):
    saved = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, saved)


# ---------------------------------------------------------------------------
# Fake host: gate-12 RecHost + a synthetic runtime collector with a boot identity

class FakeRuntime:
    """Normalized runtime evidence derived from the fake host's package state and
    boot identity, in the exact shape of hv.RealRuntime().collect()."""

    def __init__(self, host):
        self.h = host
        self.proc = os.path.join(host.root, "proc")              # like RealRuntime.proc (raw boot id lives here)

    def collect(self, inventory, names):
        h = self.h
        h.collections += 1
        if h.fail.get("observe"):
            raise hv.ValidationError("observation_failed", "injected")
        plan = resolver.check_plan(resolver.resolve(inventory, h.rules()))
        wifi = {t for c in plan["classifications"] if c["name"] == "network.wifi" for t in c["targets"]}
        devices = {}
        for d in inventory["devices"]:
            mods = set(d.get("modules", []))
            devices[d["id"]] = {"present": True, "identity": hv.identity_of(d), "driver": d.get("driver"),
                                "modules": sorted(m for m in mods if hv.MODULE.match(m)),
                                "net": [{"wireless": True}] if d["id"] in wifi else [], "drm": 0, "bluetooth": 0}
        installed = sorted(({"package": e["package"], "architecture": e["architecture"], "version": e["version"],
                             "status": e["status"], "automatic": bool(e.get("automatic"))}
                            for e in h.u_after["installed"]), key=lambda e: (e["package"], e["architecture"]))
        present = {e["package"] for e in installed}
        ev = {"privilege": "root",
              "boot": {"boot_id_digest": "sha256:" + hashlib.sha256(h.boot_id.encode()).hexdigest(),
                       "boot_time": h.boot_time},
              "devices": devices,
              "kernel_log": {"available": True, "firmware_failures": [], "microcode": {"loader_lines": 1,
                                                                                         "errors": 0}},
              "system": {"state": "running", "failed_units": []},
              "packages": {"installed": installed, "audit_clean": all(e["status"] == INSTALLED for e in installed),
                           "check": "ok",
                           "install_times": {n: h.install_times.get(n, 500) for n in names if n in present}},
              "storage": {"mounted": True, "fstype": "ext4", "read_only": False, "block_visible": True,
                          "device": None, "fs_errors": 0},
              "apply_journal": hv.RealRuntime(root=h.root)._apply_journal()}
        for mutate in h.fail.get("evidence", []):
            mutate(ev)
        return hv.check_runtime(ev)


class Host(gate12.RecHost):
    def __init__(self, name, interrupt=None, virt="none", **kw):
        super().__init__(name, interrupt=interrupt, **kw)
        self.inv["system"]["virtualization"] = {"kind": virt}
        self.inv["cpu"]["microcode_revision"] = "0x2c"
        self.boot_id = "6c2d1f0e-raw-boot-id-0001"
        self.boot_time = 2000
        self._write_boot_id()
        self.clock = 3000
        self.install_times = {}
        self.collections = 0
        self.reboots = 0
        gate14.write_passwd(self)

    def runtime(self):
        return FakeRuntime(self)

    def _write_boot_id(self):
        d = pathlib.Path(self.root, "proc/sys/kernel/random")
        d.mkdir(parents=True, exist_ok=True)
        (d / "boot_id").write_text(self.boot_id + "\n")

    def _apt_install(self, argv):
        # What real APT does first (measured on the builder, M1-N correction): its sandbox takes
        # the attempt cache's partial/ (_apt:root, 0700) before acquiring or unpacking anything.
        cache = argv[argv.index("-o") + 1].split("=", 1)[1]
        gate14.own(os.path.join(cache, gc.CACHE_PARTIAL), gate14.FAKE_APT_UID, self.owner_gid, 0o700)
        before = {e["package"] for e in self.u_after["installed"]}
        try:
            return super()._apt_install(argv)
        finally:
            for e in self.u_after["installed"]:
                if e["package"] not in before:
                    self.install_times[e["package"]] = self.clock

    def reboot(self):
        """Test-harness reboot (product code never reboots): a new boot identity
        that started after every recorded installation."""
        self.reboots += 1
        self.boot_id = f"6c2d1f0e-raw-boot-id-{self.reboots + 1:04d}"
        self._write_boot_id()
        self.boot_time = self.clock + 1000 * self.reboots
        self.clock = self.boot_time + 500


def preinstalled(name, **kw):
    """A machine whose plan is already satisfied and whose metadata is current."""
    h = Host(name, **kw)
    h.updated = True
    tx = pt.compile_transaction(h.inv, gate9.RULES, gate11.FakeBackend(h, h.u_after))
    for o in tx["operations"]:
        if o["action"] == "install":
            h.u_after["installed"].append({"package": o["package"], "architecture": o["architecture"],
                                           "version": o["version"], "status": INSTALLED,
                                           "automatic": o["relation"] == "dependency"})
            h.install_times[o["package"]] = 1000
    return h


def apt_installs(h):
    return [c for c in h.commands if c[:2] == [pa.APT_GET, "install"]]


def apt_any(h):
    return [c for c in h.commands if c[:1] == [pa.APT_GET] and c[1:2] != ["check"]]


def dpkg_recovery(h):
    return [c for c in h.commands if c[:2] in ([pa.DPKG, "--configure"], [pa.DPKG, "--triggers-only"])]


def state(h):
    return pathlib.Path(h.root, ho.STATE_DIR)


def runs(h):
    out = []
    root = state(h)
    if not root.exists():
        return out
    for d in root.iterdir():
        out.append(json.loads((d / "journal.json").read_text()))
    return sorted(out, key=lambda j: j["sequence"])


def apply_journals(h):
    return gate11.journals(h)


def receipts(h):
    d = pathlib.Path(h.root, gc.RECEIPT_DIR)
    return sorted(d.glob("*.json")) if d.exists() else []


def bundles(h):
    d = pathlib.Path(h.root, pa.STAGING_DIR)
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


def snapshot(root):
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for n in dirnames + filenames:
            p = os.path.join(dirpath, n)
            st = os.lstat(p)
            data = b""
            if os.path.isfile(p) and not os.path.islink(p):
                data = pathlib.Path(p).read_bytes()
            out[os.path.relpath(p, root)] = (st.st_mode, st.st_size, hashlib.sha256(data).hexdigest())
    return out


def reconcile(h):
    h.commands.clear()
    return ho.reconcile(h)


def outcome(doc, want, reason=None):
    expect(doc["terminal"] and doc["outcome"] == want, f"outcome {doc['outcome']} ({doc['reason']}), want {want}")
    if reason:
        expect(doc["reason"] == reason, f"reason {doc['reason']}, want {reason}")
    ho.check_journal(doc)
    return doc


def history(h):
    return ho.load_history(h, ho.state_root(h, create=False))


# ---------------------------------------------------------------------------
# Contract

@check
def journal_schema_is_strict_meta_valid_and_protected():                     # 12, 45 (schema)
    import jsonschema
    path = HARDWARE / "schemas" / ho.JOURNAL_SCHEMA_FILE
    raw = path.read_bytes()
    expect(hashlib.sha256(raw).hexdigest() == JOURNAL_SCHEMA_SHA256, "orchestration journal schema changed")
    schema = json.loads(raw)
    expect(schema["$schema"] == "https://json-schema.org/draft/2020-12/schema", "not Draft 2020-12")
    jsonschema.Draft202012Validator.check_schema(schema)
    objects = 0

    def closed(node, where):
        nonlocal objects
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                objects += 1
                expect(node.get("additionalProperties") is False, f"{where}: object is not closed")
                expect(set(node.get("required", [])) == set(node["properties"]), f"{where}: optional field")
            if node.get("type") == "string":
                expect("pattern" in node or "enum" in node or "const" in node, f"{where}: unbounded string")
            if node.get("type") == "array":
                expect("maxItems" in node, f"{where}: unbounded array")
            for k, v in node.items():
                closed(v, f"{where}/{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                closed(v, f"{where}/{i}")
    closed(schema, "")
    props = set()

    def names(node):
        if isinstance(node, dict):
            props.update(node.get("properties", {}))
            for v in node.values():
                names(v)
        elif isinstance(node, list):
            for v in node:
                names(v)
    names(schema)
    for p in props:
        expect(not re.search(r"^at$|time|date|host|user|serial|mac|address|^boot_id$|log|output|command", p),
               f"schema field {p}")
    staging = (TESTS / "hardware-staging.py").read_text()
    expect(f'"{ho.JOURNAL_SCHEMA_FILE}": "{JOURNAL_SCHEMA_SHA256}"' in staging,
           "orchestration journal schema is not in the protected contract baseline")
    return f"Draft 2020-12, meta-valid, {objects} closed objects, bounded strings/arrays, no time/identity fields, " \
           "hash protected (gate 15 and the gate-10 baseline)"


# ---------------------------------------------------------------------------
# Convergence

@check
def converged_machine_is_validated_without_apply_and_idempotent():          # 1, 23, 34, 45, 12
    h = preinstalled("converged")
    doc = outcome(reconcile(h), "complete", "converged")
    expect(not apt_any(h) and not dpkg_recovery(h), f"converged machine ran {h.commands}")
    expect(not apply_journals(h), "an apply attempt was created for a converged machine")
    expect(doc["package"]["decision"] == "no_change" and doc["package"]["apply"] is None, "decision")
    expect(doc["package"]["transaction_digest"] is not None, "precheck transaction not bound")
    expect(doc["validation"]["status"] == "PASS" and doc["validation"]["mode"] == "current", "validation")
    expect(doc["maintenance"]["gc"] == "nothing_to_collect", f"gc {doc['maintenance']}")
    expect(ho.exit_status(doc) == ho.EXIT_OK, "exit")
    expect(doc["phases"] == ["started", "preflight", "planned", "validated", "collecting", "finished"],
           f"phases {doc['phases']}")
    first = runs(h)[0]
    snap_pkg = copy.deepcopy(h.u_after["installed"])
    doc2 = outcome(reconcile(h), "complete", "converged")
    expect(not apt_any(h) and not dpkg_recovery(h) and h.u_after["installed"] == snap_pkg, "second run mutated")
    expect(doc2["sequence"] == 2 and doc2["previous"]["run_id"] == first["run_id"] and
           doc2["previous"]["journal_digest"] == resolver.digest(first), "previous run not bound")
    expect(doc2["validation"]["result_digest"] == doc["validation"]["result_digest"] and
           doc2["bindings"] == doc["bindings"], "repeated validation is not deterministic")
    expect(runs(h)[0] == first, "an earlier run journal was rewritten")
    for r in runs(h):
        d = state(h) / r["run_id"]
        for kind, f in ho.ARTIFACT_FILES.items():
            if r["artifacts"][kind]:
                data = (d / f).read_bytes()
                expect(data == resolver.canonical_json(json.loads(data)) + b"\n", f"{f} not canonical")
                expect(resolver.digest(json.loads(data)) == r["artifacts"][kind], f"{f} digest")
        expect((d / "journal.json").read_bytes() == resolver.canonical_json(r) + b"\n", "journal not canonical")
        expect(oct(os.stat(d).st_mode & 0o777) == "0o700", "run directory mode")
    expect(oct(os.stat(state(h)).st_mode & 0o777) == "0o700", "state root mode")
    expect(len(history(h)) == 2, "history")
    return "no APT/dpkg/apply on a converged machine; PASS -> complete; second run identical, chained, nothing " \
           "rewritten; canonical artifacts bound by digest; 0700 run directories"


@check
def required_change_reboot_resume_and_gc():                                 # 2, 11, 17-23, 29, 32, 34, 42
    h = Host("lifecycle")
    doc = outcome(reconcile(h), "reboot_required", "reboot_pending")
    a = apply_journals(h)
    expect(len(a) == 1 and a[0]["phase"] == "complete" and a[0]["result"] == "complete", "apply journal")
    expect(len(apt_installs(h)) == 1, f"APT installs {apt_installs(h)}")
    ap = doc["package"]["apply"]
    expect(doc["package"]["decision"] == "apply" and ap["attempt_id"] == a[0]["attempt_id"] and
           ap["result"] == "complete" and ap["journal_digest"] == resolver.digest(a[0]) and
           ap["transaction_digest"] == a[0]["digests"]["transaction_digest"], f"apply binding {ap}")
    expect(doc["package"]["current_transaction"] == {"attempt_id": a[0]["attempt_id"],
                                                     "transaction_digest": a[0]["digests"]["transaction_digest"]},
           "current transaction binding")
    expect(doc["reboot_required"] and doc["validation"]["boot_relation"] == "same_boot", "same boot")
    expect(doc["maintenance"] == {"gc": "not_run", "skipped": "outcome_not_eligible", "receipt_id": None,
                                  "receipt_digest": None, "plan_digest": None}, "GC ran before reboot validation")
    expect(bundles(h) and not receipts(h), "bundle collected before reboot validation")
    expect(ho.exit_status(doc) == ho.EXIT_REBOOT_REQUIRED, "exit")
    expect(h.reboots == 0, "the coordinator rebooted")
    pre = doc["validation"]["result_digest"]
    pre_boot = doc["boot"]["boot_id_digest"]
    # same boot: still reboot_required, nothing re-applied
    doc = outcome(reconcile(h), "reboot_required", "boot_unchanged")
    expect(not apt_any(h) and len(apply_journals(h)) == 1, "same-boot run re-applied")
    expect(doc["awaiting_reboot"]["validation_digest"] == pre and doc["boot"]["boot_changed"] is False and
           doc["validation"]["previous_result_digest"] == pre and doc["validation"]["mode"] == "revalidation",
           "same boot continuity")
    pre2 = doc["validation"]["result_digest"]
    # external (test-harness) reboot
    h.reboot()
    doc = outcome(reconcile(h), "complete", "converged")
    expect(not apt_any(h) and len(apply_journals(h)) == 1, "post-reboot run re-applied")
    expect(doc["boot"] == {"boot_id_digest": doc["boot"]["boot_id_digest"], "previous_boot_id_digest": pre_boot,
                           "boot_changed": True} and doc["boot"]["boot_id_digest"] != pre_boot, "boot binding")
    v = doc["validation"]
    expect(v["mode"] == "revalidation" and v["previous_result_digest"] == pre2 and v["continuity"] == "checked"
           and v["boot_relation"] == "post_reboot" and v["result_digest"] not in (pre, pre2), "continuity")
    result = json.loads((state(h) / doc["run_id"] / "validation.json").read_text())
    expect(result["bindings"]["previous"] == {"result_digest": pre2, "boot_changed": True}, "validator continuity")
    expect(any(c["id"].startswith("device.continuity:") and c["status"] == "PASS" for c in result["checks"]),
           "continuity checks not exercised")
    m = doc["maintenance"]
    expect(m["gc"] == "complete" and m["receipt_id"] and receipts(h) and not bundles(h), f"gc {m}")
    rec = json.loads(receipts(h)[0].read_text())
    expect(m["receipt_digest"] == resolver.digest(rec) and rec["result"] == "complete", "receipt binding")
    # the pre-reboot results stay historical, unchanged
    hist = history(h)
    expect(hist[0]["doc"]["artifacts"]["validation"] == pre and hist[1]["doc"]["artifacts"]["validation"] == pre2,
           "historical validation rewritten")
    # third run: idempotent
    doc = outcome(reconcile(h), "complete", "converged")
    expect(not apt_any(h) and not dpkg_recovery(h) and len(apply_journals(h)) == 1, "third run mutated")
    expect(doc["awaiting_reboot"] is None and not doc["reboot_required"] and doc["validation"]["mode"] == "current",
           "old reboot state reused")
    expect(doc["maintenance"]["gc"] == "nothing_to_collect" and len(receipts(h)) == 1, "GC not idempotent")
    return "missing package -> existing apply executor (1 APT run, journal bound) -> same_boot -> reboot_required " \
           "(no GC, no reboot); same boot stays reboot_required; after the harness reboot: revalidation against " \
           "the pre-reboot result, post_reboot PASS -> complete -> M1-N GC (receipt bound); third run idempotent"


@check
def vm_microcode_stays_limited():                                            # 24, 26, 28
    h = Host("vm", virt="vm")
    outcome(reconcile(h), "reboot_required", "reboot_pending")
    h.reboot()
    doc = outcome(reconcile(h), "validation_limited", "validation_limited")
    result = json.loads((state(h) / doc["run_id"] / "validation.json").read_text())
    mc = [c for c in result["checks"] if c["id"].startswith("cpu.microcode:")]
    expect(mc and all(c["status"] == "LIMITED" and c["reason"] == "virtualized_cpu" for c in mc), f"microcode {mc}")
    expect(result["status"] == "LIMITED" and doc["validation"]["status"] == "LIMITED", "LIMITED promoted")
    expect(doc["outcome"] != "complete" and ho.exit_status(doc) == ho.EXIT_VALIDATION_LIMITED, "exit")
    expect(doc["maintenance"]["gc"] == "complete", "limited outcome is terminal and non-critical")
    doc = outcome(reconcile(h), "validation_limited")
    expect(doc["maintenance"]["gc"] == "nothing_to_collect" and not apt_any(h), "idempotent")
    # a journal that claims complete for a LIMITED result is rejected by the contract itself
    bad = copy.deepcopy(doc)
    bad.update(outcome="complete", reason="converged")
    try:
        ho.check_journal(bad)
        raise Failure("complete outcome with a LIMITED validation accepted")
    except ho.OrchestrationError:
        pass
    return "VM: microcode LIMITED/virtualized_cpu after the reboot; outcome validation_limited (exit 2), never " \
           "complete; contract rejects LIMITED-as-complete"


@check
def validation_fail_is_preserved_and_blocks_gc():                           # 25, 27, 31
    h = Host("fail")
    outcome(reconcile(h), "reboot_required")
    h.reboot()

    def unbind(ev):
        for d in ev["devices"].values():
            d["driver"], d["modules"] = None, []
    h.fail["evidence"] = [unbind]
    doc = outcome(reconcile(h), "validation_failed", "validation_failed")
    expect(doc["validation"]["status"] == "FAIL" and doc["validation"]["critical_failures"] >= 1, "FAIL lost")
    expect(doc["maintenance"]["gc"] == "not_run" and doc["maintenance"]["skipped"] == "outcome_not_eligible", "gc")
    expect(bundles(h) and not receipts(h), "GC ran after a critical FAIL")
    expect(ho.exit_status(doc) == ho.EXIT_VALIDATION_FAILED, "exit")
    # PASS is preserved too (healthy evidence again)
    h.fail.pop("evidence")
    doc = outcome(reconcile(h), "complete")
    expect(doc["validation"]["status"] == "PASS", "PASS not preserved")
    return "critical driver FAIL -> validation_failed (exit 1), no GC, bundle retained; PASS -> complete"


@check
def hardware_change_is_freshly_replanned():                                 # 14 (brief), 5 (stale plan)
    h = preinstalled("replan")
    first = outcome(reconcile(h), "complete")
    wifi = h.inv["devices"].pop()
    doc = outcome(reconcile(h), "complete")
    expect(doc["bindings"]["inventory_digest"] != first["bindings"]["inventory_digest"] and
           doc["bindings"]["plan_digest"] != first["bindings"]["plan_digest"], "old plan reused")
    plan = json.loads((state(h) / doc["run_id"] / "plan.json").read_text())
    expect(not any(t == wifi["id"] for c in plan["classifications"] for t in c["targets"]), "stale plan artifact")
    expect(not apt_any(h), "replanning installed something")
    # hardware that changes between planning and validation makes the run stale
    h.hooks[("inventory", h.counts["inventory"] + 2)] = lambda host: host.inv["devices"].append(copy.deepcopy(wifi))
    doc = outcome(reconcile(h), "stale", "inventory_changed")
    expect(doc["validation"] is None and doc["maintenance"]["gc"] == "not_run", "validated a changed machine")
    old = json.loads((state(h) / first["run_id"] / "plan.json").read_text())
    expect(resolver.digest(old) == first["bindings"]["plan_digest"], "historical plan rewritten")
    return "removed device -> new inventory/plan digests and artifact; the old plan stays historical evidence only"


# ---------------------------------------------------------------------------
# Recovery first

def interrupted_by_reconcile(name, mode, **kw):
    h = Host(name, interrupt=mode, **kw)
    try:
        ho.reconcile(h)
        raise Failure(f"{mode}: reconcile was not interrupted")
    except Killed:
        pass
    h.interrupt = None                                  # the next APT run (if any) is not interrupted
    r = runs(h)
    expect(len(r) == 1 and not r[0]["terminal"] and r[0]["phase"] == "applying", f"run after kill {r[0]['phase']}")
    return h


@check
def interrupted_apply_is_recovered_before_anything_else():                  # 8, 13, 14
    h = interrupted_by_reconcile("crash-in-apply", "trigger")
    expect(apply_journals(h)[0]["phase"] == "applying", "apply journal")
    order = []
    saved = (pr.recover_interrupted_transaction, pa.apply_current_plan)
    with patched(pr, "recover_interrupted_transaction", lambda host: (order.append("recover"), saved[0](host))[1]), \
            patched(pa, "apply_current_plan", lambda host: (order.append("apply"), saved[1](host))[1]):
        doc = outcome(reconcile(h), "reboot_required")
    expect(order == ["recover"], f"order {order}")
    expect(dpkg_recovery(h) and not apt_installs(h), f"commands {h.commands}")
    rec = doc["package"]["recovery"]
    rj = gate12.recovery_journals(h)
    expect(len(rj) == 1 and rec["result"] == "complete" and rec["recovery_id"] == rj[0]["recovery_id"] and
           rec["journal_digest"] == resolver.digest(rj[0]) and rec["attempt_id"] == apply_journals(h)[0]["attempt_id"],
           f"recovery binding {rec}")
    expect(doc["package"]["decision"] == "no_change" and doc["previous"]["phase"] == "applying" and
           doc["previous"]["outcome"] is None, "resume after the crash")
    expect(doc["phases"][:5] == ["started", "preflight", "recovering", "recovered", "planned"], doc["phases"])
    return "SIGKILL inside APT leaves run/apply journals at applying; next reconcile runs bounded recovery first " \
           "(exact dpkg configure), binds it, replans, applies nothing again"


@check
def recovered_state_is_freshly_replanned_before_apply():                    # 11
    h = interrupted_by_reconcile("abandoned", "before-unpack")
    probes = h.counts.get("inventory", 0)
    doc = outcome(reconcile(h), "reboot_required")
    expect(doc["package"]["recovery"]["result"] == "abandoned", "recovery")
    expect(doc["package"]["decision"] == "apply" and len(apt_installs(h)) == 1, "no fresh apply after abandonment")
    a = apply_journals(h)
    expect(len(a) == 2 and a[1]["phase"] == "complete" and doc["package"]["apply"]["attempt_id"] == a[1]["attempt_id"],
           "new attempt")
    expect(h.counts["inventory"] - probes >= 3, "no fresh probe after recovery")
    # a recovery result that disagrees with its own durable journal is not trusted
    h = interrupted_by_reconcile("recovery-claims", "before-unpack")
    real = pr.recover_interrupted_transaction
    with patched(pr, "recover_interrupted_transaction", lambda host: dict(real(host), result="complete",
                                                                         phase="complete")):
        doc = outcome(reconcile(h), "failed", "recovery_unverified")
    expect(not apt_installs(h) and doc["validation"] is None, "continued after an unverified recovery")
    return "no-mutation interruption -> recovery abandons it -> fresh probe/plan -> one new verified apply; a " \
           "recovery claim contradicted by its journal -> failed/recovery_unverified"


@check
def manual_or_failed_recovery_blocks_mutation():                           # 9, 10, 30
    h = interrupted_by_reconcile("manual", "unrelated")
    doc = outcome(reconcile(h), "manual_recovery_required", "manual_recovery_required")
    expect(not apt_installs(h) and not dpkg_recovery(h), "manual case mutated")
    expect(doc["package"]["recovery"]["result"] == "manual_recovery_required", "recovery record")
    expect(doc["maintenance"]["gc"] == "not_run" and not receipts(h), "GC on manual recovery")
    expect(doc["validation"] is None and ho.exit_status(doc) == ho.EXIT_MANUAL_RECOVERY, "exit")
    n = len(gate12.recovery_journals(h))
    doc = outcome(reconcile(h), "manual_recovery_required", "manual_recovery_required")
    expect(doc["package"]["recovery"] is None and doc["package"]["preflight"]["blocked"] ==
           "manual_recovery_required" and len(gate12.recovery_journals(h)) == n, "manual state re-recovered")
    # failed bounded recovery
    h = interrupted_by_reconcile("recover-fails", "trigger")
    h.fail["recover_dpkg"] = True
    doc = outcome(reconcile(h), "failed", "recovery_failed")
    expect(not apt_installs(h) and doc["package"]["recovery"]["result"] == "failed", "failed recovery")
    doc = outcome(reconcile(h), "failed", "recovery_failed")
    expect(not dpkg_recovery(h) and not apt_installs(h), "failed recovery retried automatically")
    # an operator-resolved recovery (the low-level primitive) lets the next reconcile continue
    h.fail.pop("recover_dpkg")
    expect(pr.recover_interrupted_transaction(h)["result"] == "complete", "operator recovery")
    outcome(reconcile(h), "reboot_required")
    expect(not apt_installs(h), "apply after operator recovery")
    # unreadable package journals
    h = Host("unreadable")
    d = pathlib.Path(h.root, pa.JOURNAL_DIR, "00000000000000aa")
    d.mkdir(parents=True)
    (d / "journal.json").write_text("{")
    doc = outcome(reconcile(h), "manual_recovery_required", "package_journal_unreadable")
    expect(not h.commands, "commands with unreadable package journals")
    return "manual recovery -> manual_recovery_required (exit 9), no apply/GC, not re-attempted; failed recovery -> " \
           "failed/recovery_failed, never retried automatically; unreadable package journals block"


# ---------------------------------------------------------------------------
# Crash/resume and package truth

@check
def crash_before_and_after_apply_never_duplicates():                        # 13, 15, 16
    # crash after `planned`, before the apply executor is invoked
    h = Host("crash-before")

    def die(host):
        raise Killed()
    with patched(pa, "apply_current_plan", die):
        try:
            ho.reconcile(h)
            raise Failure("not interrupted")
        except Killed:
            pass
    expect(runs(h)[0]["phase"] == "applying" and not apply_journals(h), "state after the crash")
    doc = outcome(reconcile(h), "reboot_required")
    expect(len(apt_installs(h)) == 1 and len(apply_journals(h)) == 1, "apply not resumed exactly once")
    expect(doc["previous"]["phase"] == "applying", "interrupted predecessor not recorded")
    # crash after the apply completed, before the run journal advanced
    h = Host("crash-after")
    saved = ho.Run.transition

    def die_on_applied(self, phase, *a, **k):
        if phase == "applied":
            raise Killed()
        return saved(self, phase, *a, **k)
    with patched(ho.Run, "transition", die_on_applied):
        try:
            ho.reconcile(h)
            raise Failure("not interrupted")
        except Killed:
            pass
    expect(runs(h)[0]["phase"] == "applying" and apply_journals(h)[0]["phase"] == "complete", "crash point")
    doc = outcome(reconcile(h), "reboot_required")
    expect(not apt_installs(h) and len(apply_journals(h)) == 1, "apply duplicated after a stale run journal")
    expect(doc["package"]["decision"] == "no_change" and doc["package"]["current_transaction"]["attempt_id"] ==
           apply_journals(h)[0]["attempt_id"], "authoritative apply not discovered")
    # an orchestration journal that claims progress is never package truth: an external
    # low-level apply interrupted after our run finished must still be recovered first
    h = Host("external")
    outcome(reconcile(h), "reboot_required")
    h.interrupt = "trigger"
    h.updated = False
    h.u_after["installed"] = [e for e in h.u_after["installed"]
                              if e["package"] not in ("intel-microcode", "iucode-tool", "firmware-iwlwifi")]
    try:
        pa.apply_current_plan(h)
    except Killed:
        pass
    h.interrupt = None
    doc = outcome(reconcile(h), "reboot_required")
    expect(doc["package"]["recovery"]["result"] == "complete" and dpkg_recovery(h), "external crash not recovered")
    # an apply that "succeeds" without authoritative journal evidence is not trusted
    h = Host("fake-success")
    fake = {"schema": pa.JOURNAL_SCHEMA_ID, "attempt_id": "00000000000000ff", "phase": "complete",
            "result": "complete"}
    with patched(pa, "apply_current_plan", lambda host: fake):
        doc = outcome(reconcile(h), "failed", "apply_unverified")
    expect(doc["validation"] is None and doc["package"]["apply"]["result"] == "unverified", "unverified apply")
    h = Host("journal-says-failed")
    real = pa.apply_current_plan

    def lie(host):
        out = real(host)
        p = pathlib.Path(host.root, pa.JOURNAL_DIR, out["attempt_id"], "journal.json")
        j = json.loads(p.read_text())
        j["phases"].append({"phase": "failed", "at": "2026-10-14T00:00:59Z"})
        j.update(phase="failed", result="failed", failure={"code": "apply_failed", "phase": "complete"})
        p.write_bytes(resolver.canonical_json(j) + b"\n")
        return out
    with patched(pa, "apply_current_plan", lie):
        doc = outcome(reconcile(h), "failed", "apply_unverified")
    expect(doc["validation"] is None and doc["maintenance"]["gc"] == "not_run", "continued after unverified apply")
    return "crash before apply -> exactly one apply on resume; crash after a complete apply -> discovered from the " \
           "apply journal, no second APT run; external interrupted apply recovered despite a finished run; " \
           "success without (or contradicted by) the durable apply journal -> failed/apply_unverified"


@check
def apply_errors_map_to_honest_outcomes():                                  # 15, 40
    h = Host("apply-fails")
    h.fail["apt"] = True
    doc = outcome(reconcile(h), "failed", "apply_failed")
    ap = doc["package"]["apply"]
    expect(ap["result"] == "failed" and ap["attempt_id"] == apply_journals(h)[0]["attempt_id"] and
           ap["phase"] == "failed", f"failed apply binding {ap}")
    h.fail.pop("apt")
    order = []
    saved = (pr.recover_interrupted_transaction, pa.apply_current_plan)
    with patched(pr, "recover_interrupted_transaction", lambda host: (order.append("recover"), saved[0](host))[1]), \
            patched(pa, "apply_current_plan", lambda host: (order.append("apply"), saved[1](host))[1]):
        doc = reconcile(h)
    expect(order[:1] == ["recover"] and doc["package"]["recovery"] is not None,
           f"failed apply after APT start not recovered first: {order}")
    # the package lock stays separately authoritative
    h = Host("pkg-lock")
    held = pa.TransactionLock(h).acquire()
    try:
        doc = outcome(reconcile(h), "failed", "package_lock_busy")
    finally:
        held.release()
    expect(not apt_installs(h) and doc["package"]["apply"]["attempt_id"] is None, "apply ran under a held lock")
    # stale transaction (inventory drift between the precheck and apply's own revalidation)
    h = Host("stale")
    h.hooks[("inventory", 3)] = lambda host: host.inv["devices"].clear()
    doc = reconcile(h)
    expect(doc["outcome"] in ("stale", "failed") and not apt_installs(h), f"stale {doc['outcome']} {doc['reason']}")
    return "APT failure -> failed/apply_failed with the attempt bound; next run recovers first; held package lock " \
           "-> failed/package_lock_busy without APT; drift -> stale without APT"


# ---------------------------------------------------------------------------
# Locks

@check
def orchestration_lock_serializes_and_orders_locks():                      # 39, 40
    h = preinstalled("lock")
    lock_path = os.path.join(h.root, pa.LOCK_DIR, ho.LOCK_NAME)
    first = ho.OrchestrationLock(h).acquire()
    try:
        e = rejects("orchestration_busy", ho.reconcile, h)
        expect(e.exit_status == ho.EXIT_BUSY and not state(h).exists(), "busy reconcile wrote state")
    finally:
        first.release()
    # while reconcile runs, the package lock is taken inside the primitives with the
    # orchestration lock already held (order: orchestration -> package)
    h = Host("order")
    seen = []

    def probe_locks(host):
        fd = os.open(os.path.join(host.root, pa.LOCK_DIR, ho.LOCK_NAME), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            seen.append("orchestration-free")
        except BlockingIOError:
            seen.append("orchestration-held")
        finally:
            os.close(fd)
        fd = os.open(os.path.join(host.root, pa.LOCK_DIR, pa.LOCK_NAME), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            seen.append("package-free")
        except BlockingIOError:
            seen.append("package-held")
        finally:
            os.close(fd)
    h.hooks[("update", 1)] = probe_locks
    outcome(reconcile(h), "reboot_required")
    expect(seen == ["orchestration-held", "package-held"], f"lock state during apply {seen}")
    tree = ast.parse((HARDWARE / "hardware_orchestration.py").read_text())
    used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    consts = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    expect("TransactionLock" not in used and "LOCK_NAME" not in used - {"LOCK_NAME"} and pa.LOCK_NAME not in consts
           and not any(c.endswith(pa.LOCK_NAME) for c in consts), "orchestrator takes the package lock itself")
    expect(not any(isinstance(n, ast.Attribute) and n.attr == "LOCK_NAME" and getattr(n.value, "id", "") == "pa"
                   for n in ast.walk(tree)), "orchestrator refers to the package lock name")
    expect(os.path.exists(os.path.join(h.root, pa.LOCK_DIR, ho.LOCK_NAME)), "lock file")
    return "second reconcile -> busy (exit 8, nothing written); during apply both locks are held, package lock " \
           "only inside the primitive; the orchestrator never takes the package lock itself"


# ---------------------------------------------------------------------------
# Persistent state

@check
def corrupt_or_unsafe_state_fails_closed():                                 # 35-38, 41
    n = 0

    def corrupted(label, mutate, reason):
        nonlocal n
        h = preinstalled(f"corrupt-{n}")
        outcome(reconcile(h), "complete")
        mutate(h)
        before = snapshot(state(h)) if state(h).exists() and not state(h).is_symlink() else None
        e = rejects(reason, reconcile, h)
        expect(e.exit_status == ho.EXIT_STATE_INVALID, f"{label}: exit {e.exit_status}")
        expect(not h.commands, f"{label}: commands ran")
        if before is not None:
            expect(snapshot(state(h)) == before, f"{label}: state changed")
        st = ho.status(h)
        expect(st["state"] == "invalid" and st["invalid_reason"] == reason, f"{label}: status {st}")
        n += 1

    def journal(h):
        return state(h) / runs(h)[0]["run_id"] / "journal.json"
    corrupted("malformed journal", lambda h: journal(h).write_text("{"), "artifact_malformed")
    corrupted("schema-invalid journal", lambda h: journal(h).write_bytes(resolver.canonical_json(
        dict(json.loads(journal(h).read_text()), extra=1)) + b"\n"), "journal_invalid")
    corrupted("non-canonical journal", lambda h: journal(h).write_text(json.dumps(json.loads(journal(h).read_text()),
                                                                                  indent=1)), "artifact_malformed")
    corrupted("validation artifact altered", lambda h: (state(h) / runs(h)[0]["run_id"] / "validation.json").write_bytes(
        resolver.canonical_json(dict(json.loads((state(h) / runs(h)[0]["run_id"] / "validation.json").read_text()),
                                     mode="revalidation")) + b"\n"), "artifact_mismatch")
    corrupted("artifact missing", lambda h: (state(h) / runs(h)[0]["run_id"] / "plan.json").unlink(),
              "artifact_missing")
    corrupted("group-writable run", lambda h: os.chmod(state(h) / runs(h)[0]["run_id"], 0o720), "state_unsafe")
    corrupted("other-writable root", lambda h: os.chmod(state(h), 0o703), "state_unsafe")
    corrupted("group-writable journal", lambda h: os.chmod(journal(h), 0o620), "state_unsafe")

    def symlink_root(h):
        real = pathlib.Path(h.root, "elsewhere")
        state(h).rename(real)
        os.symlink(real, state(h))
    corrupted("symlinked state root", symlink_root, "state_unsafe")

    def symlink_journal(h):
        p = journal(h)
        other = pathlib.Path(h.root, "j.json")
        p.rename(other)
        os.symlink(other, p)
    corrupted("symlinked journal", symlink_journal, "state_unsafe")
    corrupted("unexpected entry", lambda h: (state(h) / "notes.txt").write_text("x"), "state_entry_unexpected")
    corrupted("unexpected run file", lambda h: (state(h) / runs(h)[0]["run_id"] / "x.json").write_text("{}"),
              "state_entry_unexpected")

    def two_heads(h):
        r = runs(h)[0]
        d = state(h) / "00000000000000ee"
        d.mkdir(mode=0o700)
        (d / "journal.json").write_bytes(resolver.canonical_json(dict(r, run_id="00000000000000ee")) + b"\n")
        for f in ("inventory.json", "plan.json", "validation.json"):
            shutil.copy(state(h) / r["run_id"] / f, d / f)
    corrupted("ambiguous sequence", two_heads, "history_ambiguous")

    def broken_link(h):
        outcome(reconcile(h), "complete")
        p = journal(h)                                # the first run: its successor binds its digest
        j = json.loads(p.read_text())
        j["maintenance"]["plan_digest"] = "sha256:" + "0" * 64
        p.write_bytes(resolver.canonical_json(j) + b"\n")
        h.commands.clear()
    corrupted("rewritten predecessor", broken_link, "history_inconsistent")
    # wrong owner (the fake host's owner identity stands in for root)
    h = preinstalled("owner")
    outcome(reconcile(h), "complete")
    other = copy.copy(h)
    other.owner_uid = h.owner_uid + 1
    rejects("state_unsafe", ho.load_history, other, ho.state_root(h, create=False))
    try:
        ho.state_root(other, create=False)
        raise Failure("foreign-owned state root accepted")
    except ho.OrchestrationError as e:
        expect(e.reason == "state_unsafe", e.reason)
    # crash debris (an unrenamed temporary) is preserved and never trusted
    h = preinstalled("debris")
    outcome(reconcile(h), "complete")
    debris = state(h) / runs(h)[0]["run_id"] / ".journal.json.00000000000000dd.tmp"
    debris.write_text("{")
    os.chmod(debris, 0o600)
    outcome(reconcile(h), "complete")
    expect(debris.read_text() == "{", "debris altered")
    # the root is fixed: nothing in the environment or the call moves it
    expect(ho.STATE_DIR == "var/lib/lubarchy/hardware-orchestration", "state root")
    return f"{n} corrupt/unsafe states (malformed, schema-invalid, non-canonical, altered/missing artifact, " \
           "group/other writable, symlinked root/journal, unexpected entries, ambiguous sequence, rewritten " \
           "predecessor) fail closed with exit 10, nothing mutated or rewritten, status reports the reason; " \
           "foreign owner rejected; crash debris preserved"


@check
def interrupted_journal_write_never_looks_complete():                      # 12 (durability)
    h = preinstalled("durability")
    calls = []
    saved = os.fsync

    def spy(fd):
        calls.append(os.readlink(f"/proc/self/fd/{fd}") if os.path.exists(f"/proc/self/fd/{fd}") else fd)
        return saved(fd)
    with patched(os, "fsync", spy):
        doc = outcome(reconcile(h), "complete")
    run_dir = str(state(h) / doc["run_id"])
    expect(any(str(c) == run_dir for c in calls), "run directory not fsynced")
    expect(any(str(c).startswith(run_dir + "/.journal.json.") for c in calls), "journal temporary not fsynced")
    # a failing durable write stops the run before any mutation
    h = Host("no-durability")
    real = pa.durable_replace

    def full(host, directory, name, data, mode):
        if name == "journal.json" and b'"applying"' in data:
            raise OSError(28, "No space left on device")
        return real(host, directory, name, data, mode)
    with patched(pa, "durable_replace", full):
        try:
            ho.reconcile(h)
            raise Failure("journal failure ignored")
        except OSError:
            pass
    expect(not apt_installs(h) and not apply_journals(h), "mutation after a failed journal write")
    expect(runs(h)[0]["phase"] == "planned" and not runs(h)[0]["terminal"], "partial transition looks complete")
    doc = outcome(reconcile(h), "reboot_required")
    expect(len(apt_installs(h)) == 1, "resume after the failed write")
    return "temporary file + fsync + rename + directory fsync; ENOSPC on the `applying` transition stops before " \
           "the apply executor runs; the previous durable phase stays visible"


# ---------------------------------------------------------------------------
# Caller authority, privilege, privacy, scope

@check
def caller_has_no_authority():                                              # 3-7, 41, 46, 47
    cli = gate11._load_cli(HARDWARE / "manage-hardware.py")
    for argv in ([], ["reconcile", "intel-microcode"], ["reconcile", "--package", "x"], ["reconcile", "--plan", "p"],
                 ["reconcile", "--device", "pci:0000:00:14.3"], ["reconcile", "--root", "/tmp"],
                 ["reconcile", "-o", "APT::Get::Fix-Broken=1"], ["reconcile", "--force"], ["reconcile", "--reboot"],
                 ["status", "--json"], ["reconcile", "--state", "/tmp"], ["apply"], ["recover"], ["gc"],
                 ["reconcile", "--attempt", "0000000000000001"], ["reconcile", "--env", "APT_CONFIG=/x"], [""]):
        with open(os.devnull, "w") as null, contextlib.redirect_stderr(null):
            expect(cli.main(argv) == ho.EXIT_USAGE, f"CLI accepted {argv}")
    with patched(cli.ho, "RealHost", lambda: type("NotRoot", (), {"euid": lambda self: 1000})()):
        with open(os.devnull, "w") as null, contextlib.redirect_stderr(null):
            expect(cli.main(["reconcile"]) == ho.EXIT_NOT_ROOT and cli.main(["status"]) == ho.EXIT_NOT_ROOT, "root")
    import inspect
    expect(list(inspect.signature(ho.reconcile).parameters) == ["host"] and
           list(inspect.signature(ho.status).parameters) == ["host"], "operations take caller input")
    expect(not inspect.signature(ho.RealHost).parameters, "RealHost takes parameters")
    # a hostile environment changes nothing (the module never reads it)
    h = preinstalled("env-a")
    base = reconcile(h)
    hostile = {"APT_CONFIG": "/tmp/evil", "DEBIAN_FRONTEND": "readline", "PATH": "/tmp/evil",
               "LUBARCHY_STATE": "/tmp/x", "PYTHONPATH": "/tmp/evil", "http_proxy": "http://evil"}
    saved = dict(os.environ)
    os.environ.update(hostile)
    try:
        h2 = preinstalled("env-b")
        doc = reconcile(h2)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    expect(doc["validation"]["result_digest"] == base["validation"]["result_digest"] and
           doc["outcome"] == base["outcome"] and not apt_any(h2), "environment influenced reconcile")
    expect(state(h2).is_dir() and not os.path.exists("/tmp/x"), "environment moved the state root")
    for f in ("hardware_orchestration.py", "manage-hardware.py"):
        src = (HARDWARE / f).read_text()
        tree = ast.parse(src)
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | \
            {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        imports = {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
        expect(not names & {"environ", "getenv", "putenv", "system", "popen", "Popen", "check_output", "execv",
                            "execve", "spawnv", "argparse", "ArgumentParser"}, f"{f}: {names & {'environ'}}")
        expect(not imports & {"subprocess", "shlex", "argparse", "pty"}, f"{f} imports {imports}")
        expect("shell=True" not in src and "APT_GET" not in src and "DPKG" not in src and "apt-get" not in src.replace(
            "apt-get update", ""), f"{f} builds a package-manager command")
    td = gate11._load_tool(REPO / "tools" / "hwe" / "trusted-deployment.py")
    root = "/root/lubarchy-m1o/" + "a" * 40
    for op, arg in (("hwe-status", "status"), ("hwe-reconcile", "reconcile")):
        expect(td.privileged_argv(root, op) == ["/usr/bin/env", "-i", "/usr/bin/python3", "-I", "-B",
                                                root + "/tree/hardware/manage-hardware.py", arg], f"launcher {op}")
    for op in ("apply", "recover", "gc-plan", "gc"):
        expect(td.privileged_argv(root, op)[5] == root + "/tree/hardware/apply-packages.py", f"launcher {op} moved")
    launched = []
    saved = (td.os.execv, td.os.geteuid, td.verify)
    td.os.execv = lambda path, args: launched.append(list(args))
    td.os.geteuid = lambda: 0
    td.verify = lambda r, c, t: {"entrypoint": os.path.join(r, "tree", td.ENTRYPOINT),
                                 "orchestrator_entrypoint": os.path.join(r, "tree", td.ORCHESTRATOR_ENTRYPOINT)}
    cwd = os.getcwd()
    try:
        td.main(["run-hwe-reconcile", "--root", root, "--commit", "c" * 40, "--tree", "d" * 40])
        td.verify = lambda r, c, t: {"entrypoint": os.path.join(r, "tree", td.ENTRYPOINT),
                                     "orchestrator_entrypoint": "/elsewhere/manage-hardware.py"}
        with open(os.devnull, "w") as null, contextlib.redirect_stderr(null):
            expect(td.main(["run-hwe-reconcile", "--root", root, "--commit", "c" * 40, "--tree", "d" * 40]) == 1,
                   "launcher accepted an unverified orchestrator entrypoint")
    finally:
        td.os.execv, td.os.geteuid, td.verify = saved
        os.chdir(cwd)
    expect(launched == [td.privileged_argv(root, "hwe-reconcile")], f"launched {launched}")
    expect("hardware_orchestration" in td.EXECUTOR_MODULES, "import probe does not cover the coordinator")
    return "17 caller-controlled argv forms rejected (exit 64); non-root exit 77; operations take only the host; " \
           "hostile environment has no effect; no environ/subprocess/shell/argparse/package-manager argv in the " \
           "coordinator; launcher runs only the verified entrypoint with env -i python3 -I -B"


@check
def status_is_read_only_and_bounded():                                      # 24 (brief), 42
    h = Host("status")
    st = ho.status(h)
    expect(st["state"] == "none" and st["latest"] is None and not state(h).exists(), f"empty status {st}")
    expect(not os.path.exists(os.path.join(h.root, pa.LOCK_DIR, ho.LOCK_NAME)), "status created the lock")
    doc = reconcile(h)
    before = snapshot(h.root)
    st = ho.status(h)
    expect(snapshot(h.root) == before, "status mutated the host root")
    expect(st["state"] == "valid" and st["latest"]["outcome"] == "reboot_required" and st["reboot"]["required"] and
           st["reboot"]["boot_changed"] is False and st["latest"]["apply_attempt"] == doc["package"]["apply"][
               "attempt_id"], f"status {st}")
    expect(st["current"] == {"determinable": True, "current": True, "changed": []}, f"current {st['current']}")
    h.reboot()
    st = ho.status(h)
    expect(st["reboot"]["boot_changed"] is True and st["current"]["current"] is False and
           st["current"]["changed"] == ["boot_id_digest"], f"after reboot {st}")
    expect(runs(h)[-1] == doc and len(runs(h)) == 1, "status wrote a run")
    # recovery-required state is reported, not acted on
    h = interrupted_by_reconcile("status-recovery", "trigger")
    before = snapshot(h.root)
    st = ho.status(h)
    expect(st["package"]["recovery_required"] and st["latest"]["interrupted"] and snapshot(h.root) == before,
           f"status {st['package']}")
    text = resolver.canonical_json(st).decode()
    expect("6c2d1f0e" not in text and len(text) < 4096, "status leaks a raw boot id or is unbounded")
    return "status takes no lock, creates nothing, writes nothing (byte-identical root); reports outcome, apply " \
           "identity, pending reboot, boot change, currentness and recovery need"


@check
def privacy_and_canonical_persistence():                                    # 43, 44, 45
    h = Host("privacy")
    reconcile(h)
    h.reboot()
    reconcile(h)
    blob = b"".join(p.read_bytes() for p in state(h).rglob("*") if p.is_file())
    expect(blob and h.boot_id.encode() in pathlib.Path(h.root, "proc/sys/kernel/random/boot_id").read_bytes(),
           "privacy probe has no raw boot id to look for")
    for raw in ("6c2d1f0e", h.boot_id, "boot-id"):
        expect(raw.encode() not in blob, f"raw boot identity {raw} persisted")
    for pat in (rb"\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b", rb"\b\d{1,3}(?:\.\d{1,3}){3}\b", rb"/home/", rb'"at":',
                rb"hostname", rb"serial", rb"username"):
        expect(not re.search(pat, blob, re.I), f"persisted data matches {pat!r}")
    for r in runs(h):
        keys = set()

        def collect(v):
            if isinstance(v, dict):
                keys.update(v)
                for x in v.values():
                    collect(x)
            elif isinstance(v, list):
                for x in v:
                    collect(x)
        collect(r)
        expect(not {k for k in keys if re.search(r"time|date|host|user|address|serial|raw", k)}, f"keys {keys}")
    return "no raw boot id, MAC, IP, home path, timestamp, hostname, serial or user name in any persisted file"


@check
def gc_uses_m1n_and_failure_is_separate():                                  # 29-33
    h = Host("gc-fails")
    outcome(reconcile(h), "reboot_required")
    h.reboot()
    with patched(gc, "collect_garbage", lambda host: (_ for _ in ()).throw(gc.GCError("unsafe_root", "injected"))):
        doc = outcome(reconcile(h), "complete", "converged")
    expect(doc["maintenance"]["gc"] == "failed" and doc["validation"]["status"] == "PASS", "GC failure hidden")
    expect(ho.exit_status(doc) == ho.EXIT_MAINTENANCE_FAILED, "GC failure reported as success")
    h2 = Host("gc-partial")
    outcome(reconcile(h2), "reboot_required")
    h2.reboot()
    real = gc.delete_bundle
    with patched(gc, "delete_bundle", lambda host, name, ident: (_ for _ in ()).throw(gc.Unsafe("unsafe_object"))):
        doc = outcome(reconcile(h2), "complete")
    expect(doc["maintenance"]["gc"] == "failed" and doc["maintenance"]["receipt_id"] and
           ho.exit_status(doc) == ho.EXIT_MAINTENANCE_FAILED, f"failed deletion {doc['maintenance']}")
    rec = json.loads(next(p for p in receipts(h2) if doc["maintenance"]["receipt_id"] in p.name).read_text())
    expect(rec["result"] == "failed" and resolver.digest(rec) == doc["maintenance"]["receipt_digest"], "receipt")
    del real
    # package state that became unresolved after validation (an external low-level run) blocks GC
    h3 = Host("gc-unresolved")
    outcome(reconcile(h3), "reboot_required")
    h3.reboot()

    def external_crash(ev):
        d = pathlib.Path(h3.root, pa.JOURNAL_DIR, "00000000000000ab")
        d.mkdir(mode=0o700)
        (d / "journal.json").write_text("{")
    h3.fail["evidence"] = [external_crash]
    doc = outcome(reconcile(h3), "complete")
    expect(doc["maintenance"] == {"gc": "not_run", "skipped": "package_state_unresolved", "receipt_id": None,
                                  "receipt_digest": None, "plan_digest": None} and bundles(h3) and not receipts(h3),
           f"GC with unresolved package state {doc['maintenance']}")
    # GC only through M1-N: the coordinator deletes nothing itself
    src = (HARDWARE / "hardware_orchestration.py").read_text()
    tree = ast.parse(src)
    calls = {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
             for n in ast.walk(tree) if isinstance(n, ast.Call)}
    expect(not calls & {"unlink", "rmdir", "remove", "rmtree", "removedirs", "rename", "replace", "truncate"},
           f"coordinator deletes or renames itself: {calls & {'unlink', 'rmdir', 'remove', 'rmtree'}}")
    expect("gc.collect_garbage(host)" in src and "compute_plan" not in src and "delete_bundle" not in src,
           "GC not delegated to M1-N")
    return "GC exception -> maintenance failed, hardware outcome kept (PASS/complete), exit 6; failed deletion " \
           "receipt bound; coordinator never unlinks/rmdirs/renames itself; only gc.collect_garbage"


def partial_of(h, attempt):
    return os.path.join(h.root, pa.JOURNAL_DIR, attempt, pa.APPLY_CACHE, gc.CACHE_PARTIAL)


@check
def corrected_apt_ownership_and_gc_integration():                           # M1-N correction in M1-O
    h = Host("apt-owner")
    doc = outcome(reconcile(h), "reboot_required")
    att = doc["package"]["apply"]["attempt_id"]
    part = partial_of(h, att)
    st = gc._lstat(part)
    expect(stat.S_ISDIR(st.st_mode) and (st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)) ==
           (gate14.FAKE_APT_UID, h.owner_gid, 0o700) and not os.listdir(part),
           "the real APT run is not modelled with the qualified _apt:root 0700 empty partial/")
    plan = gc.compute_plan(h)[0]
    bundle = doc["package"]["apply"]["transaction_digest"][7:]
    cand = next(c for c in plan["candidates"] if c["name"] == bundle)
    expect((cand["classification"], cand["reason"]) == ("eligible_complete", "complete_validated") and
           cand["apply_caches"] == [att], f"M1-N does not see the real-shaped bundle as eligible: {cand}")
    expect(doc["maintenance"]["gc"] == "not_run" and not receipts(h), "GC ran before reboot validation")
    # the old root-owned-partial assumption is not what makes it eligible: without APT's ownership the
    # same bundle is retained by M1-N
    key = (os.lstat(part).st_dev, os.lstat(part).st_ino)
    saved = gate14.OVERLAY.pop(key)
    c2 = next(c for c in gc.compute_plan(h)[0]["candidates"] if c["name"] == bundle)
    gate14.OVERLAY[key] = saved
    expect((c2["classification"], c2["reason"]) == ("invalid_or_ambiguous", "unsafe_object"),
           f"root-owned partial/ after APT accepted: {c2['classification']}")
    h.reboot()
    doc = outcome(reconcile(h), "complete")
    m = doc["maintenance"]
    rec = json.loads(next(p for p in receipts(h) if m["receipt_id"] in p.name).read_text())
    expect(m["gc"] == "complete" and rec["removed"] == [{"kind": "bundle", "name": bundle, "bytes": cand["bytes"]}]
           and resolver.digest(rec) == m["receipt_digest"], f"M1-N GC result {m}")
    expect(not os.path.exists(part) and bundle not in bundles(h) and key not in gate14.OVERLAY and
           os.listdir(os.path.dirname(part)) == [] and
           os.path.exists(os.path.join(h.root, pa.JOURNAL_DIR, att, pa.JOURNAL_FILE)),
           "reclaimed state wrong (bundle, cache links, partial/, journal)")
    doc = outcome(reconcile(h), "complete")
    expect(doc["maintenance"]["gc"] == "nothing_to_collect" and len(receipts(h)) == 1 and not apt_any(h),
           "a later reconcile reclaimed again")
    # an unsafe candidate refused by M1-N: the hardware outcome is unaffected, nothing is deleted
    h2 = Host("apt-owner-unsafe")
    d2 = outcome(reconcile(h2), "reboot_required")
    part2 = partial_of(h2, d2["package"]["apply"]["attempt_id"])
    gate14.own(part2, gate14.FAKE_APT_UID + 4000, h2.owner_gid, 0o700)
    h2.reboot()
    for _ in range(2):
        doc = outcome(reconcile(h2), "complete")
        expect(doc["maintenance"]["gc"] == "nothing_to_collect" and bundles(h2) and os.path.isdir(part2) and
               not receipts(h2) and ho.exit_status(doc) == ho.EXIT_OK, f"unsafe candidate {doc['maintenance']}")
    return "real-shaped APT run leaves _apt:root 0700 empty partial/; M1-N sees the bundle eligible (and retains it " \
           "without APT's ownership); no GC before reboot validation; post-reboot M1-N GC reclaims bundle, cache " \
           "links and partial/, journal kept; next run nothing_to_collect; an M1-N-refused candidate is retained " \
           "without affecting the outcome"


@check
def critical_failure_outranks_reboot():                                     # M1-O precedence correction
    h = Host("fail-first")

    def unbind(ev):
        for d in ev["devices"].values():
            d["driver"], d["modules"] = None, []
    h.fail["evidence"] = [unbind]
    doc = outcome(reconcile(h), "validation_failed", "validation_failed")
    v = doc["validation"]
    expect(v["status"] == "FAIL" and v["boot_relation"] == "same_boot" and v["reboot_required"] and
           not doc["reboot_required"], f"same-boot FAIL not primary: {v} {doc['reboot_required']}")
    art = (state(h) / doc["run_id"] / "validation.json").read_bytes()
    result = json.loads(art)
    expect(resolver.digest(result) == v["result_digest"] and result["status"] == "FAIL" and
           result["bindings"]["boot"]["relation"] == "same_boot", "validator evidence rewritten")
    expect(doc["maintenance"]["gc"] == "not_run" and not receipts(h) and h.reboots == 0 and
           ho.exit_status(doc) == ho.EXIT_VALIDATION_FAILED, "failed run collected, rebooted or exited wrongly")
    st = ho.status(h)
    expect(st["latest"]["outcome"] == "validation_failed" and st["latest"]["reason"] == "validation_failed" and
           not st["reboot"]["required"] and not st["latest"]["reboot_required"] and
           st["validation"]["status"] == "FAIL", f"status hides the failure: {st}")
    doc = outcome(reconcile(h), "validation_failed")
    expect(doc["awaiting_reboot"] is None and doc["validation"]["mode"] == "current" and
           (state(h) / runs(h)[0]["run_id"] / "validation.json").read_bytes() == art,
           "a failed run became a reboot continuation (or its artifact changed)")
    # the failure is corrected externally; the same boot still genuinely needs a reboot
    h.fail.pop("evidence")
    doc = outcome(reconcile(h), "reboot_required", "reboot_pending")
    expect(doc["awaiting_reboot"] is None and doc["reboot_required"] and not apt_any(h),
           "legitimate reboot requirement suppressed after the failure was corrected")
    # FAIL after the reboot is still validation_failed
    h.reboot()
    h.fail["evidence"] = [unbind]
    doc = outcome(reconcile(h), "validation_failed")
    expect(doc["boot"]["boot_changed"] and doc["validation"]["boot_relation"] == "post_reboot" and
           doc["validation"]["mode"] == "revalidation" and not doc["reboot_required"] and
           doc["maintenance"]["gc"] == "not_run", "post-reboot FAIL mishandled")
    # contradictory journals fail closed
    n = 0
    base = copy.deepcopy(runs(h)[0])
    for label, edit in (
            ("reboot_required over FAIL", lambda d: d.update(outcome="reboot_required", reason="reboot_pending",
                                                             reboot_required=True)),
            ("validation_failed without FAIL", lambda d: d["validation"].update(status="LIMITED", critical_failures=0)),
            ("actionable reboot on a failed run", lambda d: d.update(reboot_required=True)),
            ("complete with FAIL", lambda d: d.update(outcome="complete", reason="converged")),
            ("GC after validation_failed", lambda d: (d["phases"].insert(-1, "collecting"),
                                                      d["maintenance"].update(gc="nothing_to_collect")))):
        bad = copy.deepcopy(base)
        edit(bad)
        try:
            ho.check_journal(bad)
            raise Failure(f"contract accepted: {label}")
        except ho.OrchestrationError:
            n += 1
    # an awaiting-reboot continuation derived from a validation_failed run is rejected by the history
    h2 = Host("fail-continuation")
    h2.fail["evidence"] = [unbind]
    first = outcome(reconcile(h2), "validation_failed")
    h2.fail.pop("evidence")
    outcome(reconcile(h2), "reboot_required")
    second = runs(h2)[1]
    second["awaiting_reboot"] = {"run_id": first["run_id"], "boot_id_digest": first["boot"]["boot_id_digest"],
                                 "validation_digest": first["validation"]["result_digest"]}
    second["boot"]["previous_boot_id_digest"] = first["boot"]["boot_id_digest"]
    second["boot"]["boot_changed"] = False
    second["validation"]["continuity"] = "profile_changed"
    ho.check_journal(second)
    (state(h2) / second["run_id"] / "journal.json").write_bytes(resolver.canonical_json(second) + b"\n")
    rejects("history_inconsistent", reconcile, h2)
    n += 1
    return f"same-boot critical FAIL -> validation_failed (no awaiting_reboot, no GC, no reboot, artifact exact, " \
           f"status primary); corrected failure -> reboot_required; post-reboot FAIL -> validation_failed; " \
           f"{n} contradictory journal/history states fail closed"


@check
def no_reboot_service_nvidia_or_secure_boot():                              # 18, 48-50
    src = (HARDWARE / "hardware_orchestration.py").read_text().lower()
    cli = (HARDWARE / "manage-hardware.py").read_text().lower()
    for bad in ("systemctl", "shutdown", "/sbin/reboot", "/usr/sbin/reboot", "kexec", "reboot -", "init 6",
                "sysrq", "nvidia", "mokutil", "sbsign", "secureboot", "efivar", "dbus", "socket", "daemon(",
                "timer"):
        expect(bad not in src and bad not in cli, f"coordinator mentions {bad}")
    for f in ("hardware_orchestration.py", "manage-hardware.py"):
        tree = ast.parse((HARDWARE / f).read_text())
        calls = {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
                 for n in ast.walk(tree) if isinstance(n, ast.Call)}
        expect(not calls & {"reboot", "shutdown", "system", "execv", "execve", "Popen", "run", "call", "kill"},
               f"{f} calls {calls & {'reboot', 'shutdown', 'system', 'run'}}")
    files = subprocess.run(["git", "-C", str(REPO), "ls-files"], capture_output=True, text=True,
                           check=True).stdout.split()
    units = [f for f in files if re.search(r"\.(service|timer|socket|path)$", f) or "systemd" in f.split("/")]
    expect(not units, f"service/timer files {units}")
    pol = json.loads((HARDWARE / "nvidia" / "apt-policy-v1.json").read_text(encoding="utf-8"))
    expect(pol["active"] is False, "NVIDIA policy activated")
    expect(not any("nvidia" in p["name"] for r in gate9.RULES for p in r.get("effects", {}).get("packages", [])),
           "NVIDIA package in production rules")
    return "no reboot/shutdown/kexec, systemd unit, timer, socket, D-Bus or daemon; NVIDIA inactive; no MOK/Secure " \
           "Boot code"


def main():
    sys.stdout.reconfigure(line_buffering=True)
    from importlib.metadata import version
    print(f"python {sys.version.split()[0]}, jsonschema {version('jsonschema')}, dpkg-deb {shutil.which('dpkg-deb')}")
    if shutil.which("dpkg-deb") != ps.DPKG_DEB:
        print(f"FAIL: hardware orchestration needs {ps.DPKG_DEB}", file=sys.stderr)
        return 1
    failures = 0
    old = os.umask(0o022)
    try:
        with tempfile.TemporaryDirectory(prefix="lubarchy-gate15-") as tmp:
            gate11.TMP = gate10.TMP = gate14.gate11.TMP = gate14.gate10.TMP = os.path.realpath(tmp)
            gc._lstat = gate14.overlay_lstat
            for name, fn in gate14.TRACKED.items():
                setattr(os, name, fn)
            for fn in CHECKS:
                try:
                    detail = fn()
                except Exception as e:  # noqa: BLE001 - every failure is reported
                    failures += 1
                    print(f"FAIL: {fn.__name__}: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
                    continue
                print(f"ok: {fn.__name__}" + (f": {detail}" if detail else ""))
    finally:
        gc._lstat = gate14.REAL_LSTAT
        for name, fn in gate14.REAL_OS.items():
            setattr(os, name, fn)
        os.umask(old)
    if failures:
        print(f"FAIL: hardware orchestration ({failures} of {len(CHECKS)} checks failed)", file=sys.stderr)
        return 1
    print(f"PASS: hardware orchestration ({len(CHECKS)} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
