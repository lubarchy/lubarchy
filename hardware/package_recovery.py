"""LUBARCHY interrupted hardware-package transaction recovery (M1-L).

The privileged operation is "recover the interrupted LUBARCHY hardware-package
transaction", never "repair APT". Recovery first proves which state the
system is actually in. The apply journal is evidence, not truth: the real
dpkg/APT state is authoritative, and every observed change must be
attributable to the original transaction or recovery fails closed.

Two package sets are kept strictly apart:

  transaction closure   the M1-I install closure of the original transaction
                        (exact packages, versions, architectures, marks), bound
                        by the journal and the re-verified staged bundle.
  recovery impact set   pre-existing packages OUTSIDE the closure whose dpkg
                        state the exact interrupted transaction disturbed
                        through dpkg triggers. They are not dependencies and
                        never join the closure. A package is in the set only if
                        the durable pre-apply evidence bound in the apply
                        journal before `applying` (dpkg log cursor and the
                        pre-apply record of every trigger-interest package)
                        and the dpkg log written after that cursor prove it:
                        the package was fully installed with exactly that
                        identity and trigger declaration before apply, its
                        first logged change is a trigger activation after the
                        transaction's first closure operation, its later
                        changes are trigger processing only, and its current
                        state is exactly its last logged state. Every package
                        line of that log delta must belong to the closure or
                        to the impact set, otherwise nothing is attributed.

Classifications (docs/package-recovery.md):

  no_mutation                    the attempt stopped before `applying`; the
                                 package state equals the bound pre-state.
  no_package_change              APT was started but no package changed
                                 (state equals the bound pre-state).
  applied_but_unvalidated        the exact expected post-state is present.
  bounded_incomplete_transaction dpkg was interrupted; every non-final package
                                 is a closure package (exact version,
                                 architecture, mark) in a configurable state or
                                 an attributed recovery-impact package.
  manual_recovery_required       anything else: unrelated pending or damaged
                                 packages, unattributable trigger state,
                                 version/architecture/mark/hold deviations,
                                 missing or inconsistent evidence, a missing or
                                 corrupt staged bundle, changed sources,
                                 untrusted or ambiguous journals.

The pre-state is bound only as a digest. Attribution is proven by
reconstruction: from the current state, the closure packages (which the
install-only transaction added) are dropped and each attributed impact
package is replaced by its bound pre-apply record; the result must hash to
exactly the bound pre-state digest. The closure stays bound throughout: it
is checked against the journal and bundle, authorises its own targets and is
validated in the post-state.

The only mutations recovery ever performs are exact-package
`dpkg --configure <names>` and `dpkg --triggers-only <names>`, and every name
must be in the authorised recovery set (non-final closure packages plus
non-final attributed impact packages). Measured on Debian 13 / APT 3.0.3: an
APT rerun, `apt-get -f install` and `dpkg --configure -a` all configure
unrelated pending packages. No APT run, no download, no install, upgrade,
downgrade, removal, purge or mark change; dpkg takes its own locks. Already
complete states are validated, never re-run.
"""

import hashlib
import os
import re
import stat

import package_apply as pa
import package_staging as ps
import package_transaction as pt
import resolver
import source_activation as sa

RECOVERY_SCHEMA_ID = "lubarchy.packages.recovery-journal/v1"
RECOVERY_SCHEMA_FILE = "recovery-journal-v1.schema.json"
RECOVERY_DIR = "var/lib/lubarchy/package-recoveries"
DPKG_INFO = "var/lib/dpkg/info"
DPKG_LOG = "var/log/dpkg.log"
DPKG_LOG_LIMIT = 1 << 30            # bound log size accepted for the prefix digest
DELTA_LIMIT = 16 << 20              # log bytes written after the cursor that recovery will parse

PHASES = ("prepared", "locked", "inspected", "classified", "verified", "recovering", "processing_triggers", "recovered",
          "validated", "complete", "abandoned", "manual_recovery_required", "failed")
MUTATING_PHASES = ("recovering", "processing_triggers")

# The only two recovery command shapes. `--no-triggers` is an internal, fixed option: dpkg's
# `--configure <pkg>` otherwise also runs pending trigger processing, so naming an exact target
# alone would not confine maintainer/trigger execution to the authorised set. Trigger processing
# is a separate, separately authorised phase.
CONFIGURE_ARGV = (pa.DPKG, "--configure", "--no-triggers")
TRIGGERS_ARGV = (pa.DPKG, "--triggers-only")
TERMINAL = ("complete", "abandoned", "manual_recovery_required", "failed")
RESOLVING = ("complete", "abandoned")
CLASSIFICATIONS = ("no_mutation", "no_package_change", "applied_but_unvalidated", "bounded_incomplete_transaction",
                   "manual_recovery_required")

INSTALLED = "install ok installed"
CLOSURE_PARTIAL = ("install ok unpacked", "install ok half-configured", "install ok triggers-pending",
                   "install ok triggers-awaited")
CONFIGURE_STATES = ("install ok unpacked", "install ok half-configured")

# dpkg.log grammar (dpkg 1.22): "<date> <time> startup <what> <action>",
# "<date> <time> status <state> <pkg>:<arch> <version>" and
# "<date> <time> <action> <pkg>:<arch> <version> <version>". Times are never used.
LOG_STAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2} (.*)$")
LOG_PKG = re.compile(r"^([a-z0-9][a-z0-9+.-]{1,127}):([a-z0-9][a-z0-9-]{0,31})$")
CLOSURE_LOG_STATES = ("half-installed", "unpacked", "half-configured", "installed", "triggers-pending",
                      "triggers-awaited")
IMPACT_LOG_STATES = ("triggers-pending", "triggers-awaited", "half-configured", "installed")


class Manual(Exception):
    """A fail-closed classification reason (manual_recovery_required)."""


# ---------------------------------------------------------------------------
# Recovery journal

class RecoveryJournal:
    def __init__(self, host):
        self.host = host
        self.root = pa._ensure_dir(host, RECOVERY_DIR, 0o700)
        self.id = host.attempt_id()
        if len(self.id) != 16 or any(c not in "0123456789abcdef" for c in self.id):
            raise pa.ApplyError("journal_failure", "recovery identifier is malformed")
        self.dir = os.path.join(self.root, self.id)
        os.mkdir(self.dir, 0o700)
        self.doc = {"schema": RECOVERY_SCHEMA_ID, "recovery_id": self.id, "phase": "prepared", "result": None,
                    "failure": None, "mutation_started": False, "phases": [], "original": None, "observed": None,
                    "classification": None, "reason": None, "action": None, "transaction_closure": None,
                    "recovery_impact": None, "authorized_targets": [], "recovery_argv": [], "recovery_steps": [],
                    "post_state": None}
        self.transition("prepared")

    def transition(self, phase, **updates):
        import copy
        if phase not in PHASES:
            raise pa.ApplyError("journal_failure", f"unknown recovery phase {phase}")
        cur = self.doc["phase"]
        if self.doc["phases"] and cur in TERMINAL:
            raise pa.ApplyError("journal_failure", f"recovery journal is already terminal ({cur})")
        if self.doc["phases"] and phase not in TERMINAL and PHASES.index(phase) <= PHASES.index(cur):
            raise pa.ApplyError("journal_failure", f"recovery phase {phase} cannot follow {cur}")
        doc = copy.deepcopy(self.doc)
        doc.update(updates)
        if phase in MUTATING_PHASES:
            doc["mutation_started"] = True
        doc["phase"] = phase
        doc["phases"].append({"phase": phase, "at": self.host.now()})
        check_recovery_journal(doc)
        pa.durable_replace(self.host, self.dir, pa.JOURNAL_FILE, resolver.canonical_json(doc) + b"\n", 0o600)
        self.doc = doc


def check_recovery_journal(doc):
    import json
    import jsonschema
    with open(os.path.join(resolver.SCHEMA_DIR, RECOVERY_SCHEMA_FILE), encoding="utf-8") as f:
        schema = json.load(f)
    if any(True for _ in jsonschema.Draft202012Validator(schema).iter_errors(doc)):
        raise pa.ApplyError("journal_failure", "journal does not satisfy recovery-journal v1")
    return doc


def _read_json(path, check):
    try:
        return check(resolver.parse_strict(sa.read_regular(path, limit=1 << 20), label="journal"))
    except (sa.SourceError, resolver.ResolverError, pa.ApplyError):
        return None


def recovery_records(host):
    """[(recovery_id, doc)] of every recovery journal; unreadable ones fail closed."""
    root = os.path.join(host.root, RECOVERY_DIR)
    try:
        st = os.lstat(root)
    except FileNotFoundError:
        return []
    if not stat.S_ISDIR(st.st_mode):
        raise pa.ApplyError("recovery_required", "recovery journal directory is not a directory")
    out = []
    for name in sorted(os.listdir(root)):
        doc = _read_json(os.path.join(root, name, pa.JOURNAL_FILE), check_recovery_journal)
        if doc is None:
            raise pa.ApplyError("recovery_required", f"recovery journal {name} is unreadable or malformed")
        out.append((name, doc))
    return out


def resolution_state(host):
    """({attempt_id: True if resolved}, blocking reason or None) for the apply gate."""
    resolved, blocking = set(), None
    for name, doc in recovery_records(host):
        att = (doc.get("original") or {}).get("attempt_id")
        if doc["phase"] in RESOLVING and att:
            resolved.add(att)
        elif doc["phase"] not in TERMINAL:
            blocking = f"recovery {name} stopped in phase {doc['phase']}"
    for name, doc in recovery_records(host):
        att = (doc.get("original") or {}).get("attempt_id")
        if doc["phase"] in ("manual_recovery_required", "failed") and att and att not in resolved:
            blocking = blocking or f"recovery {name} ended {doc['phase']}"
    return resolved, blocking


# ---------------------------------------------------------------------------
# State inspection

def state_entries(backend, native):
    entries, holds = pa.installed_snapshot(backend, native)
    return entries, holds


def state_digest(entries):
    return resolver.digest([{k: entries[key][k] for k in ("package", "architecture", "version", "status", "automatic")}
                            for key in sorted(entries)])


def _triggers_file(host, package, architecture):
    """Bytes of the package's dpkg trigger declaration file (info/<pkg>[:<arch>].triggers), or None."""
    for name in (f"{package}:{architecture}.triggers", f"{package}.triggers"):
        path = os.path.join(host.root, DPKG_INFO, name)
        if not os.path.lexists(path):
            continue
        try:
            return sa.read_regular(path, limit=1 << 16)
        except sa.SourceError:
            return None                 # not a regular file: never eligible
    return None


def trigger_interests(data):
    """Trigger names a dpkg `triggers` control file declares interest in."""
    out = set()
    for line in (data or b"").decode("utf-8", "replace").splitlines():
        words = line.split("#", 1)[0].split()
        if len(words) == 2 and words[0] in ("interest", "interest-noawait", "interest-await"):
            out.add(words[1])
    return sorted(out)


def _sha(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _open_log(host):
    """(fd, size) of the root-owned, regular, not group/other-writable dpkg log; None if absent."""
    path = os.path.join(host.root, DPKG_LOG)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError:
        raise Manual("dpkg log is not a regular file") from None
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != host.owner_uid or st.st_mode & 0o022:
        os.close(fd)
        raise Manual("dpkg log has an unexpected type, owner or mode")
    return fd, st.st_size


def _read_exact(fd, size):
    chunks, left = [], size
    while left:
        b = os.read(fd, min(left, 1 << 20))
        if not b:
            break
        chunks.append(b)
        left -= len(b)
    return b"".join(chunks)


def dpkg_log_cursor(host):
    """{"size", "sha256"}: the durable pre-apply position and prefix digest of the dpkg log."""
    opened = _open_log(host)
    if opened is None:
        return {"size": 0, "sha256": _sha(b"")}
    fd, size = opened
    try:
        if size > DPKG_LOG_LIMIT:
            raise Manual("dpkg log is too large to bind")
        h, left = hashlib.sha256(), size
        while left:
            b = os.read(fd, min(left, 1 << 20))
            if not b:
                raise Manual("dpkg log shrank while it was bound")
            h.update(b)
            left -= len(b)
        return {"size": size, "sha256": "sha256:" + h.hexdigest()}
    finally:
        os.close(fd)


def bind_recovery_evidence(host, entries):
    """Pre-apply recovery-impact evidence, recorded durably in the apply journal
    before `applying`: the dpkg log cursor and, for every package that declares
    dpkg trigger interest, its exact identity, status and declaration digest."""
    interest = []
    for key in sorted(entries):
        e = entries[key]
        data = _triggers_file(host, e["package"], e["architecture"])
        if data is None or not trigger_interests(data):
            continue
        interest.append({"package": e["package"], "architecture": e["architecture"], "version": e["version"],
                         "status": e["status"], "triggers_sha256": _sha(data)})
    try:
        cursor = dpkg_log_cursor(host)
    except Manual as m:
        raise pa.ApplyError("contract_failure", str(m)) from None
    return {"dpkg_log": cursor, "trigger_interest": interest}


def read_delta(host, cursor):
    """The dpkg log text written after the bound cursor; the bound prefix must be intact."""
    opened = _open_log(host)
    if opened is None:
        raise Manual("dpkg log is missing (cannot attribute dpkg activity)")
    fd, size = opened
    try:
        if size < cursor["size"]:
            raise Manual("dpkg log does not continue the bound pre-apply cursor (truncated or rotated)")
        h, left = hashlib.sha256(), cursor["size"]
        while left:
            b = os.read(fd, min(left, 1 << 20))
            if not b:
                raise Manual("dpkg log does not continue the bound pre-apply cursor")
            h.update(b)
            left -= len(b)
        if "sha256:" + h.hexdigest() != cursor["sha256"]:
            raise Manual("dpkg log does not continue the bound pre-apply cursor (prefix digest differs)")
        if size - cursor["size"] > DELTA_LIMIT:
            raise Manual("dpkg log delta is too large to attribute")
        delta = _read_exact(fd, size - cursor["size"])
    finally:
        os.close(fd)
    try:
        text = delta.decode("utf-8")
    except UnicodeDecodeError:
        raise Manual("dpkg log delta is not valid text") from None
    if text and not text.endswith("\n"):
        raise Manual("dpkg log delta ends with an incomplete line")
    return delta, text


def parse_log_line(line):
    """("startup",) | ("status", state, key) + (version,) | (action, key, old, new); Manual if unknown."""
    m = LOG_STAMP.match(line)
    words = m.group(1).split(" ") if m else []
    if len(words) == 3 and words[0] == "startup":
        return ("startup",)
    if len(words) == 4 and words[0] == "status" and LOG_PKG.match(words[2]):
        return ("status", words[1], tuple(LOG_PKG.match(words[2]).groups()), words[3])
    if len(words) == 4 and words[0] in ("install", "upgrade", "configure", "trigproc", "remove", "purge") \
            and LOG_PKG.match(words[1]):
        return (words[0], tuple(LOG_PKG.match(words[1]).groups()), words[2], words[3])
    raise Manual("dpkg log delta contains an unrecognised line")


def attribute_impact(host, attempt, closure, entries):
    """Prove the recovery impact set of the exact interrupted transaction from the
    durable evidence. Returns {"evidence": ..., "packages": {key: record}}; raises
    Manual on anything that is not provably caused by this transaction."""
    ev = attempt.get("recovery_evidence")
    if not ev:
        raise Manual("apply journal lacks the pre-apply recovery-impact evidence")
    delta, text = read_delta(host, ev["dpkg_log"])
    bound = {(r["package"], r["architecture"]): r for r in ev["trigger_interest"]}
    if len(bound) != len(ev["trigger_interest"]):
        raise Manual("pre-apply trigger-interest evidence is ambiguous")
    last, impact, started, current_op = {}, {}, False, None
    lines = text.splitlines()
    for line in lines:
        rec = parse_log_line(line)
        if rec[0] == "startup":
            continue
        if rec[0] == "status":
            kind, state, key, version = rec
        else:
            kind, key, old, new = rec
        name = f"{key[0]}:{key[1]}"
        if key in closure:
            want = closure[key]["version"]
            ok = (kind == "status" and state in CLOSURE_LOG_STATES and version == want) or \
                 (kind == "install" and old == "<none>" and new == want) or \
                 (kind in ("configure", "trigproc") and old == want and new == "<none>")
            if not ok:
                raise Manual(f"dpkg log shows an unexpected operation on closure package {name}")
            if kind == "status":
                last[key] = state
            else:
                current_op = f"{kind} {name}"
            started = True
            continue
        b = bound.get(key)
        if b is None:
            raise Manual(f"dpkg log shows {name}, which is neither in the transaction closure nor a bound "
                         "pre-apply trigger-interest package")
        if not started:
            raise Manual(f"dpkg log shows {name} before the transaction's first closure operation")
        if b["status"] != INSTALLED:
            raise Manual(f"{name} was not fully installed before apply (not attributable)")
        if key not in impact:
            if not (kind == "status" and state == "triggers-pending" and version == b["version"]):
                raise Manual(f"first logged change of {name} is not a trigger activation")
            impact[key] = {"package": key[0], "architecture": key[1], "version": b["version"],
                           "pre_status": b["status"], "activated_during": current_op,
                           "events": [], "interests": []}
        ok = (kind == "status" and state in IMPACT_LOG_STATES and version == b["version"]) or \
             (kind == "trigproc" and old == b["version"] and new == "<none>")
        if not ok:
            raise Manual(f"dpkg log shows a non-trigger operation on {name}")
        impact[key]["events"].append(f"status {state}" if kind == "status" else kind)
        if kind == "status":
            last[key] = state
    for key, p in closure.items():
        e = entries.get(key)
        if e is not None and key in last and e["status"] != f"install ok {last[key]}":
            raise Manual(f"dpkg log and package database disagree on {key[0]}")
    for key, r in impact.items():
        e = entries.get(key)
        name = f"{key[0]}:{key[1]}"
        if e is None:
            raise Manual(f"recovery-impact package {name} is missing or has a different architecture")
        if e["version"] != r["version"]:
            raise Manual(f"recovery-impact package {name} version deviates from the pre-apply record")
        if e["status"] != f"install ok {last[key]}":
            raise Manual(f"recovery-impact package {name} state differs from its last logged state")
        data = _triggers_file(host, key[0], key[1])
        if data is None or _sha(data) != bound[key]["triggers_sha256"] or not trigger_interests(data):
            raise Manual(f"trigger declarations of {name} changed since the pre-apply record")
        r["interests"] = trigger_interests(data)
        if not all(re.fullmatch(r"[!-~]{1,256}", t) for t in r["interests"]):
            raise Manual(f"trigger declarations of {name} are not recordable")
        r["observed_status"] = e["status"]
    return {"evidence": {"dpkg_log_cursor": dict(ev["dpkg_log"]), "delta_sha256": _sha(delta),
                         "delta_lines": len(lines)},
            "packages": impact}


def health(host):
    rc, out = host.run([pa.DPKG, "--audit"])
    audit_clean = rc == 0 and not out.strip()
    rc, _out = host.run([pa.APT_GET, "check"])
    return audit_clean, rc == 0


def unresolved_attempts(host):
    """Apply attempts that need recovery: non-terminal, or failed after APT started, and not
    resolved by a completed/abandoned recovery."""
    resolved, _blocking = resolution_state(host)
    out = []
    for name, phase, started in pa.prior_attempts(host):
        if name in resolved:
            continue
        if phase not in pa.TERMINAL or (phase == "failed" and started):
            out.append(name)
    return out


def load_attempt(host, name):
    doc = _read_json(os.path.join(host.root, pa.JOURNAL_DIR, name, pa.JOURNAL_FILE), pa.check_journal)
    if doc is None or doc["attempt_id"] != name:
        raise Manual("apply journal identity cannot be trusted")
    return doc


# ---------------------------------------------------------------------------
# Classification

def classify(host, attempt):
    """Return (classification, details) from the REAL state; raise Manual on any doubt."""
    native = host.architecture()
    backend = host.backend()
    entries, holds = state_entries(backend, native)
    audit_clean, check_ok = health(host)
    digests = attempt["digests"]
    before = digests["installed_state_digest_before"]
    observed = {"state_digest": state_digest(entries), "dpkg_audit_clean": audit_clean, "apt_check_ok": check_ok,
                "pending": [{k: e[k] for k in ("package", "architecture", "version", "status")}
                            for _k, e in sorted(entries.items()) if e["status"] != INSTALLED],
                "pre_state_reconstructed": False}
    details = {"observed": observed, "entries": entries, "manifest": None, "bundle": None,
               "configure": [], "triggers": [], "authorized": [], "authorized_keys": {}, "closure": {},
               "reconstructed": None,
               "impact": {"evidence": None, "packages": {}}}

    if not attempt["apply_started"]:
        # APT never started: nothing LUBARCHY did can have changed packages.
        if not audit_clean or not check_ok:
            raise Manual("dpkg/APT state is unhealthy although no LUBARCHY package mutation started")
        if before is not None and observed["state_digest"] != before:
            raise Manual("package state changed although no LUBARCHY package mutation started")
        observed["pre_state_reconstructed"] = before is not None
        return "no_mutation", details

    for key in ("transaction_digest", "bundle_digest", "source_activation_digest", "repository_metadata_digest"):
        if not digests[key]:
            raise Manual(f"apply journal lacks {key}")
    if before is None:
        raise Manual("apply journal lacks the bound pre-state digest")

    # The exact original payload set: the root-owned bundle, re-verified (re-hash, size, dpkg-deb identity).
    bundle = os.path.join(host.root, pa.STAGING_DIR, ps.bundle_name(digests["transaction_digest"]))
    try:
        manifest, _results = ps.verify_bundle(bundle, host.inspector(), expected={
            "transaction_digest": digests["transaction_digest"],
            "source_activation_digest": digests["source_activation_digest"],
            "repository_metadata_digest": digests["repository_metadata_digest"]})
    except (ps.StagingError, OSError) as e:
        raise Manual(f"staged bundle cannot be verified ({getattr(e, 'code', 'missing')})") from None
    if manifest["bundle_digest"] != digests["bundle_digest"]:
        raise Manual("staged bundle digest differs from the apply journal")
    want = [{k: p[k] for k in ("package", "architecture", "version", "relation")} for p in manifest["payloads"]]
    if want != attempt["expected"]["closure"]:
        raise Manual("expected closure differs between the apply journal and the bundle")
    details["manifest"], details["bundle"] = manifest, bundle

    closure, absent, partial = {}, [], []
    for p in manifest["payloads"]:
        key = (p["package"], p["architecture"])
        e = entries.get(key)
        closure[key] = p
        if e is None:
            absent.append(p["package"])
            continue
        if e["version"] != p["version"]:
            raise Manual(f"{p['package']} has an unexpected version")
        if e["status"] != INSTALLED and e["status"] not in CLOSURE_PARTIAL:
            raise Manual(f"{p['package']} is in state '{e['status']}' (needs re-unpacking; not automatic)")
        if e["automatic"] != p["automatic_after"]:
            raise Manual(f"{p['package']} mark deviates from the transaction")
        if e["status"] != INSTALLED:
            partial.append(e)
    details["closure"] = closure
    others = {k: dict(v) for k, v in entries.items() if k not in closure}
    if any(k[0] in {c[0] for c in closure} for k in others):
        raise Manual("a closure package is present with an unexpected architecture")
    nonfinal = [e for _k, e in sorted(others.items()) if e["status"] != INSTALLED]
    if absent and len(absent) != len(closure):
        raise Manual("closure is partially absent (would need re-unpacking from payload)")
    if absent:
        for e in nonfinal:
            raise Manual(f"unrelated package {e['package']} is in state '{e['status']}'")
        if state_digest(entries) != before:
            raise Manual("package state changed although no closure package is present")
        observed["pre_state_reconstructed"] = True
        if not audit_clean or not check_ok:
            raise Manual("dpkg/APT state is unhealthy")
        return "no_package_change", details

    # Any dpkg completion needs the durable attribution evidence; a package outside the
    # closure may be non-final only if it is a proven member of the recovery impact set.
    impact = {"evidence": None, "packages": {}}
    if partial or nonfinal:
        impact = attribute_impact(host, attempt, closure, entries)
    for e in nonfinal:
        if (e["package"], e["architecture"]) not in impact["packages"]:
            raise Manual(f"unrelated package {e['package']} is in state '{e['status']}'")
    details["impact"] = impact

    # Pre-state reconstruction: drop the closure packages the install-only transaction added
    # and restore each attributed impact package to its bound pre-apply status.
    reconstructed = {k: dict(v, status=impact["packages"][k]["pre_status"]) if k in impact["packages"] else v
                     for k, v in others.items()}
    if state_digest(reconstructed) != before:
        raise Manual("changes outside the transaction closure (pre-state reconstruction does not match)")
    observed["pre_state_reconstructed"] = True
    details["reconstructed"] = reconstructed
    if not partial and not nonfinal:
        if not audit_clean or not check_ok:
            raise Manual("expected packages are present but dpkg/APT state is unhealthy")
        return "applied_but_unvalidated", details

    # Bounded incomplete transaction: A = non-final closure packages, B = non-final attributed
    # impact packages. Nothing else can be a target.
    targets = partial + nonfinal
    details["authorized"] = sorted({e["package"] for e in targets})
    details["authorized_keys"] = {e["package"]: (e["architecture"], e["version"], e["automatic"]) for e in targets}
    details["configure"] = sorted(e["package"] for e in targets if e["status"] in CONFIGURE_STATES)
    details["triggers"] = sorted(e["package"] for e in targets if e["status"] not in CONFIGURE_STATES)
    return "bounded_incomplete_transaction", details


def _split(argv):
    """(head, targets) of one of the two recovery command shapes, else (None, [])."""
    argv = list(argv)
    for head in (CONFIGURE_ARGV, TRIGGERS_ARGV):
        if tuple(argv[:len(head)]) == head:
            return head, argv[len(head):]
    return None, []


def authorize_argv(argvs, details):
    """Every recovery argv is exact-package dpkg completion (configure with triggers suppressed,
    or trigger processing) whose targets are an exact subset of the authorised recovery set
    (closure + attributed impact, non-final only)."""
    allowed = set(details["authorized"])
    closure_names = {k[0] for k in details["closure"]}
    impact_names = {k[0] for k in details["impact"]["packages"]}
    for argv in argvs:
        head, targets = _split(argv)
        if head is None or not targets or not pa.allowed_command(argv):
            raise pa.ApplyError("contract_failure", "recovery argv is not exact-package dpkg completion")
        if not set(targets) <= allowed or not set(targets) <= (closure_names | impact_names):
            raise pa.ApplyError("contract_failure", "recovery argv names a package outside the authorised "
                                                    "recovery set")
    return argvs


class StopManual(Exception):
    """A recovery command revealed work outside the authorisation: the journal is already
    terminal (manual_recovery_required) with the step evidence preserved."""


def contain_step(host, argv, cursor, details, before, holds_before):
    """Containment of ONE executed recovery command: its own dpkg log delta (from the cursor
    bound immediately before it) and the package state after it. Returns (step evidence,
    problems, entries). Nothing outside the authorised set may have been executed or changed;
    an unauthorised package merely marked trigger-pending is reported, never processed."""
    delta, text = read_delta(host, cursor)
    head, targets = _split(argv)
    action = "configure" if head == CONFIGURE_ARGV else "trigproc"
    authorized = details["authorized_keys"]
    handlers, problems = [], []
    lines = text.splitlines()
    for line in lines:
        rec = parse_log_line(line)
        if rec[0] == "startup":
            continue
        if rec[0] == "status":
            kind, state, key, version = rec
        else:
            kind, key, version, new = rec
        name = f"{key[0]}:{key[1]}"
        a = authorized.get(key[0])
        if a is None or a[:2] != (key[1], version):
            if kind == "status" and state == "triggers-pending":
                problems.append(f"recovery activated a trigger of {name}, outside the authorised recovery set "
                                "(its handler was not executed)")
            else:
                problems.append(f"recovery command logged {kind} for {name}, outside the authorised recovery set")
            continue
        if kind == "status":
            if state not in ("unpacked", "half-configured", "installed", "triggers-pending", "triggers-awaited"):
                problems.append(f"recovery left {name} in state {state}")
            continue
        if kind != action or new != "<none>" or key[0] not in targets:
            problems.append(f"recovery command logged {kind} for {name}, which it was not authorised to execute")
            continue
        handlers.append(f"{kind} {name}")
    entries, holds = state_entries(host.backend(), host.architecture())
    for k in sorted(set(before) | set(entries)):
        if k[0] in authorized:
            e = entries.get(k)
            if e is None or (e["architecture"], e["version"], e["automatic"]) != authorized[k[0]]:
                problems.append(f"authorised package {k[0]} changed identity or mark during recovery")
        elif entries.get(k) != before.get(k):
            problems.append(f"package {k[0]} outside the authorised recovery set changed during recovery")
    if holds != holds_before:
        problems.append("held packages changed during recovery")
    pending = [{"package": e["package"], "status": e["status"]} for k, e in sorted(entries.items())
               if k[0] in authorized and e["status"] != INSTALLED]
    step = {"argv": list(argv), "log_cursor": cursor, "delta_sha256": _sha(delta), "delta_lines": len(lines),
            "handlers": handlers, "pending_after": pending}
    return step, problems, entries


def run_step(host, journal, details, phase, argv, before, holds_before):
    """Authorise, bind a log cursor, journal, execute ONE command, then contain and re-inspect.
    Returns the package state after it; never broadens the authorisation."""
    authorize_argv([argv], details)
    try:
        cursor = dpkg_log_cursor(host)
    except Manual as m:
        journal.transition("manual_recovery_required", result="manual_recovery_required", reason=str(m))
        raise StopManual() from None
    step = {"argv": list(argv), "log_cursor": cursor, "delta_sha256": None, "delta_lines": None, "handlers": [],
            "pending_after": None}
    steps = journal.doc["recovery_steps"]
    journal.transition(phase, recovery_argv=journal.doc["recovery_argv"] + [list(argv)],
                       recovery_steps=steps + [step])
    rc, _out = host.run(argv)
    try:
        step, problems, entries = contain_step(host, argv, cursor, details, before, holds_before)
    except Manual as m:
        journal.transition("manual_recovery_required", result="manual_recovery_required",
                           reason=f"recovery command evidence: {m}"[:512])
        raise StopManual() from None
    if problems:
        journal.transition("manual_recovery_required", result="manual_recovery_required",
                           reason=problems[0][:512], recovery_steps=steps + [step])
        raise StopManual()
    if rc != 0:
        journal.transition("failed", result="failed", failure={"code": "recovery_failed", "phase": phase},
                           recovery_steps=steps + [step])
        raise pa.ApplyError("recovery_failed", f"{argv[1]} exited {rc}")
    _record(journal, steps + [step])
    return entries


def _record(journal, steps):
    """Durably record the completed step evidence without a phase change."""
    import copy
    doc = copy.deepcopy(journal.doc)
    doc["recovery_steps"] = steps
    check_recovery_journal(doc)
    pa.durable_replace(journal.host, journal.dir, pa.JOURNAL_FILE, resolver.canonical_json(doc) + b"\n", 0o600)
    journal.doc = doc


def source_exact(host, attempt):
    try:
        contract = sa.compile_activation(sa.inspect(host.root), host.architecture(), host.keyring_sha256())
    except sa.SourceError:
        return False                    # unrecognised, unauthorised or ambiguous source state
    return contract["action"] == "already_satisfied" and \
        sa.activation_digest(contract) == attempt["digests"]["source_activation_digest"]


def expected_after(details):
    """Expected post-state: the reconstructed pre-state plus the closure, installed, with its marks."""
    entries = {k: dict(v) for k, v in details["reconstructed"].items()}
    for k, p in details["closure"].items():
        entries[k] = {"package": p["package"], "architecture": p["architecture"], "version": p["version"],
                      "status": INSTALLED, "automatic": p["automatic_after"]}
    return entries


def validate_post(host, attempt, details):
    """Exact post-state, health, files and sources. Returns problems (empty if valid)."""
    problems = []
    audit_clean, check_ok = health(host)
    if not audit_clean:
        problems.append("dpkg --audit is not clean")
    if not check_ok:
        problems.append("apt-get check failed")
    entries, _holds = state_entries(host.backend(), host.architecture())
    if state_digest(entries) != state_digest(expected_after(details)):
        problems.append("package state differs from the exact expected post-state")
    if not source_exact(host, attempt):
        problems.append("source state differs from the transaction")
    if not pa.package_files_present(host, sorted({k[0] for k in details["closure"]})):
        problems.append("package files missing")
    return problems, entries


# ---------------------------------------------------------------------------
# Bounded cleanup of an abandoned attempt (attempt-local state only)

def cleanup_attempt(host, attempt_id):
    """Remove only the attempt's apply cache (validated: payload names, empty partial/, APT lock)
    and incomplete `.staging-*` directories; never journals, never unknown entries."""
    removed = []
    cache = os.path.join(host.root, pa.JOURNAL_DIR, attempt_id, pa.APPLY_CACHE)
    if os.path.lexists(cache):
        st = os.lstat(cache)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or st.st_uid != host.owner_uid:
            raise Manual("apply cache is not a root-owned directory")
        dfd = os.open(cache, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for name in os.listdir(dfd):
                est = os.stat(name, dir_fd=dfd, follow_symlinks=False)
                if name == "partial" and stat.S_ISDIR(est.st_mode):
                    os.rmdir(name, dir_fd=dfd)              # non-recursive: must be empty
                elif stat.S_ISREG(est.st_mode) and (name == "lock" or ps.FILENAME.match(name)):
                    os.unlink(name, dir_fd=dfd)
                else:
                    raise Manual(f"unexpected entry {name} in the apply cache")
            os.fsync(dfd)
        finally:
            os.close(dfd)
        os.rmdir(cache)
        removed.append("apply-cache")
    staging = os.path.join(host.root, pa.STAGING_DIR)
    if os.path.isdir(staging) and not os.path.islink(staging):
        for name in sorted(os.listdir(staging)):
            if name.startswith(".staging-"):
                ps._remove_bundle_dir(os.path.join(staging, name), allow_empty_dirs=True)
                removed.append(name)
    return removed


# ---------------------------------------------------------------------------
# The recovery operation

def _original(attempt):
    d = attempt["digests"]
    return {"attempt_id": attempt["attempt_id"], "phase": attempt["phase"], "apply_started": attempt["apply_started"],
            "transaction_digest": d["transaction_digest"], "bundle_digest": d["bundle_digest"],
            "source_activation_digest": d["source_activation_digest"],
            "repository_metadata_digest": d["repository_metadata_digest"],
            "installed_state_digest_before": d["installed_state_digest_before"]}


def _closure_record(details):
    if not details["closure"]:
        return None
    return [{"package": p["package"], "architecture": p["architecture"], "version": p["version"]}
            for _k, p in sorted(details["closure"].items())]


def _impact_record(details):
    imp = details["impact"]
    if imp["evidence"] is None:
        return None
    return {"evidence": imp["evidence"],
            "packages": [{k: r[k] for k in ("package", "architecture", "version", "pre_status", "observed_status",
                                            "activated_during", "events", "interests")}
                         for _k, r in sorted(imp["packages"].items())]}


def recover_interrupted_transaction(host):
    """Recover the interrupted LUBARCHY hardware-package transaction. Returns the
    final recovery journal (or {"result": "nothing_to_recover"})."""
    if host.euid() != 0:
        raise pa.ApplyError("not_root", "package recovery requires effective UID 0")
    lock = pa.TransactionLock(host).acquire()          # the same global lock as apply
    journal = None
    try:
        # A recovery that was itself interrupted is closed as failed; state is re-inspected.
        for name, doc in recovery_records(host):
            if doc["phase"] not in TERMINAL:
                stale = RecoveryJournal.__new__(RecoveryJournal)
                stale.host, stale.doc = host, doc
                stale.dir = os.path.join(host.root, RECOVERY_DIR, name)
                stale.transition("failed", result="failed", failure={"code": "interrupted", "phase": doc["phase"]})
        candidates = unresolved_attempts(host)
        if not candidates:
            return {"result": "nothing_to_recover"}
        pa._ensure_dir(host, pa.LUBARCHY_STATE, 0o755)
        journal = RecoveryJournal(host)
        journal.transition("locked")
        if len(candidates) > 1:
            journal.transition("manual_recovery_required", result="manual_recovery_required",
                               classification="manual_recovery_required", reason="more than one interrupted attempt")
            return journal.doc
        try:
            attempt = load_attempt(host, candidates[0])
            journal.transition("inspected", original=_original(attempt))
            classification, details = classify(host, attempt)
        except Manual as m:
            journal.transition("manual_recovery_required", result="manual_recovery_required",
                               classification="manual_recovery_required", reason=str(m))
            return journal.doc
        journal.transition("classified", classification=classification, observed=details["observed"],
                           transaction_closure=_closure_record(details), recovery_impact=_impact_record(details),
                           authorized_targets=details["authorized"])

        if classification in ("no_mutation", "no_package_change"):
            try:
                removed = cleanup_attempt(host, attempt["attempt_id"])
            except Manual as m:
                journal.transition("manual_recovery_required", result="manual_recovery_required", reason=str(m))
                return journal.doc
            journal.transition("abandoned", result="abandoned", action="finalize",
                               reason=f"no package mutation; removed {', '.join(removed) or 'nothing'}")
            return journal.doc

        if not source_exact(host, attempt):
            journal.transition("manual_recovery_required", result="manual_recovery_required",
                               classification="manual_recovery_required",
                               reason="source state differs from the transaction")
            return journal.doc
        pt.check_config(host.backend().config_text())
        journal.transition("verified", action="validate_only" if classification == "applied_but_unvalidated"
                           else "configure_exact")

        if classification == "bounded_incomplete_transaction":
            # Phase A: configure with trigger processing suppressed; Phase B: trigger processing
            # only for authorised packages that are then trigger-pending. Each phase is contained
            # and the state re-inspected before anything else runs.
            authorized = details["authorized_keys"]
            state, holds = state_entries(host.backend(), host.architecture())
            if details["configure"]:
                state = run_step(host, journal, details, "recovering", [*CONFIGURE_ARGV, *details["configure"]],
                                 state, holds)
            pending = sorted(k[0] for k, e in state.items()
                             if k[0] in authorized and e["status"] == "install ok triggers-pending")
            if pending:
                state = run_step(host, journal, details, "processing_triggers", [*TRIGGERS_ARGV, *pending],
                                 state, holds)
            left = sorted(f"{k[0]} ({e['status']})" for k, e in state.items()
                          if k[0] in authorized and e["status"] != INSTALLED)
            if left:
                journal.transition("manual_recovery_required", result="manual_recovery_required",
                                   reason=f"authorised packages still not final after bounded recovery: "
                                          f"{', '.join(left)}"[:512])
                return journal.doc
            journal.transition("recovered")

        problems, after = validate_post(host, attempt, details)
        if problems:
            raise pa.ApplyError("validation_failed", "; ".join(problems))
        journal.transition("validated", post_state={"packages": [
            {k: after[key][k] for k in ("package", "architecture", "version", "status", "automatic")}
            for key in sorted(set(details["closure"]) | set(details["impact"]["packages"]))]})
        journal.transition("complete", result="complete")
        return journal.doc
    except StopManual:
        return journal.doc
    except (pa.ApplyError, pt.TransactionError, sa.SourceError, ps.StagingError, resolver.ResolverError, OSError) as e:
        err = (pa.ApplyError("backend_failure", f"filesystem operation failed ({e.errno})") if isinstance(e, OSError)
               else e if isinstance(e, pa.ApplyError) else pa.ApplyError(e.code, getattr(e, "message", str(e))))
        if journal is not None and journal.doc["phase"] not in TERMINAL:
            try:
                journal.transition("failed", result="failed", failure={"code": err.code, "phase": journal.doc["phase"]})
            except (pa.ApplyError, OSError):
                pass        # stays at its last durable phase: the next recovery closes it and re-inspects
        raise err from None
    finally:
        lock.release()
