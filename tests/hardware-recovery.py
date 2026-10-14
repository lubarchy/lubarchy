#!/usr/bin/python3
"""LUBARCHY hardware package recovery gate (M1-L; synthetic, non-mutating).

Drives hardware/package_recovery.py (via the gate-11 fake host, rooted in a
temporary directory) through interrupted apply attempts whose package states
are modelled on the states measured on the Debian 13 builder: an interrupted
initramfs-tools trigger (closure installed, initramfs-tools half-configured),
an interruption at the start of dpkg --unpack (no package changed),
interruptions before `applying` and after APT completed, plus unpacked,
half-installed and partially absent closure packages and unrelated damage.
Process death is simulated by an exception the executor cannot catch, so
journals stay exactly where a real SIGKILL leaves them. Fake dpkg recovery
commands change only the fake state. It never runs apt-get or a mutating
dpkg, needs no root, contacts no network and never inspects the runner.
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
import tempfile  # noqa: E402

TESTS = pathlib.Path(__file__).resolve().parent
REPO = TESTS.parent
HARDWARE = REPO / "hardware"
sys.path.insert(0, str(HARDWARE))
import package_apply as pa  # noqa: E402
import package_recovery as pr  # noqa: E402
import package_staging as ps  # noqa: E402
import package_transaction as pt  # noqa: E402
import resolver  # noqa: E402
import source_activation as sa  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate11 = _load("gate11", TESTS / "hardware-apply.py")
gate10 = gate11.gate10
gate9 = gate11.gate9
sys.path.remove(str(HARDWARE))

RECOVERY_SCHEMA_SHA256 = "e675420f8bcbd5617347d49c83014c2f51aa9dc8dd477a55fb8f7e449eebef9e"
INSTALLED = "install ok installed"


class Failure(Exception):
    pass


def expect(cond, message):
    if not cond:
        raise Failure(message)


CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


class Killed(BaseException):
    """Simulated SIGKILL: the executor cannot catch it; journals stay where they were."""


def rejects(code, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except pa.ApplyError as e:
        expect(e.code == code, f"expected {code}, got {e.code}: {e.message}")
        return e
    raise Failure(f"expected {code}, but it was accepted")


# ---------------------------------------------------------------------------
# Fake host with interruptions and fake exact-package dpkg recovery

class RecBackend(gate11.FakeBackend):
    def auto_text(self):
        # like apt-mark showauto: every automatic package that has a current version (any installed state)
        return "".join(e["package"] + "\n" for e in self.u["installed"]
                       if e.get("automatic") and not e["status"].endswith((" not-installed", " config-files")))


MANDB = ("man-db", "amd64", "2.13.1-1")
INITRAMFS = ("initramfs-tools", "all", "0.148.4")
LOG_HISTORY = ("2026-10-01 09:00:00 startup archives unpack\n"
               "2026-10-01 09:00:01 install base-files:amd64 13.8 13.8+deb13u1\n"
               "2026-10-01 09:00:02 status installed base-files:amd64 13.8+deb13u1\n")
# Where each simulated SIGKILL lands in the measured dpkg.log sequence (inclusive).
CUT = {"trigger": "status half-configured initramfs-tools:all 0.148.4",
       "unrelated": "status half-configured initramfs-tools:all 0.148.4",
       "unrelated-log": "status half-configured initramfs-tools:all 0.148.4",
       "triggers-pending": "status triggers-pending initramfs-tools:all 0.148.4",
       "closure-unpacked": "startup packages configure",
       "half-installed": "status half-installed intel-microcode:amd64 ",
       "partial-absent": "status unpacked iucode-tool:amd64 "}


class RecHost(gate11.FakeHost):
    """Fake host whose dpkg.log follows the sequence measured on the builder and whose
    package state after an interruption is the replay of exactly the logged prefix."""

    def backend(self):
        self._event("backend")
        return RecBackend(self, self.universe())

    def __init__(self, name, interrupt=None, pre_status=None, extra_triggers=None, **kw):
        pre_status = pre_status or {}
        installed = list(kw.pop("installed", ())) + [
            {"package": n, "architecture": a, "version": v, "status": pre_status.get(n, INSTALLED), "automatic": auto}
            for (n, a, v), auto in ((INITRAMFS, True), (MANDB, False))]
        super().__init__(name, installed=installed, **kw)
        self.interrupt = interrupt
        self.kill_next_run = False
        self.handlers = []
        info = pathlib.Path(self.root, "var/lib/dpkg/info")
        info.mkdir(parents=True, exist_ok=True)
        (info / "initramfs-tools.triggers").write_text("interest-noawait update-initramfs\n")
        (info / "man-db.triggers").write_text("interest-noawait /usr/share/man\n")
        for n, text in (extra_triggers or {}).items():
            (info / f"{n}.triggers").write_text(text)
        os.makedirs(os.path.join(self.root, "var/log"), exist_ok=True)
        self.dpkg_log = pathlib.Path(self.root, pr.DPKG_LOG)
        self.dpkg_log.write_text(LOG_HISTORY)

    def entry(self, name):
        return next((e for e in self.u_after["installed"] if e["package"] == name), None)

    def write_log(self, *events):
        with open(self.dpkg_log, "a") as f:
            f.write("".join(f"2026-10-14 15:13:33 {e}\n" for e in events))

    def transaction_events(self, closure):
        """The dpkg.log of an uninterrupted run, as measured: unpack (man-db file trigger
        activated during the first unpack), configure (intel-microcode's postinst activates
        update-initramfs), then trigger processing."""
        ev = ["startup archives unpack"]
        for i, o in enumerate(closure):
            n, v = f"{o['package']}:{o['architecture']}", o["version"]
            ev += [f"install {n} <none> {v}", f"status half-installed {n} {v}"]
            if i == 0:
                ev.append("status triggers-pending man-db:amd64 2.13.1-1")
            ev.append(f"status unpacked {n} {v}")
        ev.append("startup packages configure")
        for o in closure:
            n, v = f"{o['package']}:{o['architecture']}", o["version"]
            ev += [f"configure {n} {v} <none>", f"status unpacked {n} {v}", f"status half-configured {n} {v}",
                   f"status installed {n} {v}"]
            if o["package"] == "intel-microcode":
                ev.append("status triggers-pending initramfs-tools:all 0.148.4")
        ev += ["trigproc man-db:amd64 2.13.1-1 <none>", "status half-configured man-db:amd64 2.13.1-1",
               "status installed man-db:amd64 2.13.1-1", "trigproc initramfs-tools:all 0.148.4 <none>",
               "status half-configured initramfs-tools:all 0.148.4", "status installed initramfs-tools:all 0.148.4"]
        return ev

    def run(self, argv):
        if self.kill_next_run:
            self.kill_next_run = False
            raise Killed()
        if argv[:2] == [pa.DPKG, "--audit"]:
            expect(pa.allowed_command(argv, self.root), "audit not allowlisted")
            self.commands.append(list(argv))
            bad = [e["package"] for e in self.u_after["installed"] if e["status"] != INSTALLED]
            return 0, ("The following packages are only half configured:\n " + "\n ".join(bad) + "\n") if bad else ""
        if argv[:2] == [pa.APT_GET, "check"]:
            self.commands.append(list(argv))
            bad = any(e["status"] in ("install ok half-configured", "install ok unpacked", "install ok half-installed")
                      for e in self.u_after["installed"])
            return (100, "E: broken\n") if bad else (0, "")
        if argv[:2] in ([pa.DPKG, "--configure"], [pa.DPKG, "--triggers-only"]):
            expect(pa.allowed_command(argv, self.root), f"recovery argv not allowlisted {argv}")
            self.commands.append(list(argv))
            return self._dpkg(argv)
        return super().run(argv)

    # postinst activations measured on Debian 13: intel-microcode's postinst and initramfs-tools'
    # own postinst run `update-initramfs -u`, which under dpkg defers via dpkg-trigger.
    ACTIVATES = {"intel-microcode": ["initramfs-tools"], "initramfs-tools": ["initramfs-tools"]}

    def _key(self, e):
        return f"{e['package']}:{e['architecture']} {e['version']}"

    def _activate(self, names):
        for n in names:
            e = self.entry(n)
            if e is not None and e["status"] in (INSTALLED, "install ok triggers-pending"):
                e["status"] = "install ok triggers-pending"
                self.write_log(f"status triggers-pending {self._key(e)}")

    def _trigproc(self, e):
        self.handlers.append(("trigproc", e["package"]))
        self.write_log(f"trigproc {self._key(e)} <none>", f"status half-configured {self._key(e)}")
        e["status"] = INSTALLED
        self.write_log(f"status installed {self._key(e)}")
        self._activate(self.fail.get("trigproc_activates", {}).get(e["package"], []))

    def _dpkg(self, argv):
        """Fake dpkg with the measured semantics: `--configure` runs the postinst (which may
        activate triggers) and, unless --no-triggers, then processes EVERY pending trigger;
        `--triggers-only <names>` processes only the named packages' pending triggers."""
        if self.fail.get("recover_dpkg"):
            if self.fail.get("recover_dpkg_logged"):
                self.write_log("startup packages configure", "configure initramfs-tools:all 0.148.4 <none>",
                               "status half-configured initramfs-tools:all 0.148.4")
            return 1, "dpkg: error (injected)\n"
        for hook in self.fail.get("during_dpkg", []):
            hook(self, argv)
        if argv[1] == "--configure":
            no_triggers = argv[2] == "--no-triggers"
            names = argv[3:] if no_triggers else argv[2:]
            self.write_log("startup packages configure")
            for n in names:
                e = self.entry(n)
                if e is None or e["status"] not in ("install ok unpacked", "install ok half-configured"):
                    return 1, f"dpkg: error processing package {n}\n"
                self.handlers.append(("configure", n))
                self.write_log(f"configure {self._key(e)} <none>", f"status half-configured {self._key(e)}")
                e["status"] = INSTALLED
                self.write_log(f"status installed {self._key(e)}")
                self._activate(self.ACTIVATES.get(n, []) + self.fail.get("activates", {}).get(n, []))
            if not no_triggers:
                for e in list(self.u_after["installed"]):
                    if e["status"] == "install ok triggers-pending":
                        self._trigproc(e)
        else:
            self.write_log("startup packages triggers-only")
            for n in argv[2:]:
                e = self.entry(n)
                if e is None or e["status"] != "install ok triggers-pending":
                    return 1, f"dpkg: error processing package {n}\n"
                self._trigproc(e)
        for mutate in self.fail.get("after_recover", []):
            mutate(self.u_after)
        return 0, ""

    def _apt_install(self, argv):
        mode = self.interrupt
        if mode == "before-unpack":
            self.write_log("startup archives unpack")
            raise Killed()
        tx = pt.compile_transaction(self.inv, gate9.RULES, gate11.FakeBackend(self, self.u_after))
        rc, out = super()._apt_install(argv)
        closure = sorted((o for o in tx["operations"] if o["action"] == "install"),
                         key=lambda o: o["relation"] != "dependency")
        events = self.transaction_events(closure)
        if mode in CUT:
            events = events[:next(i for i, e in enumerate(events) if e.startswith(CUT[mode])) + 1]
        self.write_log(*events)
        state = {}
        for e in events:
            w = e.split(" ")
            if w[0] == "status":
                state[w[2]] = w[1]
        for o in closure:
            k = f"{o['package']}:{o['architecture']}"
            if k not in state:
                self.u_after["installed"].remove(self.entry(o["package"]))
        for e in self.u_after["installed"]:
            k = f"{e['package']}:{e['architecture']}"
            if k in state:
                e["status"] = "install ok " + state[k]
        if mode in ("unrelated", "unrelated-log"):
            self.u_after["installed"].append({"package": "hello", "architecture": "amd64", "version": "2.10-5",
                                              "status": "install ok unpacked"})
            if mode == "unrelated-log":
                self.write_log("startup archives unpack", "install hello:amd64 <none> 2.10-5",
                         "status half-installed hello:amd64 2.10-5", "status unpacked hello:amd64 2.10-5")
        for mutate in self.fail.get("during_apt", []):
            mutate(self)
        if mode is None:
            return rc, out
        if mode == "after-apt":
            self.kill_next_run = True
            return rc, out
        raise Killed()


def interrupted(name, mode, **kw):
    """Run the real executor until the simulated kill; return the host."""
    h = RecHost(name, interrupt=mode, **kw)
    try:
        pa.apply_current_plan(h)
        raise Failure(f"{mode}: apply was not interrupted")
    except Killed:
        pass
    return h


def recover(h):
    h.commands.clear()
    h.handlers.clear()
    return pr.recover_interrupted_transaction(h)


def apt_ran(h):
    return any(c[:1] == [pa.APT_GET] and c[1:2] != ["check"] for c in h.commands)


def apply_journal(h):
    return gate11.journals(h)[-1]


def recovery_journals(h):
    return [json.loads(p.read_text()) for p in sorted(pathlib.Path(h.root, pr.RECOVERY_DIR).glob("*/journal.json"))]


def manual(h, reason_part):
    doc = recover(h)
    expect(doc["result"] == "manual_recovery_required" and doc["classification"] == "manual_recovery_required",
           f"expected manual recovery, got {doc.get('result')} {doc.get('classification')}")
    expect(reason_part in (doc["reason"] or ""), f"reason {doc['reason']!r} lacks {reason_part!r}")
    expect(not doc["mutation_started"] and doc["recovery_argv"] == [], "manual case mutated")
    expect(not [c for c in h.commands if c[:2] in ([pa.DPKG, "--configure"], [pa.DPKG, "--triggers-only"])]
           and not apt_ran(h), f"manual case ran {h.commands}")
    rejects("recovery_required", pa.apply_current_plan, h)       # apply stays blocked
    return doc


# ---------------------------------------------------------------------------
# Contract

@check
def recovery_journal_schema_is_closed_and_protected():
    import jsonschema
    schema = json.loads((HARDWARE / "schemas" / pr.RECOVERY_SCHEMA_FILE).read_text(encoding="utf-8"))
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
    sha = hashlib.sha256((HARDWARE / "schemas" / pr.RECOVERY_SCHEMA_FILE).read_bytes()).hexdigest()
    expect(sha == RECOVERY_SCHEMA_SHA256, f"recovery-journal schema changed ({sha})")
    expect(gate10.PROTECTED_SCHEMAS.get(pr.RECOVERY_SCHEMA_FILE) == sha, "not in the gate-10 protected baseline")
    return "recovery-journal v1 meta-valid, closed, protected baseline matches"


# ---------------------------------------------------------------------------
# Classifications (1-3, 18, 29-30)

@check
def no_mutation_is_abandoned_with_bounded_cleanup():                 # 1, 39
    h = RecHost("no-mutation")
    real = pa.durable_replace

    def die_before_applying(host, directory, name, data, mode):
        if name == pa.JOURNAL_FILE and b'"phase":"applying"' in data:
            raise Killed()
        return real(host, directory, name, data, mode)
    pa.durable_replace = die_before_applying
    try:
        pa.apply_current_plan(h)
        raise Failure("not interrupted")
    except Killed:
        pass
    finally:
        pa.durable_replace = real
    att = apply_journal(h)
    expect(att["phase"] == "bundle_verified" and att["apply_started"] is False, f"apply journal {att['phase']}")
    cache = pathlib.Path(h.root, pa.JOURNAL_DIR, att["attempt_id"], pa.APPLY_CACHE)
    expect(cache.is_dir(), "apply cache missing before recovery")
    pathlib.Path(h.root, pa.STAGING_DIR, ".staging-leftover").mkdir()
    rejects("recovery_required", pa.apply_current_plan, h)
    doc = recover(h)
    expect(doc["classification"] == "no_mutation" and doc["result"] == "abandoned" and doc["action"] == "finalize",
           f"{doc['classification']} {doc['result']}")
    expect(not cache.exists() and not pathlib.Path(h.root, pa.STAGING_DIR, ".staging-leftover").exists(),
           "attempt-local state not cleaned")
    expect(pathlib.Path(h.root, pa.JOURNAL_DIR, att["attempt_id"], pa.JOURNAL_FILE).exists(), "journal erased")
    expect(not apt_ran(h) and not any(c[1:2] in (["--configure"], ["--triggers-only"]) for c in h.commands),
           "no-mutation recovery ran package commands")
    expect(gate11.owned(h).exists(), "owned source must be retained (documented policy)")
    # the retained bundle of the abandoned attempt is stale once metadata moved: removed and restaged
    pathlib.Path(h.list_file(h.u_after["repositories"]["trixie-main"])).write_text("Package: moved\n")
    staged = next(pathlib.Path(h.root, pa.STAGING_DIR).glob("[0-9a-f]*/manifest.json"))
    old_meta = json.loads(staged.read_text())["repository_metadata_digest"]
    real_write = h._write_lists
    h._write_lists = lambda: None                    # keep the moved index for this run
    try:
        expect(pa.apply_current_plan(h)["result"] == "complete", "a resolved no-mutation attempt still blocks apply")
    finally:
        h._write_lists = real_write
    expect(json.loads(staged.read_text())["repository_metadata_digest"] != old_meta, "stale bundle was reused")
    # stopped before applying, but the package state changed anyway -> never abandoned
    h3 = RecHost("no-mutation-changed")
    pa.durable_replace = die_before_applying
    try:
        pa.apply_current_plan(h3)
    except Killed:
        pass
    finally:
        pa.durable_replace = real
    h3.entry("libc6")["version"] = "2.41-99"
    manual(h3, "package state changed although no LUBARCHY package mutation started")
    # bounded cleanup: an unexpected entry in the apply cache is never deleted
    h2 = RecHost("no-mutation-bad-cache")
    pa.durable_replace = die_before_applying
    try:
        pa.apply_current_plan(h2)
    except Killed:
        pass
    finally:
        pa.durable_replace = real
    att2 = apply_journal(h2)
    bundle2 = next(pathlib.Path(h2.root, pa.STAGING_DIR).glob("[0-9a-f]*/manifest.json")).parent
    inode = (bundle2 / "manifest.json").stat().st_ino
    stray = pathlib.Path(h2.root, pa.JOURNAL_DIR, att2["attempt_id"], pa.APPLY_CACHE, "notes.txt")
    stray.write_text("x")
    doc = recover(h2)
    expect(doc["result"] == "manual_recovery_required" and stray.exists(), "cleanup deleted an unknown entry")
    expect((bundle2 / "manifest.json").stat().st_ino == inode, "bundle touched by a refused cleanup")
    return "stopped before applying -> abandoned; apply cache + .staging-* removed, journal kept, owned source kept"


@check
def apt_started_without_package_change():
    h = interrupted("no-change", "before-unpack")
    expect(apply_journal(h)["phase"] == "applying", "journal not at applying")
    doc = recover(h)
    expect(doc["classification"] == "no_package_change" and doc["result"] == "abandoned", doc["classification"])
    expect(doc["observed"]["pre_state_reconstructed"] is True, "pre-state not proven")
    expect(not apt_ran(h), "APT ran")
    return "APT started but nothing changed (measured on the builder) -> abandoned after pre-state digest proof"


@check
def applied_but_unvalidated_is_validated_without_rerun():             # 2, 18
    h = interrupted("applied", "after-apt")
    expect(apply_journal(h)["phase"] in ("applying", "applied"), "journal phase")
    doc = recover(h)
    expect(doc["classification"] == "applied_but_unvalidated" and doc["result"] == "complete" and
           doc["action"] == "validate_only", f"{doc['classification']} {doc['result']}")
    expect(not apt_ran(h) and doc["recovery_argv"] == [] and not doc["mutation_started"], "complete state was re-run")
    expect(not [c for c in h.commands if c[1:2] in (["--configure"], ["--triggers-only"])], "complete state reconfigured")
    expect({p["package"]: p["automatic"] for p in doc["post_state"]["packages"]} ==
           {"firmware-iwlwifi": False, "intel-microcode": False, "iucode-tool": True}, "post-state marks")
    expect(pa.apply_current_plan(h)["result"] == "nothing_to_apply", "recovered attempt still blocks apply")
    return "complete post-state -> validated (audit, check, files, source, marks) and closed; no APT/dpkg rerun"


@check
def bounded_trigger_interruption_is_completed_narrowly():              # 3, 29, 30, 36
    verified = []
    real_verify = ps.verify_bundle

    def spy(bundle, inspector, expected=None, tx=None):
        verified.append((os.path.basename(bundle), dict(expected or {})))
        return real_verify(bundle, inspector, expected=expected, tx=tx)
    for mode, argv in (# as measured on the builder: configure with triggers suppressed leaves
                       # initramfs-tools trigger-pending (its postinst re-activates update-initramfs),
                       # then its own trigger is processed in the separately authorised phase
                       ("trigger", [[pa.DPKG, "--configure", "--no-triggers", "initramfs-tools"],
                                    [pa.DPKG, "--triggers-only", "initramfs-tools"]]),
                       # killed right after the update-initramfs activation: man-db's file trigger
                       # (activated during the first unpack) is still pending too, and both are attributed
                       ("triggers-pending", [[pa.DPKG, "--triggers-only", "initramfs-tools", "man-db"]])):
        h = interrupted("bounded-" + mode, mode)
        att = apply_journal(h)
        locks = [pathlib.Path(h.root, "var/lib/dpkg", n) for n in ("lock", "lock-frontend")]
        for lk in locks:
            lk.write_text("")
        verified.clear()
        ps.verify_bundle = spy
        try:
            doc = recover(h)
        finally:
            ps.verify_bundle = real_verify
        expect(doc["classification"] == "bounded_incomplete_transaction" and doc["result"] == "complete",
               f"{mode}: {doc['classification']} {doc['result']} {doc['reason']}")
        expect(doc["recovery_argv"] == argv and doc["mutation_started"], f"{mode}: argv {doc['recovery_argv']}")
        ran = [c for c in h.commands if c[1:2] in (["--configure"], ["--triggers-only"])]
        expect(ran == argv, f"{mode}: commands {ran}")
        expect([s["argv"] for s in doc["recovery_steps"]] == argv and
               all(s["delta_sha256"] and not s["pending_after"] for s in doc["recovery_steps"][-1:]),
               f"{mode}: step evidence {doc['recovery_steps']}")
        expect(not apt_ran(h), f"{mode}: APT ran during recovery")
        expect(verified and verified[0][0] == att["digests"]["transaction_digest"][7:] and
               verified[0][1] == {k: att["digests"][k] for k in ("transaction_digest", "source_activation_digest",
                                                                  "repository_metadata_digest")},
               f"{mode}: the original bundle was not re-verified against the journal ({verified})")
        expect(all(e["status"] == INSTALLED for e in h.u_after["installed"]), f"{mode}: state not complete")
        expect(all(lk.exists() for lk in locks), f"{mode}: a native dpkg lock file was removed")
        expect(pa.apply_current_plan(h)["result"] == "nothing_to_apply", f"{mode}: apply still blocked")
    return "trigger (configure --no-triggers initramfs-tools, then --triggers-only initramfs-tools), " \
           "triggers-pending (--triggers-only initramfs-tools man-db) completed with exact-package dpkg only, " \
           "bundle re-verified, no APT"


# ---------------------------------------------------------------------------
# Recovery impact set: transaction-attributed trigger impact (M1-L amendment)

def journal_edit(fn):
    def go(h):
        p = next(pathlib.Path(h.root, pa.JOURNAL_DIR).glob("*/journal.json"))
        d = json.loads(p.read_text())
        fn(d)
        p.write_bytes(resolver.canonical_json(d) + b"\n")
    return go


@check
def trigger_impact_is_attributed_and_closure_stays_bound():           # CTO 1, 11
    h = interrupted("impact", "trigger")
    att = apply_journal(h)
    ev = att["recovery_evidence"]
    expect(att["phases"][-1]["phase"] == "applying" and
           [p["phase"] for p in att["phases"]].index("bundle_verified") <
           [p["phase"] for p in att["phases"]].index("applying"), "evidence not bound before applying")
    expect(ev["dpkg_log"]["size"] == len(LOG_HISTORY) and
           ev["dpkg_log"]["sha256"] == "sha256:" + hashlib.sha256(LOG_HISTORY.encode()).hexdigest(),
           f"pre-apply dpkg log cursor {ev['dpkg_log']}")
    expect([(r["package"], r["status"]) for r in ev["trigger_interest"]] ==
           [("initramfs-tools", INSTALLED), ("man-db", INSTALLED)], f"trigger-interest record {ev['trigger_interest']}")
    doc = recover(h)
    expect(doc["classification"] == "bounded_incomplete_transaction" and doc["result"] == "complete", doc["reason"])
    closure = [{k: c[k] for k in ("package", "architecture", "version")} for c in att["expected"]["closure"]]
    expect(doc["transaction_closure"] == sorted(closure, key=lambda c: (c["package"], c["architecture"])),
           f"closure record {doc['transaction_closure']}")
    imp = {p["package"]: p for p in doc["recovery_impact"]["packages"]}
    expect(sorted(imp) == ["initramfs-tools", "man-db"], f"impact set {sorted(imp)}")
    expect(not set(imp) & {c["package"] for c in closure}, "impact set overlaps the transaction closure")
    i = imp["initramfs-tools"]
    expect(i["pre_status"] == INSTALLED and i["observed_status"] == "install ok half-configured" and
           i["activated_during"] == "configure intel-microcode:amd64" and i["interests"] == ["update-initramfs"] and
           i["events"] == ["status triggers-pending", "trigproc", "status half-configured"],
           f"initramfs-tools attribution {i}")
    expect(imp["man-db"]["observed_status"] == INSTALLED and
           imp["man-db"]["activated_during"] == "install iucode-tool:amd64", f"man-db attribution {imp['man-db']}")
    e = doc["recovery_impact"]["evidence"]
    expect(e["dpkg_log_cursor"] == ev["dpkg_log"] and e["delta_lines"] == 30, f"impact evidence {e}")
    expect(doc["authorized_targets"] == ["initramfs-tools"] and
           doc["recovery_argv"] == [[pa.DPKG, "--configure", "--no-triggers", "initramfs-tools"],
                                    [pa.DPKG, "--triggers-only", "initramfs-tools"]],
           f"targets {doc['authorized_targets']} argv {doc['recovery_argv']}")
    expect({p["package"] for p in doc["post_state"]["packages"]} ==
           {"firmware-iwlwifi", "intel-microcode", "iucode-tool", "initramfs-tools", "man-db"}, "post-state packages")
    # the closure stays bound: a recovery that leaves a closure package changed is never complete
    h = interrupted("impact-closure-bound", "trigger")
    h.fail["after_recover"] = [lambda u: next(x for x in u["installed"] if x["package"] == "intel-microcode")
                               .update(version="3.20251111.1~deb13u9")]
    doc = recover(h)
    expect(doc["result"] == "manual_recovery_required" and doc["mutation_started"] and
           "intel-microcode outside the authorised recovery set changed" in doc["reason"],
           f"changed closure accepted: {doc['result']} {doc['reason']}")
    expect(doc["transaction_closure"] == recovery_journals(h)[-1]["transaction_closure"], "closure record")
    return "initramfs-tools (activated during configure intel-microcode, half-configured) and man-db (completed) " \
           "attributed from the bound dpkg-log cursor; argv configure initramfs-tools only; closure bound and validated"


@check
def unattributable_or_tampered_impact_fails_closed():                  # CTO 2-9, 12
    cases = []

    def case(label, mode, fn, reason, **kw):
        h = interrupted("x-" + label, mode, **kw)
        if fn:
            fn(h)
        manual(h, reason)
        cases.append(label)

    def edit_log(fn):
        def go(h):
            h.dpkg_log.write_bytes(fn(h.dpkg_log.read_bytes()))
        return go
    # 2 unrelated unpacked package (state only, and with its own dpkg log lines as on the builder)
    case("unrelated-unpacked", "unrelated", None, "unrelated package hello")
    case("unrelated-unpacked-logged", "unrelated-log", None, "hello:amd64, which is neither")
    # 3 unrelated half-configured package
    case("unrelated-half-configured", "trigger", lambda h: h.entry("apt").update(status="install ok half-configured"),
         "unrelated package apt")
    # 4 unrelated package that declares trigger interest (bound pre-apply) but was never activated
    def dbus(h):
        h.entry("dbus").update(status="install ok triggers-pending")
    case("unrelated-trigger-interest", "trigger", dbus, "unrelated package dbus",
         installed=[{"package": "dbus", "architecture": "amd64", "version": "1.16.2-2", "status": INSTALLED,
                     "automatic": True}],
         extra_triggers={"dbus": "interest-noawait /usr/share/dbus-1/system.d\n"})
    # 5 trigger-impact package already unhealthy before apply
    case("impact-unhealthy-before", "trigger", None, "was not fully installed before apply",
         pre_status={"initramfs-tools": "install ok triggers-pending"})
    # 6 / 7 recovery-impact version and architecture drift
    case("impact-version-drift", "trigger", lambda h: h.entry("initramfs-tools").update(version="0.148.5"),
         "version deviates from the pre-apply record")
    case("impact-arch-drift", "trigger", lambda h: h.entry("initramfs-tools").update(architecture="amd64"),
         "missing or has a different architecture")
    # 8 evidence tampering
    case("log-prefix-tampered", "trigger", edit_log(lambda b: b.replace(b"base-files", b"base-filez", 1)),
         "prefix digest differs")
    case("log-truncated", "trigger", edit_log(lambda b: b[:40]), "truncated or rotated")
    case("log-rotated", "trigger", edit_log(lambda b: b[len(LOG_HISTORY):]), "does not continue the bound")
    case("cursor-tampered", "trigger", journal_edit(lambda d: d["recovery_evidence"]["dpkg_log"].update(
        sha256="sha256:" + "3" * 64)), "prefix digest differs")
    case("bound-record-tampered", "trigger", journal_edit(lambda d: d["recovery_evidence"]["trigger_interest"][0]
                                                         .update(version="0.148.3")), "not a trigger activation")
    case("triggers-file-changed", "trigger", lambda h: pathlib.Path(h.root, pr.DPKG_INFO, "initramfs-tools.triggers")
         .write_text("interest-noawait update-initramfs\ninterest-noawait /boot\n"), "trigger declarations")
    case("forged-hitchhiker-line", "trigger", lambda h: (h.write_log("status triggers-pending apt:amd64 3.0.3"),
                                                         h.entry("apt").update(status="install ok triggers-pending")),
         "apt:amd64, which is neither")
    case("non-trigger-operation-on-impact", "trigger",
         lambda h: h.write_log("configure initramfs-tools:all 0.148.4 <none>"), "non-trigger operation")
    case("closure-line-wrong-version", "trigger",
         lambda h: h.write_log("status installed iucode-tool:amd64 2.3.1-9"), "unexpected operation on closure")
    case("closure-state-disagrees", "trigger", lambda h: h.entry("iucode-tool").update(status="install ok unpacked"),
         "disagree")
    case("incomplete-line", "trigger", edit_log(lambda b: b + b"2026-10-14 15:13:34 status"), "incomplete line")
    case("unknown-line", "trigger", lambda h: h.write_log("conffile /etc/default/intel-microcode keep"),
         "unrecognised line")
    case("impact-state-not-last-logged", "trigger",
         lambda h: h.entry("initramfs-tools").update(status="install ok triggers-pending"), "last logged state")

    case("impact-before-transaction", "trigger",
         edit_log(lambda b: LOG_HISTORY.encode() + b"2026-10-14 15:13:30 status triggers-pending initramfs-tools:all "
                  b"0.148.4\n" + b[len(LOG_HISTORY):]), "before the transaction's first closure operation")

    def no_activation(h):
        h.write_log("trigproc dbus:amd64 1.16.2-2 <none>", "status half-configured dbus:amd64 1.16.2-2")
        h.entry("dbus").update(status="install ok half-configured")
    case("impact-without-activation", "trigger", no_activation, "is not a trigger activation",
         installed=[{"package": "dbus", "architecture": "amd64", "version": "1.16.2-2", "status": INSTALLED,
                     "automatic": True}],
         extra_triggers={"dbus": "interest-noawait /usr/share/dbus-1/system.d\n"})

    def log_symlink(h):
        real = h.dpkg_log.with_name("dpkg.log.real")
        h.dpkg_log.rename(real)
        h.dpkg_log.symlink_to(real.name)
    case("log-symlink", "trigger", log_symlink, "not a regular file")
    # 9 missing evidence
    case("evidence-missing", "trigger", journal_edit(lambda d: d.pop("recovery_evidence")),
         "lacks the pre-apply recovery-impact evidence")
    case("evidence-null", "trigger", journal_edit(lambda d: d.update(recovery_evidence=None)),
         "lacks the pre-apply recovery-impact evidence")
    case("log-deleted", "trigger", lambda h: h.dpkg_log.unlink(), "dpkg log is missing")
    # 12 a second trigger package cannot hitchhike: man-db is attributed but complete in the log
    case("hitchhike-attributed-package", "trigger",
         lambda h: h.entry("man-db").update(status="install ok half-configured"), "man-db:amd64 state differs")
    case("hitchhike-unbound-trigger-package", "trigger", dbus, "unrelated package dbus",
         installed=[{"package": "dbus", "architecture": "amd64", "version": "1.16.2-2", "status": INSTALLED,
                     "automatic": True}])
    # a recovery whose own dpkg run logged activity and then failed is not retried automatically
    h = interrupted("x-failed-recovery-logged", "trigger")
    h.fail.update(recover_dpkg=True, recover_dpkg_logged=True)
    rejects("recovery_failed", recover, h)
    h.fail.pop("recover_dpkg")
    manual(h, "non-trigger operation")
    cases.append("failed-recovery-left-dpkg-activity")
    return f"{len(cases)} unattributable/tampered/missing-evidence/hitchhiker cases -> manual, zero mutation"


DBUS = {"installed": [{"package": "dbus", "architecture": "amd64", "version": "1.16.2-2", "status": INSTALLED,
                        "automatic": True}],
        "extra_triggers": {"dbus": "interest-noawait /usr/share/dbus-1/system.d\n"}}


def unrelated_state(h, authorized=("initramfs-tools",)):
    return sorted(json.dumps(e, sort_keys=True) for e in h.u_after["installed"] if e["package"] not in authorized)


@check
def trigger_execution_is_contained():                                  # CTO (dpkg boundary) 1, 4-7, 10-12
    # 11/12: the measured real case reaches a healthy final state in two contained phases
    h = interrupted("contain-real", "trigger", **copy.deepcopy(DBUS))
    others = unrelated_state(h)
    doc = recover(h)
    expect(doc["result"] == "complete", f"real case {doc['result']} {doc['reason']}")
    expect(h.handlers == [("configure", "initramfs-tools"), ("trigproc", "initramfs-tools")],
           f"handlers run {h.handlers}")
    st = doc["recovery_steps"]
    expect(st[0]["handlers"] == ["configure initramfs-tools:all"] and
           st[0]["pending_after"] == [{"package": "initramfs-tools", "status": "install ok triggers-pending"}] and
           st[1]["handlers"] == ["trigproc initramfs-tools:all"] and st[1]["pending_after"] == [] and
           st[1]["log_cursor"]["size"] > st[0]["log_cursor"]["size"], f"step evidence {st}")
    expect([p["phase"] for p in doc["phases"]][5:7] == ["recovering", "processing_triggers"], "phases")
    expect(unrelated_state(h) == others, "a package outside the authorised set changed")
    expect(recover(h)["result"] == "nothing_to_recover", "repeated recover")
    cases = ["real-two-phase"]

    def stopped(label, reason, fail, commands, mode="trigger", handlers=None, **kw):
        h = interrupted("contain-" + label, mode, **copy.deepcopy(kw or DBUS))
        h.fail.update(fail)
        doc = recover(h)
        expect(doc["result"] == "manual_recovery_required" and doc["mutation_started"],
               f"{label}: {doc['result']} {doc['reason']}")
        expect(reason in (doc["reason"] or ""), f"{label}: reason {doc['reason']!r} lacks {reason!r}")
        ran = [c for c in h.commands if c[1:2] in (["--configure"], ["--triggers-only"])]
        expect(len(ran) == commands and len(doc["recovery_steps"]) == commands,
               f"{label}: {len(ran)} commands, steps {doc['recovery_steps']}")
        expect(not [c for c in ran if any(n in ("dbus", "apt") for n in c)], f"{label}: unauthorised target {ran}")
        if handlers is not None:
            expect(h.handlers == handlers, f"{label}: handlers {h.handlers}")
        expect(doc["authorized_targets"] == sorted({n for c in ran for n in c[2:] if not n.startswith("-")} |
                                                   set(doc["authorized_targets"])), "authorisation expanded")
        rejects("recovery_required", pa.apply_current_plan, h)
        cases.append(label)
        return h, doc
    # 4/5/6/7: configuring the authorised package activates an unrelated trigger -> its handler never
    # runs, no second command, authorisation not expanded, manual recovery
    h, doc = stopped("configure-activates-unrelated", "recovery activated a trigger of dbus:amd64",
                     {"activates": {"initramfs-tools": ["dbus"]}}, 1,
                     handlers=[("configure", "initramfs-tools")])
    expect(h.entry("dbus")["status"] == "install ok triggers-pending" and doc["authorized_targets"] ==
           ["initramfs-tools"], "dbus processed or authorisation expanded")
    # the same during the separately authorised trigger phase
    h, doc = stopped("trigproc-activates-unrelated", "recovery activated a trigger of dbus:amd64",
                     {"trigproc_activates": {"initramfs-tools": ["dbus"]}}, 2,
                     handlers=[("configure", "initramfs-tools"), ("trigproc", "initramfs-tools")])
    # recovery-induced impact on a package the ORIGINAL transaction never touched: killed before any
    # configure, configuring intel-microcode activates update-initramfs -> initramfs-tools is NOT authorised
    h, doc = stopped("closure-configure-activates-initramfs", "recovery activated a trigger of initramfs-tools:all",
                     {}, 1, mode="closure-unpacked",
                     handlers=[("configure", "firmware-iwlwifi"), ("configure", "intel-microcode"),
                               ("configure", "iucode-tool")],
                     installed=[])
    expect(h.entry("initramfs-tools")["status"] == "install ok triggers-pending" and
           h.entry("man-db")["status"] == "install ok triggers-pending", "pending triggers were processed")
    # 10: the command's own log delta shows unauthorised execution -> fail closed
    def log_line(text):
        return {"during_dpkg": [lambda host, argv: host.write_log(text)]}
    stopped("delta-unauthorised-trigproc", "logged trigproc for dbus:amd64",
            log_line("trigproc dbus:amd64 1.16.2-2 <none>"), 1)
    stopped("delta-unauthorised-configure", "logged configure for apt:amd64",
            log_line("configure apt:amd64 3.0.3 <none>"), 1)
    stopped("delta-trigproc-in-configure-phase", "not authorised to execute",
            log_line("trigproc initramfs-tools:all 0.148.4 <none>"), 1)
    # an authorised package re-activated by its own trigger processing is not looped on
    stopped("still-pending-after-trigger-phase", "still not final after bounded recovery",
            {"trigproc_activates": {"initramfs-tools": ["initramfs-tools"]}}, 2)
    # an authorised package must keep its identity and mark through each command
    stopped("authorised-mark-changed", "initramfs-tools changed identity or mark",
            {"during_dpkg": [lambda host, argv: host.entry("initramfs-tools").update(automatic=False)]}, 1)
    # 12: a package outside the authorised set changed during a recovery command
    stopped("unrelated-changed", "outside the authorised recovery set changed",
            {"during_dpkg": [lambda host, argv: host.entry("apt").update(automatic=True)]}, 1)
    return f"{len(cases)} cases: two contained phases with exact handlers; unrelated activation never executed, " \
           "authorisation never expanded, unauthorised log/state changes -> manual"


@check
def recovery_argv_is_bounded_by_the_authorised_set():                  # CTO 10
    h = interrupted("argv-bound", "trigger")
    real = pr.classify

    def widened(host, attempt):
        classification, details = real(host, attempt)
        details["configure"] = sorted(details["configure"] + ["apt"])
        return classification, details
    pr.classify = widened
    try:
        rejects("contract_failure", recover, h)
    finally:
        pr.classify = real
    doc = recovery_journals(h)[-1]
    expect(doc["phase"] == "failed" and not doc["mutation_started"] and doc["recovery_argv"] == [],
           f"widened argv reached phase {doc['phase']}")
    expect(not [c for c in h.commands if c[1:2] in (["--configure"], ["--triggers-only"])], "widened argv ran")
    details = {"authorized": ["initramfs-tools"], "closure": {("intel-microcode", "amd64"): {}},
               "impact": {"packages": {("initramfs-tools", "all"): {}}}}
    pr.authorize_argv([[pa.DPKG, "--configure", "--no-triggers", "initramfs-tools"],
                       [pa.DPKG, "--triggers-only", "initramfs-tools"]], details)
    for argv in ([[pa.DPKG, "--configure", "--no-triggers", "initramfs-tools", "hello"]],
                 [[pa.DPKG, "--configure", "--no-triggers", "-a"]], [[pa.DPKG, "--configure", "--no-triggers",
                                                                       "--pending"]],
                 [[pa.DPKG, "--configure", "--no-triggers"]], [[pa.DPKG, "--configure", "initramfs-tools"]],
                 [[pa.DPKG, "--no-triggers", "--configure", "initramfs-tools"]],
                 [[pa.DPKG, "--triggers-only", "man-db"]], [[pa.DPKG, "--triggers-only", "-a"]],
                 [[pa.DPKG, "--triggers-only", "--pending"]], [[pa.DPKG, "--triggers-only"]],
                 [[pa.APT_GET, "-f", "install"]],
                 [[pa.DPKG, "--configure", "--no-triggers", "intel-microcode"]],   # closure, but not a target
                 [[pa.DPKG, "--remove", "initramfs-tools"]]):
        rejects("contract_failure", pr.authorize_argv, argv, details)
    rejects("recovery_required", pa.apply_current_plan, h)
    return "argv outside the authorised recovery set (unrelated, -a, --pending, complete closure package, other " \
           "operation) -> contract_failure before any mutation"


# ---------------------------------------------------------------------------
# Fail-closed classification (4-17)

@check
def unrelated_or_unprovable_state_requires_manual_recovery():
    cases = 0
    h = interrupted("unrelated", "unrelated")
    manual(h, "unrelated package hello")
    cases += 1
    h = interrupted("half-installed", "half-installed")
    manual(h, "needs re-unpacking")
    cases += 1
    h = interrupted("partial-absent", "partial-absent")
    manual(h, "partially absent")
    cases += 1

    def mutate_after(label, mode, fn, reason):
        nonlocal cases
        h = interrupted("m-" + label, mode)
        fn(h)
        manual(h, reason)
        cases += 1
    mutate_after("outside-closure", "trigger", lambda h: h.entry("libc6").update(version="2.41-99"),
                 "changes outside the transaction closure")
    mutate_after("hold", "trigger", lambda h: h.entry("dpkg").update(status="hold ok installed"),
                 "unrelated package dpkg")
    mutate_after("hold-installed", "after-apt", lambda h: h.entry("apt").update(automatic=True),
                 "changes outside the transaction closure")
    mutate_after("closure-version", "trigger", lambda h: h.entry("iucode-tool").update(version="2.3.1-9"),
                 "unexpected version")
    mutate_after("closure-arch", "trigger", lambda h: h.entry("iucode-tool").update(architecture="i386"),
                 "unexpected architecture")
    mutate_after("mark", "trigger", lambda h: h.entry("iucode-tool").update(automatic=False), "mark deviates")
    mutate_after("non-trigger-package", "trigger",
                 lambda h: h.entry("apt").update(status="install ok half-configured"), "unrelated package apt")

    def corrupt(h):
        b = next(pathlib.Path(h.root, pa.STAGING_DIR).glob("*/archives/intel-microcode_*.deb"))
        data = bytearray(b.read_bytes())
        data[-4] ^= 0xFF
        b.write_bytes(bytes(data))
    mutate_after("corrupt-payload", "trigger", corrupt, "staged bundle cannot be verified")
    mutate_after("missing-payload", "trigger",
                 lambda h: next(pathlib.Path(h.root, pa.STAGING_DIR).glob("*/archives/iucode-tool_*.deb")).unlink(),
                 "staged bundle cannot be verified")

    def journal_edit(fn):
        def go(h):
            p = next(pathlib.Path(h.root, pa.JOURNAL_DIR).glob("*/journal.json"))
            d = json.loads(p.read_text())
            fn(d)
            p.write_bytes(resolver.canonical_json(d) + b"\n")
        return go
    mutate_after("tx-digest", "trigger",
                 journal_edit(lambda d: d["digests"].update(transaction_digest="sha256:" + "1" * 64)),
                 "staged bundle cannot be verified")
    mutate_after("bundle-digest", "trigger",
                 journal_edit(lambda d: d["digests"].update(bundle_digest="sha256:" + "2" * 64)),
                 "bundle digest differs")
    mutate_after("closure-journal", "trigger",
                 journal_edit(lambda d: d["expected"]["closure"].pop()), "expected closure differs")
    mutate_after("owned-source", "trigger",
                 lambda h: gate11.owned(h).write_bytes(gate11.owned(h).read_bytes() + b"# drift\n"),
                 "source state differs")
    mutate_after("foreign-source", "trigger",
                 lambda h: pathlib.Path(h.root, "etc/apt/sources.list").write_text(
                     gate11.BASELINE.replace("trixie main", "trixie main contrib", 1)),
                 "")
    return f"{cases} unrelated/unprovable states -> manual_recovery_required, no command, apply stays blocked"


@check
def untrusted_or_ambiguous_journals_fail_closed():                    # 5, 22
    h = interrupted("garbled", "trigger")
    p = next(pathlib.Path(h.root, pa.JOURNAL_DIR).glob("*/journal.json"))
    p.write_text("{not json")
    rejects("recovery_required", recover, h)
    expect(not any(c[1:2] in (["--configure"], ["--triggers-only"]) for c in h.commands), "garbled journal mutated")
    # two interrupted attempts at once -> ambiguous -> manual
    h = interrupted("ambiguous", "trigger")
    att = apply_journal(h)
    clone = pathlib.Path(h.root, pa.JOURNAL_DIR, "f" * 16)
    clone.mkdir()
    d = dict(att, attempt_id="f" * 16)
    (clone / "journal.json").write_bytes(resolver.canonical_json(d) + b"\n")
    doc = recover(h)
    expect(doc["result"] == "manual_recovery_required" and "more than one" in doc["reason"], doc["reason"])
    # an interrupted recovery is closed as failed and the real state is re-inspected
    h = interrupted("interrupted-recovery", "trigger")
    real_run = h.run

    def die_in_dpkg(argv):
        if argv[1:2] == ["--configure"]:
            raise Killed()
        return real_run(argv)
    h.run = die_in_dpkg
    try:
        recover(h)
        raise Failure("recovery not interrupted")
    except Killed:
        pass
    h.run = real_run
    stuck = recovery_journals(h)[-1]
    expect(stuck["phase"] == "recovering" and stuck["mutation_started"], "recovery journal not left at recovering")
    rejects("recovery_required", pa.apply_current_plan, h)
    doc = recover(h)
    expect(doc["result"] == "complete" and recovery_journals(h)[0]["phase"] == "failed" and
           recovery_journals(h)[0]["failure"]["code"] == "interrupted", "interrupted recovery not handled")
    expect(recover(h)["result"] == "nothing_to_recover", "nothing left to recover")
    # a recovery journal stuck before binding any attempt still blocks apply until closed
    pa._ensure_dir(h, pa.LUBARCHY_STATE, 0o755)
    stuck2 = pr.RecoveryJournal(h)
    stuck2.transition("locked")
    rejects("recovery_required", pa.apply_current_plan, h)
    expect(recover(h)["result"] == "nothing_to_recover", "stuck unbound recovery not closed")
    expect(pa.apply_current_plan(h)["result"] == "nothing_to_apply", "closed unbound recovery still blocks apply")
    return "garbled journal rejected; two attempts -> manual; interrupted recovery closed as failed and re-inspected"


@check
def failed_recovery_is_durable_and_validated():                         # 37, 38
    h = interrupted("recover-fails", "trigger")
    h.fail["recover_dpkg"] = True
    rejects("recovery_failed", recover, h)
    doc = recovery_journals(h)[-1]
    expect(doc["phase"] == "failed" and doc["mutation_started"] and doc["failure"]["code"] == "recovery_failed",
           f"failed recovery journal {doc['phase']}")
    expect(not apt_ran(h) and len([c for c in h.commands if c[1:2] == ["--configure"]]) == 1, "retried or repaired")
    rejects("recovery_required", pa.apply_current_plan, h)
    h.fail.pop("recover_dpkg")
    expect(recover(h)["result"] == "complete", "re-inspection after a failed recovery")
    # a recovery that leaves any deviation is not complete
    h = interrupted("recover-deviates", "trigger")
    h.fail["after_recover"] = [lambda u: u["installed"].append({"package": "evil", "architecture": "amd64",
                                                                "version": "1", "status": INSTALLED})]
    doc = recover(h)
    expect(doc["result"] == "manual_recovery_required" and "package evil outside the authorised recovery set "
           "changed" in doc["reason"], f"deviating recovery accepted: {doc['result']} {doc['reason']}")
    # final validation still guards what the per-command containment does not see (package files)
    h = interrupted("recover-files-missing", "trigger")
    h.fail["missing_file"] = "intel-microcode"
    rejects("validation_failed", recover, h)
    expect(recovery_journals(h)[-1]["phase"] == "failed", "missing package files accepted")
    return "dpkg failure -> durable failed recovery, apply blocked, no retry/repair; state deviation during " \
           "recovery -> manual; missing package files after recovery -> failed validation"


# ---------------------------------------------------------------------------
# Boundaries (19-28, 31-35, 40)

@check
def lock_root_and_caller_boundaries():
    h = interrupted("lock", "trigger")
    lockfile = pathlib.Path(h.root, pa.LOCK_DIR, pa.LOCK_NAME)
    fd = os.open(lockfile, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        rejects("busy", recover, h)                                   # concurrent recovery blocked
        rejects("busy", pa.apply_current_plan, h)                     # concurrent apply blocked
        expect(not recovery_journals(h), "busy recovery wrote a journal")
    finally:
        os.close(fd)
    h.uid = 1000
    rejects("not_root", recover, h)
    h.uid = 0
    expect(list(__import__("inspect").signature(pr.recover_interrupted_transaction).parameters) == ["host"],
           "recover signature")
    cli = gate11._load_cli(HARDWARE / "apply-packages.py")
    saved = cli.pa.RealHost

    class Sentinel:
        def __init__(self):
            raise Failure("CLI constructed a real host")
    cli.pa.RealHost = Sentinel
    try:
        for argv in (["recover", "intel-microcode"], ["recover", "--configure"], ["recover", "-a"],
                     ["recover", "--classification", "applied_but_unvalidated"], ["recover", "--attempt", "x"],
                     ["recover", "-o", "Dir::Cache=/tmp"], ["repair"], ["recover", ""]):
            with open(os.devnull, "w") as null, contextlib.redirect_stderr(null):
                rc = cli.main(argv)
            expect(rc == 64, f"CLI {argv} -> {rc}")
    finally:
        cli.pa.RealHost = saved
    for argv in ([pa.DPKG, "--configure", "-a"], [pa.DPKG, "--configure", "--pending"], [pa.DPKG, "--configure"],
                 [pa.DPKG, "--configure", "initramfs-tools"],          # would also run pending triggers
                 [pa.DPKG, "--no-triggers", "--configure", "initramfs-tools"], [pa.DPKG, "--configure", "--no-triggers"],
                 [pa.DPKG, "--configure", "--no-triggers", "-a"], [pa.DPKG, "--configure", "--no-triggers", "--pending"],
                 [pa.DPKG, "--configure", "--no-triggers", "--no-triggers", "x"], [pa.DPKG, "--triggers-only"],
                 [pa.DPKG, "--triggers-only", "--pending"], [pa.DPKG, "--triggers-only", "--no-triggers", "x"],
                 [pa.DPKG, "--configure", "--force-all", "x"], [pa.DPKG, "--triggers-only", "-a"],
                 [pa.DPKG, "-i", "hello.deb"], [pa.DPKG, "--remove", "hello"], [pa.DPKG, "--purge", "hello"],
                 [pa.DPKG, "--unpack", "hello"], [pa.DPKG, "--set-selections", "hello"],
                 [pa.APT_GET, "-f", "install"], [pa.APT_GET, "install", "-f"], [pa.DPKG, "--configure", "x", "x"],
                 ["/usr/bin/apt-mark", "auto", "x"], [pa.DPKG, "--configure", "a b"]):
        expect(not pa.allowed_command(argv), f"{argv} allowlisted")
    expect(pa.allowed_command([pa.DPKG, "--configure", "--no-triggers", "initramfs-tools"]) and
           pa.allowed_command([pa.DPKG, "--triggers-only", "initramfs-tools"]), "exact recovery shapes not allowlisted")
    return "shared lock (recovery/apply mutually exclusive), root only, CLI takes no input, allowlist exact-package only"


@check
def recovery_code_has_no_repair_upgrade_or_shell():                     # 27, 28, 31-35
    src = (HARDWARE / "package_recovery.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    # The dpkg.log grammar names dpkg's own action words ("install", "remove", ...). Those two
    # read-only parsers are exempt from the word scan, but they may never build or run a command.
    lines = src.splitlines(keepends=True)
    grammar = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in ("parse_log_line",
                                                                                     "attribute_impact")]
    expect(len(grammar) == 2, "log grammar functions not found")
    for fn in grammar:
        for node in ast.walk(fn):
            expect(not (isinstance(node, ast.Attribute) and node.attr in ("run", "DPKG", "APT_GET", "transition")),
                   f"{fn.name} builds or runs a command or writes a journal")
    for fn in sorted(grammar, key=lambda n: -n.lineno):
        del lines[fn.lineno - 1:fn.end_lineno]
    scanned = "".join(lines)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            expect(name not in ("system", "popen", "Popen", "check_output", "call", "check_call", "execv",
                                "rmtree", "setuid"), f"package_recovery calls {name}()")
            if name == "run":
                expect(isinstance(node.func, ast.Attribute) and getattr(node.func.value, "id", "") == "host",
                       "package_recovery runs commands other than through the host's allowlisted runner")
    expect("import subprocess" not in src, "recovery spawns processes itself (must use the host runner)")
    for bad in (r'"install"', r'"remove"', r'"purge"', r'"--pending"', r'"-a"', r'"-f"', r'"--fix-broken"',
                r'"update"', r'"download"', r'"upgrade"', r'"--force', r"apt-mark", r'"-i"', "nvidia", "contrib",
                "backports"):
        expect(not re.search(bad, scanned, re.I), f"package_recovery contains {bad}")
    expect(set(re.findall(r'[\[(]pa\.DPKG, "(--[a-z-]+)"', src)) == {"--audit", "--configure", "--triggers-only"},
           "recovery dpkg operations")
    expect(pr.CONFIGURE_ARGV == (pa.DPKG, "--configure", "--no-triggers") and
           pr.TRIGGERS_ARGV == (pa.DPKG, "--triggers-only"), "recovery command shapes")
    expect(src.count("pa.DPKG") == 3, "recovery builds dpkg commands outside the two fixed shapes and --audit")
    calls = []

    class R:
        returncode, stdout, stderr = 0, b"", b""
    real = pa.subprocess.run
    pa.subprocess.run = lambda argv, **kw: (calls.append(kw), R())[1]
    os.environ.update(APT_CONFIG="/tmp/x", http_proxy="http://x:1", DPKG_ROOT="/tmp/evil")
    try:
        pa.RealHost().run([pa.DPKG, "--configure", "--no-triggers", "initramfs-tools"])
    finally:
        pa.subprocess.run = real
        for k in ("APT_CONFIG", "http_proxy", "DPKG_ROOT"):
            os.environ.pop(k, None)
    expect(calls and calls[0]["shell"] is False and calls[0]["env"] == pa.APPLY_ENV, "recovery command env/shell")
    return "no shell/subprocess/rmtree; no install/remove/purge/-a/--pending/-f/apt-mark/NVIDIA; literal environment"


@check
def recovery_launch_uses_trusted_deployment():                            # 40
    td = gate11._load_tool(REPO / "tools" / "hwe" / "trusted-deployment.py")
    root = "/root/lubarchy-m1k/" + "a" * 40
    expect(td.privileged_argv(root, "recover") ==
           ["/usr/bin/env", "-i", "/usr/bin/python3", "-I", "-B", root + "/tree/hardware/apply-packages.py", "recover"],
           "run-recover argv")
    try:
        td.privileged_argv(root, "repair")
        raise Failure("unknown privileged operation accepted")
    except td.DeploymentError:
        pass
    launched = []
    saved = (td.os.execv, td.os.geteuid, td.verify)
    td.os.execv = lambda path, args: launched.append(list(args))
    td.os.geteuid = lambda: 0
    td.verify = lambda r, c, t: {"entrypoint": os.path.join(r, "tree", td.ENTRYPOINT)}
    cwd = os.getcwd()
    try:
        td.main(["run-recover", "--root", root, "--commit", "c" * 40, "--tree", "d" * 40])
    finally:
        td.os.execv, td.os.geteuid, td.verify = saved
        os.chdir(cwd)
    expect(launched == [td.privileged_argv(root, "recover")], f"launcher {launched}")
    return "recover is launched only as env -i python3 -I -B <verified deployment>/tree/hardware/apply-packages.py recover"


def main():
    sys.stdout.reconfigure(line_buffering=True)
    from importlib.metadata import version
    print(f"python {sys.version.split()[0]}, jsonschema {version('jsonschema')}, dpkg-deb {shutil.which('dpkg-deb')}")
    if shutil.which("dpkg-deb") != ps.DPKG_DEB:
        print(f"FAIL: hardware package recovery needs {ps.DPKG_DEB}", file=sys.stderr)
        return 1
    failures = 0
    old = os.umask(0o022)
    try:
        with tempfile.TemporaryDirectory(prefix="lubarchy-gate12-") as tmp:
            gate11.TMP = gate10.TMP = os.path.realpath(tmp)
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
        print(f"FAIL: hardware package recovery ({failures} of {len(CHECKS)} checks failed)", file=sys.stderr)
        return 1
    print(f"PASS: hardware package recovery ({len(CHECKS)} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
