"""LUBARCHY hardware-enablement orchestration (M1-O).

The production coordinator composes the already qualified primitives into one
resumable, durable, idempotent operation, "reconcile the current machine with
its current source-controlled hardware plan":

  orchestration lock -> validate persistent orchestration state
  -> package preflight (apply/recovery journals, authoritative)
  -> bounded recovery first when legal (M1-L), else stop
  -> fresh probe -> production resolver -> read-only transaction precheck
  -> existing apply executor only if a package change is required (M1-K)
  -> fresh validation (M1-M), continuity against a pending pre-reboot result
  -> reboot_required (never a reboot) or a terminal validation outcome
  -> conservative GC through the existing collector (M1-N)
  -> one durable run journal

It is not a second resolver, transaction engine, recovery engine, validator
or collector: it only decides which qualified primitive runs next, whether
execution may continue, and which orchestration phase and outcome to record.
It never builds an APT or dpkg argv, never names a package, never deletes,
never reboots and never trusts its own earlier phase as proof that a package
operation happened: package state is re-read from the package subsystem's own
journals every time, and every primitive's result is re-read from disk.

Persistent state lives only under /var/lib/lubarchy/hardware-orchestration/
(root-owned, not group/other writable, no symlink, no mount crossing). Every
run owns one directory <run-id>/ with journal.json and the canonical
inventory, plan and validation result it produced. Runs form one linear,
digest-bound chain; nothing is ever deleted or rewritten after a later run
starts. Malformed, ambiguous or unsafe state fails closed before any mutation.

Lock order: /run/lubarchy/hardware-orchestration.lock, then (inside the
primitives) /run/lubarchy/package-transaction.lock. Never the reverse.
"""

import copy
import fcntl
import os
import re
import stat

import hardware_validation as hv
import package_apply as pa
import package_gc as gc
import package_recovery as pr
import package_transaction as pt
import resolver
import source_activation as sa

JOURNAL_SCHEMA_ID = "lubarchy.hardware.orchestration-journal/v1"
JOURNAL_SCHEMA_FILE = "orchestration-journal-v1.schema.json"

# Fixed system locations (relative to the host root; "/" in production).
STATE_DIR = pa.LUBARCHY_STATE + "/hardware-orchestration"
LOCK_NAME = "hardware-orchestration.lock"
JOURNAL_FILE = "journal.json"
ARTIFACT_FILES = {"inventory": "inventory.json", "plan": "plan.json", "validation": "validation.json"}
RUN_NAME = re.compile(r"^[0-9a-f]{16}$")
# durable_replace leaves `.<name>.<id>.tmp` only if the process died before its rename: crash
# debris that never looks complete. It is preserved, never parsed and never trusted.
DEBRIS_NAME = re.compile(r"^\.(journal|inventory|plan|validation)\.json\.[0-9a-f]{16}\.tmp$")
MAX_FILE = 1 << 24

PHASES = ("started", "preflight", "recovering", "recovered", "planned", "applying", "applied", "validated",
          "collecting", "finished")
OUTCOMES = ("complete", "reboot_required", "validation_limited", "validation_failed", "manual_recovery_required",
            "policy_blocked", "stale", "failed")
GC_ELIGIBLE_OUTCOMES = ("complete", "validation_limited")
MAINTENANCE_FAILED = ("partial", "failed")

EXIT_OK = 0
EXIT_VALIDATION_FAILED = 1
EXIT_VALIDATION_LIMITED = 2
EXIT_STALE = 3
EXIT_POLICY = 4
EXIT_REBOOT_REQUIRED = 5
EXIT_MAINTENANCE_FAILED = 6
EXIT_FAILED = 7
EXIT_BUSY = 8
EXIT_MANUAL_RECOVERY = 9
EXIT_STATE_INVALID = 10
EXIT_USAGE = 64
EXIT_NOT_ROOT = 77
OUTCOME_EXIT = {"complete": EXIT_OK, "validation_failed": EXIT_VALIDATION_FAILED,
                "validation_limited": EXIT_VALIDATION_LIMITED, "stale": EXIT_STALE, "policy_blocked": EXIT_POLICY,
                "reboot_required": EXIT_REBOOT_REQUIRED, "failed": EXIT_FAILED,
                "manual_recovery_required": EXIT_MANUAL_RECOVERY}


class OrchestrationError(Exception):
    """The run cannot start (busy, not root, unsafe or corrupt state). Messages
    carry reason codes and file names only."""

    EXIT = {"busy": EXIT_BUSY, "not_root": EXIT_NOT_ROOT, "state_invalid": EXIT_STATE_INVALID}

    def __init__(self, code, reason, message):
        super().__init__(f"{code}: {reason}: {message}")
        self.code = code
        self.reason = reason
        self.message = message

    @property
    def exit_status(self):
        return self.EXIT.get(self.code, EXIT_FAILED)


def _invalid(reason, message):
    return OrchestrationError("state_invalid", reason, message)


# ---------------------------------------------------------------------------
# Host

class RealHost(pa.RealHost):
    """The production host of the package primitives plus the read-only
    validation collector. Has no parameters."""

    def runtime(self):
        return hv.RealRuntime()


# ---------------------------------------------------------------------------
# Lock (the orchestration lock; the package lock stays the primitives' own)

class OrchestrationLock:
    """/run/lubarchy/hardware-orchestration.lock: root-owned parent, regular
    file, O_NOFOLLOW, exclusive non-blocking flock, never deleted. It
    serializes reconcile runs; it is not a security boundary against root."""

    def __init__(self, host):
        self.host = host
        self.fd = None

    def acquire(self):
        try:
            d = pa._ensure_dir(self.host, pa.LOCK_DIR, 0o755)
        except pa.ApplyError:
            raise OrchestrationError("failed", "lock_unsafe", "lock directory is unsafe") from None
        try:
            fd = os.open(os.path.join(d, LOCK_NAME), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        except OSError:
            raise OrchestrationError("failed", "lock_unsafe", "orchestration lock is not a regular file") from None
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != self.host.owner_uid or st.st_mode & 0o077:
            os.close(fd)
            raise OrchestrationError("failed", "lock_unsafe", "orchestration lock has an unexpected type, owner or mode")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise OrchestrationError("busy", "orchestration_busy", "another reconcile holds the lock") from None
        self.fd = fd
        return self

    def release(self):
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


# ---------------------------------------------------------------------------
# Journal contract

def check_journal(doc):
    """Schema + semantic self-check of one orchestration journal."""
    import json
    import jsonschema
    with open(os.path.join(resolver.SCHEMA_DIR, JOURNAL_SCHEMA_FILE), encoding="utf-8") as f:
        schema = json.load(f)
    if any(True for _ in jsonschema.Draft202012Validator(schema).iter_errors(doc)):
        raise _invalid("journal_invalid", "journal does not satisfy orchestration-journal v1")
    phases = doc["phases"]
    if phases[-1] != doc["phase"] or phases[0] != "started" or \
            [PHASES.index(p) for p in phases] != sorted({PHASES.index(p) for p in phases}):
        raise _invalid("journal_invalid", "phase history is not a monotonic sequence ending in the phase")
    if doc["terminal"] != (doc["phase"] == "finished") or (doc["outcome"] is None) == doc["terminal"] or \
            (doc["reason"] is None) == doc["terminal"]:
        raise _invalid("journal_invalid", "terminal, outcome and reason are inconsistent")
    if (doc["sequence"] == 1) != (doc["previous"] is None) or \
            (doc["previous"] is not None and doc["previous"]["sequence"] != doc["sequence"] - 1):
        raise _invalid("journal_invalid", "previous run link is inconsistent with the sequence")
    v = doc["validation"]
    if (v is None) != (doc["artifacts"]["validation"] is None) or \
            (v is not None and v["result_digest"] != doc["artifacts"]["validation"]):
        raise _invalid("journal_invalid", "validation result is not bound to its artifact")
    if doc["outcome"] == "reboot_required" and not (doc["reboot_required"] and v is not None and v["reboot_required"]):
        raise _invalid("journal_invalid", "reboot_required outcome without a same-boot validation")
    if doc["outcome"] in ("complete", "validation_limited", "validation_failed") and v is None:
        raise _invalid("journal_invalid", "validation outcome without a validation result")
    if doc["outcome"] == "reboot_required" and v["status"] == "FAIL":
        raise _invalid("journal_invalid", "reboot_required outcome hides a critical validation failure")
    if doc["outcome"] == "validation_failed" and v["status"] != "FAIL":
        raise _invalid("journal_invalid", "validation_failed outcome without an authoritative FAIL")
    if doc["outcome"] == "complete" and (v["status"] != "PASS" or v["reboot_required"]):
        raise _invalid("journal_invalid", "complete outcome without a post-reboot PASS validation")
    if doc["outcome"] == "validation_limited" and (v["status"] not in ("LIMITED", "NOT_TESTED") or v["reboot_required"]):
        raise _invalid("journal_invalid", "validation_limited outcome inconsistent with its validation")
    if doc["terminal"] and "validated" in phases and doc["outcome"] != validation_outcome(v)[0]:
        raise _invalid("journal_invalid", "outcome differs from the validation result it is bound to")
    gc_ran = doc["maintenance"]["gc"] != "not_run"
    if (gc_ran and not doc["terminal"]) or (doc["terminal"] and gc_ran != ("collecting" in phases)) or \
            ("collecting" in phases and doc["outcome"] not in (None,) + GC_ELIGIBLE_OUTCOMES):
        raise _invalid("journal_invalid", "garbage collection outside an eligible terminal state")
    aw = doc["awaiting_reboot"]
    if aw is not None and doc["boot"] is not None and doc["boot"]["previous_boot_id_digest"] != aw["boot_id_digest"]:
        raise _invalid("journal_invalid", "boot binding differs from the awaited reboot")
    if aw is not None and v is not None and v["continuity"] != "profile_changed" and \
            (v["previous_result_digest"] != aw["validation_digest"] or v["mode"] != "revalidation"):
        raise _invalid("journal_invalid", "resumed validation is not bound to the pre-reboot result")
    # The top-level flag is the ACTIONABLE reboot state: true only when a reboot is what the
    # validation outcome calls for (a critical FAIL outranks it); the validation record keeps the
    # validator's evidence that post-reboot observation is pending.
    if v is not None and doc["reboot_required"] != (validation_outcome(v)[0] == "reboot_required"):
        raise _invalid("journal_invalid", "actionable reboot flag differs from the validation outcome")
    if v is None and doc["reboot_required"]:
        raise _invalid("journal_invalid", "actionable reboot flag without a validation")
    if doc["terminal"] and doc["reboot_required"] != (doc["outcome"] == "reboot_required"):
        raise _invalid("journal_invalid", "actionable reboot flag inconsistent with the outcome")
    ap = doc["package"]["apply"]
    if "applied" in phases and (doc["package"]["decision"] != "apply" or ap is None or ap["attempt_id"] is None
                                or ap["result"] not in ("complete", "nothing_to_apply")):
        raise _invalid("journal_invalid", "applied phase without a verified apply attempt")
    resolver.canonical_json(doc)
    return doc


def _read_doc(path, label):
    try:
        data = sa.read_regular(path, limit=MAX_FILE)
    except sa.SourceError:
        raise _invalid("artifact_unsafe", f"{label} cannot be read as a regular file") from None
    try:
        doc = resolver.parse_strict(data, label=label)
    except resolver.ResolverError:
        raise _invalid("artifact_malformed", f"{label} is not strict JSON") from None
    try:
        canonical = resolver.canonical_json(doc) + b"\n"
    except resolver.ResolverError:
        raise _invalid("artifact_malformed", f"{label} is not canonical JSON") from None
    if data != canonical:
        raise _invalid("artifact_malformed", f"{label} is not canonically serialized")
    return doc


def _check_artifact(kind, doc):
    try:
        if kind == "inventory":
            if resolver.inventory_errors(doc):
                raise _invalid("artifact_malformed", "inventory artifact is not inventory v1")
        elif kind == "plan":
            resolver.check_plan(doc)
        else:
            hv.check_result(doc)
    except (resolver.ResolverError, hv.ValidationError):
        raise _invalid("artifact_malformed", f"{kind} artifact does not satisfy its contract") from None
    return doc


# ---------------------------------------------------------------------------
# Persistent state (fixed root; read-only inspection; fails closed)

def _check_object(st, host, dev, kind, label):
    want = stat.S_ISDIR if kind == "dir" else stat.S_ISREG
    if stat.S_ISLNK(st.st_mode) or not want(st.st_mode):
        raise _invalid("state_unsafe", f"{label} is not a real {'directory' if kind == 'dir' else 'regular file'}")
    if st.st_uid != host.owner_uid or st.st_mode & 0o022:
        raise _invalid("state_unsafe", f"{label} has a wrong owner or is group/other writable")
    if dev is not None and st.st_dev != dev:
        raise _invalid("state_unsafe", f"{label} is on another filesystem")


def state_root(host, create):
    """Path of the checked state root, or None if it does not exist (and create is False)."""
    parent = os.path.join(host.root, pa.LUBARCHY_STATE)
    root = os.path.join(host.root, STATE_DIR)
    if create:
        try:
            pa._ensure_dir(host, pa.LUBARCHY_STATE, 0o755)
        except pa.ApplyError:
            raise _invalid("state_unsafe", "/" + pa.LUBARCHY_STATE + " is unsafe") from None
        if not os.path.lexists(root):
            os.mkdir(root, 0o700)
            os.chmod(root, 0o700)
            dfd = pa._open_dir(parent)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
    try:
        pst = os.lstat(parent)
    except FileNotFoundError:
        return None
    _check_object(pst, host, None, "dir", "/" + pa.LUBARCHY_STATE)
    try:
        st = os.lstat(root)
    except FileNotFoundError:
        return None
    _check_object(st, host, pst.st_dev, "dir", "/" + STATE_DIR)
    return root


def load_history(host, root):
    """Every run, verified, ordered by sequence: [{"id", "doc", "digest", "dir"}].
    Anything unexpected raises state_invalid; nothing is repaired or removed."""
    if root is None:
        return []
    dev = os.lstat(root).st_dev
    runs = []
    for name in sorted(os.listdir(root)):
        rdir = os.path.join(root, name)
        if not RUN_NAME.match(name):
            raise _invalid("state_entry_unexpected", f"unexpected entry {name!r} in the state root")
        _check_object(os.lstat(rdir), host, dev, "dir", f"run {name}")
        files = {}
        for entry in sorted(os.listdir(rdir)):
            est = os.lstat(os.path.join(rdir, entry))
            if DEBRIS_NAME.match(entry):
                _check_object(est, host, dev, "file", f"run {name} debris")
                continue
            if entry != JOURNAL_FILE and entry not in ARTIFACT_FILES.values():
                raise _invalid("state_entry_unexpected", f"unexpected entry {entry!r} in run {name}")
            _check_object(est, host, dev, "file", f"run {name} {entry}")
            files[entry] = os.path.join(rdir, entry)
        if JOURNAL_FILE not in files:
            raise _invalid("journal_missing", f"run {name} has no journal")
        doc = check_journal(_read_doc(files[JOURNAL_FILE], f"run {name} journal"))
        if doc["run_id"] != name:
            raise _invalid("journal_invalid", f"run {name} journal names another run")
        for kind, fname in ARTIFACT_FILES.items():
            want = doc["artifacts"][kind]
            if want is None:
                if fname in files and doc["terminal"]:
                    raise _invalid("artifact_unbound", f"run {name} has an unbound {kind} artifact")
                continue                    # an interrupted run may have written it just before its journal
            if fname not in files:
                raise _invalid("artifact_missing", f"run {name} {kind} artifact is missing")
            art = _check_artifact(kind, _read_doc(files[fname], f"run {name} {kind}"))
            if resolver.digest(art) != want:
                raise _invalid("artifact_mismatch", f"run {name} {kind} artifact differs from its journal binding")
        runs.append({"id": name, "doc": doc, "digest": resolver.digest(doc), "dir": rdir})
    runs.sort(key=lambda r: r["doc"]["sequence"])
    seqs = [r["doc"]["sequence"] for r in runs]
    if seqs != list(range(1, len(runs) + 1)):
        raise _invalid("history_ambiguous", "run sequence numbers are not one unique, gap-free chain")
    for i, r in enumerate(runs):
        prev = r["doc"]["previous"]
        if i == 0:
            continue
        p = runs[i - 1]
        if prev["run_id"] != p["id"] or prev["journal_digest"] != p["digest"] or \
                prev["phase"] != p["doc"]["phase"] or prev["outcome"] != p["doc"]["outcome"]:
            raise _invalid("history_inconsistent", f"run {r['id']} does not bind its predecessor")
    for r in runs:
        aw = r["doc"]["awaiting_reboot"]
        if aw is None:
            continue
        src = next((x for x in runs if x["id"] == aw["run_id"]), None)
        if src is None or src["doc"]["sequence"] >= r["doc"]["sequence"] or \
                src["doc"]["artifacts"]["validation"] != aw["validation_digest"] or \
                (src["doc"]["boot"] or {}).get("boot_id_digest") != aw["boot_id_digest"] or \
                validation_outcome(src["doc"]["validation"])[0] != "reboot_required":
            raise _invalid("history_inconsistent", f"run {r['id']} awaits a reboot of an unknown or altered run")
    return runs


def load_artifact(run, kind):
    doc = _read_doc(os.path.join(run["dir"], ARTIFACT_FILES[kind]), f"run {run['id']} {kind}")
    if resolver.digest(doc) != run["doc"]["artifacts"][kind]:
        raise _invalid("artifact_mismatch", f"run {run['id']} {kind} artifact differs from its journal binding")
    return _check_artifact(kind, doc)


def awaiting_reboot(latest):
    """The pending pre-reboot validation a new run must resume against: the latest
    run's own validation when its outcome is reboot_required (never a critical FAIL),
    or the reference it carried forward because it never validated (interrupted or
    stopped early); None once a run validated with any other outcome."""
    if latest is None:
        return None
    d = latest["doc"]
    if d["validation"] is not None:
        if validation_outcome(d["validation"])[0] == "reboot_required":
            return {"run_id": latest["id"], "boot_id_digest": d["boot"]["boot_id_digest"],
                    "validation_digest": d["validation"]["result_digest"]}
        return None
    return copy.deepcopy(d["awaiting_reboot"])


# ---------------------------------------------------------------------------
# One run's journal

class Run:
    def __init__(self, host, root, history):
        self.host = host
        latest = history[-1] if history else None
        self.id = host.attempt_id()
        if not RUN_NAME.match(self.id or ""):
            raise OrchestrationError("failed", "internal_failure", "run identifier is malformed")
        self.dir = os.path.join(root, self.id)
        os.mkdir(self.dir, 0o700)                       # exclusive: an existing id is never reused
        dfd = pa._open_dir(root)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
        self.doc = {
            "schema": JOURNAL_SCHEMA_ID, "run_id": self.id,
            "sequence": 1 if latest is None else latest["doc"]["sequence"] + 1,
            "previous": None if latest is None else {
                "run_id": latest["id"], "sequence": latest["doc"]["sequence"], "journal_digest": latest["digest"],
                "phase": latest["doc"]["phase"], "outcome": latest["doc"]["outcome"]},
            "awaiting_reboot": awaiting_reboot(latest),
            "phase": "started", "phases": [], "terminal": False, "outcome": None, "reason": None,
            "reboot_required": False, "boot": None, "bindings": None,
            "package": {"preflight": None, "recovery": None, "decision": None, "transaction_digest": None,
                        "apply": None, "current_transaction": None},
            "validation": None,
            "maintenance": {"gc": "not_run", "skipped": None, "receipt_id": None, "receipt_digest": None,
                            "plan_digest": None},
            "artifacts": {"inventory": None, "plan": None, "validation": None},
        }
        self.transition("started")

    def transition(self, phase, package=None, **updates):
        """Durable atomic journal update (temp file, fsync, rename, directory fsync).
        The in-memory document only ever holds what is durable on disk."""
        if self.doc["terminal"]:
            raise OrchestrationError("failed", "internal_failure", "run journal is already terminal")
        if self.doc["phases"] and PHASES.index(phase) <= PHASES.index(self.doc["phase"]) and phase != "finished":
            raise OrchestrationError("failed", "internal_failure", f"phase {phase} cannot follow {self.doc['phase']}")
        doc = copy.deepcopy(self.doc)
        doc.update(copy.deepcopy(updates))
        if package:
            doc["package"].update(copy.deepcopy(package))
        if phase != doc["phase"] or not doc["phases"]:
            doc["phases"].append(phase)
        doc["phase"] = phase
        doc["terminal"] = phase == "finished"
        check_journal(doc)
        pa.durable_replace(self.host, self.dir, JOURNAL_FILE, resolver.canonical_json(doc) + b"\n", 0o600)
        self.doc = doc

    def artifact(self, kind, value):
        """Write a canonical artifact durably; the journal binds its digest afterwards."""
        pa.durable_replace(self.host, self.dir, ARTIFACT_FILES[kind], resolver.canonical_json(value) + b"\n", 0o600)
        return resolver.digest(value)

    def finish(self, outcome, reason, **updates):
        self.transition("finished", outcome=outcome, reason=reason, **updates)
        return self.doc


# ---------------------------------------------------------------------------
# Package subsystem views (read-only; the package journals are authoritative)

def package_state(host):
    """Read-only summary of the apply and recovery journals. Unreadable or
    malformed package journals are reported as such, never guessed around."""
    try:
        attempts = {a: (p, s) for a, p, s in pa.prior_attempts(host)}
        records = dict(pr.recovery_records(host))
        resolved, _blocking = pr.resolution_state(host)
    except pa.ApplyError:
        return {"readable": False, "unresolved": [], "nonterminal_recoveries": [], "blocked": "none",
                "attempts": {}, "recoveries": {}}
    unresolved = sorted(a for a, (p, s) in attempts.items()
                        if a not in resolved and (p not in pa.TERMINAL or (p == "failed" and s)))
    nonterminal = sorted(n for n, d in records.items() if d["phase"] not in pr.TERMINAL)
    blocked = "none"
    for d in records.values():
        att = (d.get("original") or {}).get("attempt_id")
        if att in unresolved and d["phase"] == "manual_recovery_required":
            blocked = "manual_recovery_required"
    if blocked == "none":
        for d in records.values():
            att = (d.get("original") or {}).get("attempt_id")
            if att in unresolved and d["phase"] == "failed":
                blocked = "recovery_failed"
    return {"readable": True, "unresolved": unresolved, "nonterminal_recoveries": nonterminal, "blocked": blocked,
            "attempts": attempts, "recoveries": records}


def package_clean(state):
    return state["readable"] and not state["unresolved"] and not state["nonterminal_recoveries"] and \
        state["blocked"] == "none"


def _preflight_record(state):
    return {"readable": state["readable"], "unresolved_attempts": len(state["unresolved"]),
            "nonterminal_recoveries": len(state["nonterminal_recoveries"]), "blocked": state["blocked"]}


def _disk_apply_journal(host, attempt):
    if not isinstance(attempt, str) or not RUN_NAME.match(attempt):
        return None
    try:
        data = sa.read_regular(os.path.join(host.root, pa.JOURNAL_DIR, attempt, pa.JOURNAL_FILE), limit=1 << 20)
        return pa.check_journal(resolver.parse_strict(data, label="journal"))
    except (sa.SourceError, resolver.ResolverError, pa.ApplyError):
        return None


def _disk_recovery_journal(host, recovery):
    if not isinstance(recovery, str) or not RUN_NAME.match(recovery):
        return None
    try:
        data = sa.read_regular(os.path.join(host.root, pr.RECOVERY_DIR, recovery, pa.JOURNAL_FILE), limit=1 << 20)
        return pr.check_recovery_journal(resolver.parse_strict(data, label="journal"))
    except (sa.SourceError, resolver.ResolverError, pa.ApplyError):
        return None


def _disk_receipt(host, receipt_id):
    if not isinstance(receipt_id, str) or not RUN_NAME.match(receipt_id):
        return None
    try:
        data = sa.read_regular(os.path.join(host.root, gc.RECEIPT_DIR, f"{receipt_id}.json"), limit=1 << 20)
        return gc.check_document(resolver.parse_strict(data, label="receipt"), gc.RECEIPT_SCHEMA_FILE)
    except (sa.SourceError, resolver.ResolverError, pa.ApplyError):
        return None


# ---------------------------------------------------------------------------
# Steps

class Stop(Exception):
    """Finish the run with this outcome and reason."""

    def __init__(self, outcome, reason, **updates):
        super().__init__(reason)
        self.outcome = outcome
        self.reason = reason
        self.updates = updates


RECOVERY_RESULTS = ("complete", "abandoned", "manual_recovery_required", "failed")


def recover_first(host, run):
    """Recovery-first rule: package state must be safe before anything is planned."""
    state = package_state(host)
    run.transition("preflight", package={"preflight": _preflight_record(state)})
    if not state["readable"]:
        raise Stop("manual_recovery_required", "package_journal_unreadable")
    if state["blocked"] == "manual_recovery_required":
        raise Stop("manual_recovery_required", "manual_recovery_required")
    if state["blocked"] == "recovery_failed":
        raise Stop("failed", "recovery_failed")
    if not state["unresolved"] and not state["nonterminal_recoveries"]:
        return
    run.transition("recovering")
    before = set(state["recoveries"])
    try:
        out = pr.recover_interrupted_transaction(host)
        error = None
    except pa.ApplyError as e:
        out, error = None, e
    after = package_state(host)
    new = sorted(set(after["recoveries"]) - before)
    rid = new[-1] if len(new) == 1 else (out or {}).get("recovery_id")
    disk = _disk_recovery_journal(host, rid) if rid else None
    record = {"result": "failed", "recovery_id": rid if disk is not None else None,
              "attempt_id": ((disk or {}).get("original") or {}).get("attempt_id"),
              "journal_digest": resolver.digest(disk) if disk is not None else None}
    if error is not None:
        run.transition("recovered", package={"recovery": record})
        raise Stop("failed", "package_lock_busy" if error.code == "busy" else "recovery_failed")
    if out.get("result") == "nothing_to_recover":
        record["result"] = "nothing_to_recover"
    elif disk is None or disk != out or disk["phase"] not in RECOVERY_RESULTS or disk["result"] != disk["phase"]:
        run.transition("recovered", package={"recovery": record})
        raise Stop("failed", "recovery_unverified")
    else:
        record["result"] = disk["result"]
    run.transition("recovered", package={"recovery": record, "preflight": _preflight_record(after)})
    if record["result"] == "manual_recovery_required" or after["blocked"] == "manual_recovery_required":
        raise Stop("manual_recovery_required", "manual_recovery_required")
    if record["result"] == "failed" or after["blocked"] == "recovery_failed":
        raise Stop("failed", "recovery_failed")
    if not package_clean(after):
        raise Stop("failed", "recovery_unverified")


def fresh_plan(host, run):
    """Fresh probe and the production resolver; persisted as canonical artifacts."""
    try:
        inventory = host.inventory()
    except Exception:  # noqa: BLE001 - any probe failure: nothing is planned
        raise Stop("failed", "probe_failed") from None
    rules = host.rules()
    try:
        plan = resolver.check_plan(resolver.resolve(inventory, rules))
    except resolver.ResolverError:
        raise Stop("failed", "internal_failure") from None
    inv_digest = run.artifact("inventory", inventory)
    plan_digest = run.artifact("plan", plan)
    bindings = {"inventory_digest": plan["inventory_digest"] or resolver.digest(inventory),
                "rule_set_digest": plan["rule_set_digest"], "plan_digest": plan_digest,
                "plan_status": plan["status"]}
    return inventory, rules, plan, bindings, inv_digest, plan_digest


POLICY_CODES = ("plan_rejected", "unauthorised_source", "unsafe_change_set", "input_malformed", "trusted_rejected",
                "signed_by_missing")


def decide(host, run, inventory, rules, plan, bindings, inv_digest, plan_digest):
    """Plan acceptance and the read-only package precheck. Returns "apply" or
    "no_change"; never names a package itself."""
    artifacts = dict(run.doc["artifacts"], inventory=inv_digest, plan=plan_digest)
    if plan["status"] == "invalid":
        raise Stop("policy_blocked", "plan_invalid", bindings=bindings, artifacts=artifacts)
    if plan["status"] == "conflict" or plan["conflicts"]:
        raise Stop("policy_blocked", "plan_conflict", bindings=bindings, artifacts=artifacts)
    try:
        pt.accept_plan(plan)
    except pt.TransactionError:
        raise Stop("policy_blocked", "plan_policy_review", bindings=bindings, artifacts=artifacts) from None
    # Read-only precheck against the CURRENT local package state and metadata. Zero install
    # operations proves nothing needs to change. If the local metadata cannot prove the plan
    # (provenance), only the apply executor may refresh it: apply decides, under its own lock.
    tx_digest = None
    try:
        tx = pt.compile_transaction(inventory, rules, host.backend())
        installs = [o for o in tx["operations"] if o["action"] == "install"]
        decision = "apply" if installs else "no_change"
        tx_digest = pt.transaction_digest(tx)
        if tx["plan_digest"] != plan_digest or tx["inventory_digest"] != bindings["inventory_digest"]:
            raise Stop("stale", "inventory_changed", bindings=bindings, artifacts=artifacts)
    except pt.TransactionError as e:
        if e.code == "provenance_failure":
            decision = "apply"
        elif e.code in POLICY_CODES:
            raise Stop("policy_blocked", "transaction_rejected", bindings=bindings, artifacts=artifacts) from None
        elif e.code == "stale_transaction":
            raise Stop("stale", "stale_transaction", bindings=bindings, artifacts=artifacts) from None
        else:
            raise Stop("failed", "observation_failed", bindings=bindings, artifacts=artifacts) from None
    run.transition("planned", bindings=bindings, artifacts=artifacts,
                   package={"decision": decision, "transaction_digest": tx_digest})
    return decision


APPLY_STALE = ("stale_transaction", "stale_activation", "stale_bundle")


def apply_if_required(host, run, decision):
    """Only the existing apply executor mutates packages; its result is trusted only
    after its own durable journal says so and the package gate is clean again."""
    if decision != "apply":
        return
    before = set(package_state(host)["attempts"])
    run.transition("applying")
    try:
        out = pa.apply_current_plan(host)
        error = None
    except pa.ApplyError as e:
        out, error = None, e
    after = package_state(host)
    new = sorted(set(after["attempts"]) - before)
    attempt = (out or {}).get("attempt_id") if out is not None else (new[0] if len(new) == 1 else None)
    disk = _disk_apply_journal(host, attempt)
    record = {"attempt_id": attempt if disk is not None else None, "result": "failed",
              "phase": disk["phase"] if disk is not None else None,
              "transaction_digest": disk["digests"]["transaction_digest"] if disk is not None else None,
              "journal_digest": resolver.digest(disk) if disk is not None else None}
    if error is not None:
        code = error.code
        if code == "busy":
            raise Stop("failed", "package_lock_busy", package=dict(run.doc["package"], apply=record))
        if code == "recovery_required":
            raise Stop("stale", "package_state_changed", package=dict(run.doc["package"], apply=record))
        if code in APPLY_STALE:
            raise Stop("stale", "stale_transaction", package=dict(run.doc["package"], apply=record))
        if code in POLICY_CODES:
            raise Stop("policy_blocked", "transaction_rejected", package=dict(run.doc["package"], apply=record))
        raise Stop("failed", "apply_failed", package=dict(run.doc["package"], apply=record))
    verified = (disk is not None and len(new) == 1 and new[0] == attempt and disk == out
                and disk["phase"] == "complete" and disk["result"] in ("complete", "nothing_to_apply")
                and package_clean(after))
    if not verified:
        record["result"] = "unverified"
        raise Stop("failed", "apply_unverified", package=dict(run.doc["package"], apply=record))
    record["result"] = disk["result"]
    run.transition("applied", package={"apply": record, "preflight": _preflight_record(after)})


def validate(host, run, rules, bindings):
    """Fresh observation and the existing validator. A pending pre-reboot result
    is used only as the validator's `previous` (continuity), never as current."""
    try:
        inventory = host.inventory()
    except Exception:  # noqa: BLE001
        raise Stop("failed", "probe_failed") from None
    plan = resolver.check_plan(resolver.resolve(inventory, rules))
    if resolver.digest(plan) != bindings["plan_digest"]:
        raise Stop("stale", "inventory_changed")
    names = [p["name"] for p in plan["packages"] if p["disposition"] == "require"]
    try:
        ev = host.runtime().collect(inventory, names)
    except hv.ValidationError:
        raise Stop("failed", "observation_failed") from None
    boot = ev["boot"]["boot_id_digest"]
    pending = run.doc["awaiting_reboot"]
    previous, continuity = None, "not_applicable"
    if pending is not None:
        previous = run.pending_result
        continuity = "checked"
    try:
        try:
            result = hv.evaluate(inventory, rules, ev, previous=previous)
        except hv.ValidationError as e:
            if e.code != "stale_previous":
                raise
            result, continuity = hv.evaluate(inventory, rules, ev, previous=None), "profile_changed"
    except hv.ValidationError:
        raise Stop("failed", "validation_error") from None
    digest = run.artifact("validation", result)
    relation = result["bindings"]["boot"]["relation"]
    reboot = relation == "same_boot" or any(c["reason"] == "reboot_required" for c in result["checks"])
    record = {"result_digest": digest, "status": result["status"], "mode": result["mode"], "boot_relation": relation,
              "previous_result_digest": (result["bindings"]["previous"] or {}).get("result_digest"),
              "continuity": continuity, "critical_failures": result["summary"]["critical_failures"],
              "reboot_required": reboot}
    boot_rec = {"boot_id_digest": boot,
                "previous_boot_id_digest": pending["boot_id_digest"] if pending else None,
                "boot_changed": (pending["boot_id_digest"] != boot) if pending else None}
    current = result["bindings"]["package_transaction"]
    actionable = validation_outcome(record)[0] == "reboot_required"
    run.transition("validated", validation=record, boot=boot_rec, reboot_required=actionable,
                   artifacts=dict(run.doc["artifacts"], validation=digest),
                   package={"current_transaction": current})
    out, reason = validation_outcome(record)
    if out == "reboot_required" and pending and not boot_rec["boot_changed"]:
        reason = "boot_unchanged"
    return out, reason


def validation_outcome(v):
    """The orchestration outcome a validation record implies, in strict precedence:
    1. validation_failed  a critical FAIL is never hidden behind a reboot request;
    2. reboot_required    no FAIL, but the result is same-boot (not authoritative);
    3. validation_limited LIMITED / NOT_TESTED, never promoted to PASS;
    4. complete           an authoritative PASS with no pending reboot."""
    if v["status"] == "FAIL":
        return "validation_failed", "validation_failed"
    if v["reboot_required"]:
        return "reboot_required", "reboot_pending"
    if v["status"] != "PASS":
        return "validation_limited", "validation_limited"
    return "complete", "converged"


def collect_garbage(host, run, outcome):
    """Conservative GC through the existing M1-N collector: only after a terminal,
    non-critical, post-reboot-complete validation and clean package state."""
    if outcome not in GC_ELIGIBLE_OUTCOMES:
        return {"gc": "not_run", "skipped": "outcome_not_eligible", "receipt_id": None, "receipt_digest": None,
                "plan_digest": None}
    if not package_clean(package_state(host)):
        return {"gc": "not_run", "skipped": "package_state_unresolved", "receipt_id": None, "receipt_digest": None,
                "plan_digest": None}
    run.transition("collecting")
    try:
        out = gc.collect_garbage(host)
    except pa.ApplyError:
        return {"gc": "failed", "skipped": None, "receipt_id": None, "receipt_digest": None, "plan_digest": None}
    if out.get("result") == "nothing_to_collect":
        return {"gc": "nothing_to_collect", "skipped": None, "receipt_id": None, "receipt_digest": None,
                "plan_digest": out.get("plan_digest")}
    disk = _disk_receipt(host, out.get("receipt_id"))
    if disk is None or disk != out or disk["result"] not in ("complete", "partial", "failed"):
        return {"gc": "failed", "skipped": None, "receipt_id": None, "receipt_digest": None, "plan_digest": None}
    return {"gc": disk["result"], "skipped": None, "receipt_id": disk["receipt_id"],
            "receipt_digest": resolver.digest(disk), "plan_digest": disk["plan_digest"]}


# ---------------------------------------------------------------------------
# The operations

def reconcile(host):
    """Converge this machine toward its current qualified hardware plan, or stop
    safely with a precise state. Returns the final run journal."""
    if host.euid() != 0:
        raise OrchestrationError("not_root", "not_root", "reconcile requires effective UID 0")
    lock = OrchestrationLock(host).acquire()
    try:
        root = state_root(host, create=True)
        history = load_history(host, root)               # corrupt state: nothing below runs
        pending = awaiting_reboot(history[-1] if history else None)
        pending_result = None
        if pending is not None:
            src = next(r for r in history if r["id"] == pending["run_id"])
            pending_result = load_artifact(src, "validation")
        run = Run(host, root, history)
        run.pending_result = pending_result
        try:
            recover_first(host, run)
            inventory, rules, plan, bindings, inv_digest, plan_digest = fresh_plan(host, run)
            decision = decide(host, run, inventory, rules, plan, bindings, inv_digest, plan_digest)
            apply_if_required(host, run, decision)
            outcome, reason = validate(host, run, rules, bindings)
        except Stop as s:
            return run.finish(s.outcome, s.reason, **s.updates)
        except (pa.ApplyError, pt.TransactionError, resolver.ResolverError, hv.ValidationError, sa.SourceError):
            return run.finish("failed", "internal_failure")
        maintenance = collect_garbage(host, run, outcome)
        return run.finish(outcome, reason, maintenance=maintenance)
    finally:
        lock.release()


def exit_status(doc):
    status = OUTCOME_EXIT[doc["outcome"]]
    if doc["maintenance"]["gc"] in MAINTENANCE_FAILED and status in (EXIT_OK, EXIT_VALIDATION_LIMITED):
        return EXIT_MAINTENANCE_FAILED
    return status


def status(host):
    """Read-only summary: no lock, no directory creation, no journal write."""
    if host.euid() != 0:
        raise OrchestrationError("not_root", "not_root", "status requires effective UID 0")
    out = {"state": "none", "invalid_reason": None, "runs": 0, "latest": None, "validation": None,
           "package": None, "reboot": {"required": False, "boot_changed": None},
           "current": {"determinable": False, "current": None, "changed": []}}
    pkg = package_state(host)
    out["package"] = {"readable": pkg["readable"], "recovery_required": bool(pkg["unresolved"] or
                                                                               pkg["nonterminal_recoveries"]),
                      "unresolved_attempts": len(pkg["unresolved"]), "blocked": pkg["blocked"]}
    try:
        history = load_history(host, state_root(host, create=False))
    except OrchestrationError as e:
        out.update(state="invalid", invalid_reason=e.reason)
        return out
    except OSError:
        out.update(state="invalid", invalid_reason="state_unsafe")
        return out
    if not history:
        return out
    latest = history[-1]
    d = latest["doc"]
    out["state"] = "valid"
    out["runs"] = len(history)
    out["latest"] = {"run_id": latest["id"], "sequence": d["sequence"], "phase": d["phase"],
                     "terminal": d["terminal"], "interrupted": not d["terminal"], "outcome": d["outcome"],
                     "reason": d["reason"], "reboot_required": d["reboot_required"],
                     "maintenance": d["maintenance"]["gc"], "journal_digest": latest["digest"],
                     "apply_attempt": (d["package"]["apply"] or {}).get("attempt_id"),
                     "recovery_id": (d["package"]["recovery"] or {}).get("recovery_id")}
    with_validation = next((r for r in reversed(history) if r["doc"]["validation"] is not None), None)
    if with_validation is not None:
        v = with_validation["doc"]["validation"]
        out["validation"] = {"run_id": with_validation["id"], "status": v["status"], "mode": v["mode"],
                             "boot_relation": v["boot_relation"], "result_digest": v["result_digest"]}
    pending = awaiting_reboot(latest)
    out["reboot"]["required"] = pending is not None
    try:
        inventory = host.inventory()
        rules = host.rules()
        plan = resolver.check_plan(resolver.resolve(inventory, rules))
        names = [p["name"] for p in plan["packages"] if p["disposition"] == "require"]
        ev = host.runtime().collect(inventory, names)
        if pending is not None:
            out["reboot"]["boot_changed"] = pending["boot_id_digest"] != ev["boot"]["boot_id_digest"]
        if with_validation is not None:
            fr = hv.freshness(load_artifact(with_validation, "validation"), inventory, rules, ev)
            out["current"] = {"determinable": True, "current": fr["current"], "changed": fr["changed"]}
    except Exception:  # noqa: BLE001 - currentness is reported as not determinable, never guessed
        pass
    return out
