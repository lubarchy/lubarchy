"""LUBARCHY package-state retention and garbage collection (M1-N).

Reclaims only LUBARCHY-owned package payload state that is PROVEN obsolete.
It is not generic filesystem cleanup: it owns exactly these fixed roots and
never accepts a path, ID, age, size or selection from a caller.

    var/lib/lubarchy/package-staging/        finalized bundles (<transaction hex>) and
                                             temporary staging directories (.staging-*)
    var/lib/lubarchy/package-transactions/   apply journals (+ attempt apply caches)
    var/lib/lubarchy/package-recoveries/     recovery journals
    var/lib/lubarchy/package-gc/             GC receipts (written, never collected)

Reference graph (docs/package-garbage-collection.md): an apply journal binds
its transaction digest (= the bundle directory name) and bundle digest; its
apply cache holds hard links to that bundle's payloads; recovery journals bind
the original attempt. Every bundle is classified from every journal that
references it:

    protected_active     referenced by a nonterminal apply attempt, or any
                         recovery is nonterminal
    protected_recovery   referenced by an attempt that needs or needed recovery
                         without resolution (failed after APT started, failed or
                         manual recovery)
    protected_unknown    lifecycle not provable (unreferenced bundle, or the
                         reference graph is incomplete: an unreadable journal)
    eligible_complete    every reference is a complete, validated attempt (or one
                         resolved by a complete recovery)
    eligible_abandoned   every reference is a proven no-mutation attempt (failed
                         before APT started, or resolved by an abandoned recovery);
                         also a safe temporary staging directory
    invalid_or_ambiguous corrupt, unverifiable, unexpected objects, hard-link
                         ambiguity or ownership/mode violations

Deletion requires proof; unknown, corrupt, ambiguous and recovery-relevant
state is preserved. Journals are never deleted (current policy). Wall-clock
time and mtimes are never used. `gc` recomputes the plan under the shared
transaction lock, re-verifies each candidate immediately before deleting it
and deletes through directory descriptors without following links, comparing
every object's identity with the verified one. A durable receipt is written
before the first deletion and finalized afterwards; a crash never leaves a
success receipt.
"""

import hashlib
import os
import re
import stat

import package_apply as pa
import package_recovery as pr
import package_staging as ps
import resolver

PLAN_SCHEMA_ID = "lubarchy.packages.gc-plan/v1"
PLAN_SCHEMA_FILE = "gc-plan-v1.schema.json"
RECEIPT_SCHEMA_ID = "lubarchy.packages.gc-receipt/v1"
RECEIPT_SCHEMA_FILE = "gc-receipt-v1.schema.json"
RECEIPT_DIR = "var/lib/lubarchy/package-gc"
ROOTS = {"staging": pa.STAGING_DIR, "transactions": pa.JOURNAL_DIR, "recoveries": pr.RECOVERY_DIR,
         "receipts": RECEIPT_DIR}

BUNDLE_NAME = re.compile(r"^[0-9a-f]{64}$")
TEMP_NAME = re.compile(r"^\.staging-[a-z0-9_]{8}$")          # tempfile.mkdtemp(prefix=".staging-")
ATTEMPT_NAME = re.compile(r"^[0-9a-f]{16}$")
SAFE_NAME = re.compile(r"^[A-Za-z0-9._+-]{1,128}$")

CLASSIFICATIONS = ("protected_active", "protected_recovery", "protected_unknown", "eligible_complete",
                   "eligible_abandoned", "invalid_or_ambiguous")
ELIGIBLE = ("eligible_complete", "eligible_abandoned")
REASONS = (
    "complete_validated", "resolved_by_recovery", "resolved_without_mutation", "failed_before_apply",
    "abandoned_temporary", "nonterminal_apply", "nonterminal_recovery", "recovery_required",
    "manual_recovery_required", "recovery_failed", "unreferenced", "reference_graph_incomplete",
    "temporary_possibly_owned", "bundle_verification_failed", "binding_mismatch", "apply_cache_unexpected",
    "hard_link_ambiguity", "unsafe_object",
)
# attempt state -> (classification, reason) of the bundle it references
ATTEMPT_CLASS = {
    "complete": ("eligible_complete", "complete_validated"),
    "resolved_complete": ("eligible_complete", "resolved_by_recovery"),
    "resolved_abandoned": ("eligible_abandoned", "resolved_without_mutation"),
    "failed_before_apply": ("eligible_abandoned", "failed_before_apply"),
    "nonterminal": ("protected_active", "nonterminal_apply"),
    "recovery_required": ("protected_recovery", "recovery_required"),
    "manual_recovery_required": ("protected_recovery", "manual_recovery_required"),
    "recovery_failed": ("protected_recovery", "recovery_failed"),
}
STRENGTH = {"protected_active": 0, "protected_recovery": 1, "protected_unknown": 2, "invalid_or_ambiguous": 3,
            "eligible_complete": 4, "eligible_abandoned": 5}
CACHE_LOCK = "lock"                 # APT's own lock file in the attempt cache: never deleted
CACHE_PARTIAL = "partial"
# APT sandbox (Debian 13 / APT 3.0 default, never overridden by LUBARCHY): before acquiring, APT
# changes its archive cache's `partial/` to the sandbox user and mode 0700. This exact directory
# is the only object GC accepts with a non-root owner (owner _apt, group root, mode 0700, empty),
# and only in the cache of an attempt whose APT run started. The user is resolved from the host's
# own root-owned /etc/passwd, never guessed and never caller input.
APT_SANDBOX_USER = "_apt"
PASSWD = "etc/passwd"
PARTIAL_MODE = 0o700
SYSTEM_UID_MAX = 999


class GCError(pa.ApplyError):
    """A GC-level refusal (exit status through the apply CLI's error mapping)."""


class Unsafe(Exception):
    """One candidate is not provably safe (it is retained)."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _name(n):
    return n if SAFE_NAME.match(n) else "sha256:" + hashlib.sha256(n.encode("utf-8", "surrogateescape")).hexdigest()


# ---------------------------------------------------------------------------
# Fixed roots (inspected, never created by planning)

def _root(host, rel):
    """(path, stat) of a fixed root, or (path, None) if absent. Every component must be a
    real directory; the root itself owned by the host owner and not group/other writable."""
    cur = host.root
    for part in rel.split("/"):
        cur = os.path.join(cur, part)
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            return os.path.join(host.root, rel), None
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise GCError("unsafe_root", f"/{rel}: a path component is not a real directory")
    if st.st_uid != host.owner_uid or st.st_mode & 0o022:
        raise GCError("unsafe_root", f"/{rel} must be owned by root and not group/other writable")
    return cur, st


def _listdir(path):
    try:
        return sorted(os.listdir(path))
    except FileNotFoundError:
        return []


# ---------------------------------------------------------------------------
# Reference graph (journals are parsed and validated, never modified)

def read_graph(host):
    jroot, _ = _root(host, pa.JOURNAL_DIR)
    rroot, _ = _root(host, pr.RECOVERY_DIR)
    graph = {"complete": True, "settled": True, "attempts": {}, "recoveries": {}, "unreadable": []}
    for n in _listdir(jroot):
        doc = pr._read_json(os.path.join(jroot, n, pa.JOURNAL_FILE), pa.check_journal) \
            if ATTEMPT_NAME.match(n) else None
        if doc is None or doc["attempt_id"] != n:
            graph["complete"] = False
            graph["unreadable"].append("package-transactions/" + _name(n))
            continue
        graph["attempts"][n] = doc
    for n in _listdir(rroot):
        doc = pr._read_json(os.path.join(rroot, n, pa.JOURNAL_FILE), pr.check_recovery_journal) \
            if ATTEMPT_NAME.match(n) else None
        if doc is None or doc["recovery_id"] != n:
            graph["complete"] = False
            graph["unreadable"].append("package-recoveries/" + _name(n))
            continue
        graph["recoveries"][n] = doc
        if doc["phase"] not in pr.TERMINAL:
            graph["settled"] = False
    resolved = {}
    for n, doc in sorted(graph["recoveries"].items()):
        att = (doc.get("original") or {}).get("attempt_id")
        if att and doc["phase"] in pr.RESOLVING:
            resolved.setdefault(att, set()).add(doc["phase"])
    for n, doc in graph["attempts"].items():
        doc["_state"] = attempt_state(doc, resolved.get(n), [r for r in graph["recoveries"].values()
                                                             if (r.get("original") or {}).get("attempt_id") == n])
    return graph


def attempt_state(doc, resolution, recoveries):
    if resolution:
        return "resolved_complete" if "complete" in resolution else "resolved_abandoned"
    if doc["phase"] == "complete":
        return "complete" if doc["result"] == "complete" else "nothing_to_apply"
    phases = {r["phase"] for r in recoveries}
    if "manual_recovery_required" in phases:
        return "manual_recovery_required"
    if "failed" in phases:
        return "recovery_failed"
    if doc["phase"] not in pa.TERMINAL:
        return "nonterminal"
    return "failed_before_apply" if not doc["apply_started"] else "recovery_required"


def _journal_digest(host, rel, name):
    data = pr.sa.read_regular(os.path.join(host.root, rel, name, pa.JOURNAL_FILE), limit=1 << 20)
    return "sha256:" + hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Candidate inspection (identity of every object that would be deleted)

def _ident(st):
    """Object identity compared at deletion time: device, inode, type and, for files, size.
    Directory contents are compared by exact listings instead of their size."""
    return (st.st_dev, st.st_ino, stat.S_IFMT(st.st_mode), st.st_size if stat.S_ISREG(st.st_mode) else 0)


def _lstat(path, dir_fd=None):
    """The single metadata read of every object GC inspects (never follows links)."""
    return os.lstat(path, dir_fd=dir_fd)


def apt_sandbox_uid(host):
    """UID of the APT sandbox user from the host root's /etc/passwd, or None when it cannot
    be resolved unambiguously (missing, duplicated, aliased, not a system user, unsafe file)."""
    path = os.path.join(host.root, PASSWD)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != host.owner_uid or st.st_mode & 0o022 or st.st_size > 1 << 20:
            return None
        data = b""
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            data += chunk
    finally:
        os.close(fd)
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return None
    uids = {}
    for line in lines:
        f = line.split(":")
        if len(f) != 7 or not f[2].isdigit():
            return None
        uids.setdefault(f[0], []).append(int(f[2]))
    found = uids.get(APT_SANDBOX_USER, [])
    if len(found) != 1:
        return None
    uid = found[0]
    if not 0 < uid <= SYSTEM_UID_MAX or uid == host.owner_uid or \
            sum(1 for v in uids.values() for u in v if u == uid) != 1:
        return None
    return uid


def partial_owner(host, attempt):
    """The exact (uid, gid) an attempt cache's `partial/` must have: the APT sandbox user once
    APT ran, root while it never did; None when the sandbox user is unresolvable."""
    if not attempt["apply_started"]:
        return host.owner_uid, host.owner_gid
    uid = apt_sandbox_uid(host)
    return None if uid is None else (uid, host.owner_gid)


def _check_partial(st, dev, want):
    """The APT sandbox exception: a real directory on the same filesystem, exactly the expected
    owner and group, mode exactly 0700. Anything else is unsafe."""
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or st.st_dev != dev:
        raise Unsafe("unsafe_object")
    if want is None or (st.st_uid, st.st_gid) != want or stat.S_IMODE(st.st_mode) != PARTIAL_MODE:
        raise Unsafe("unsafe_object")
    return (st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode))


def _check_obj(st, dev, host, kind):
    if stat.S_ISLNK(st.st_mode):
        raise Unsafe("unsafe_object")
    if kind == "dir" and not stat.S_ISDIR(st.st_mode) or kind == "file" and not stat.S_ISREG(st.st_mode):
        raise Unsafe("unsafe_object")
    if st.st_dev != dev:
        raise Unsafe("unsafe_object")                     # mount crossing
    if st.st_uid != host.owner_uid or st.st_mode & 0o022:
        raise Unsafe("unsafe_object")


def inspect_bundle(host, sroot, sdev, name, refs, graph):
    """Verify an eligible bundle and every apply cache linking it. Returns the
    identity map to compare against at deletion time; raises Unsafe."""
    path = os.path.join(sroot, name)
    _check_obj(_lstat(path), sdev, host, "dir")
    try:
        manifest, _results = ps.verify_bundle(path, host.inspector(),
                                              expected={"transaction_digest": "sha256:" + name})
    except (ps.StagingError, OSError):
        raise Unsafe("bundle_verification_failed") from None
    for att in refs:
        want = graph["attempts"][att]["digests"]["bundle_digest"]
        if want is not None and want != manifest["bundle_digest"]:
            raise Unsafe("binding_mismatch")
    ident = {"bundle": _ident(_lstat(path))}
    mst = _lstat(os.path.join(path, ps.MANIFEST))
    _check_obj(mst, sdev, host, "file")
    if mst.st_nlink != 1:
        raise Unsafe("hard_link_ambiguity")
    ident["manifest"] = _ident(mst)
    ast = _lstat(os.path.join(path, ps.PAYLOAD_DIR))
    _check_obj(ast, sdev, host, "dir")
    ident["archives"] = _ident(ast)
    payloads, links = {}, {}
    for p in manifest["payloads"]:
        pst = _lstat(os.path.join(path, ps.PAYLOAD_DIR, p["filename"]))
        _check_obj(pst, sdev, host, "file")
        payloads[p["filename"]] = pst
        links[(pst.st_dev, pst.st_ino)] = 1
    ident["payloads"] = {f: _ident(s) for f, s in payloads.items()}
    jroot, jst = _root(host, pa.JOURNAL_DIR)
    caches = {}
    for att in sorted(refs):
        cpath = os.path.join(jroot, att, pa.APPLY_CACHE)
        try:
            cst = _lstat(cpath)
        except FileNotFoundError:
            continue
        _check_obj(cst, sdev, host, "dir")
        entries, partial = {}, None
        for e in _listdir(cpath):
            est = _lstat(os.path.join(cpath, e))
            if e in payloads:
                _check_obj(est, sdev, host, "file")
                if (est.st_dev, est.st_ino) != (payloads[e].st_dev, payloads[e].st_ino):
                    raise Unsafe("hard_link_ambiguity")
                links[(est.st_dev, est.st_ino)] += 1
                entries[e] = _ident(est)
            elif e == CACHE_LOCK:
                _check_obj(est, sdev, host, "file")
            elif e == CACHE_PARTIAL:
                partial = _check_partial(est, sdev, partial_owner(host, graph["attempts"][att]))
                if _listdir(os.path.join(cpath, e)):
                    raise Unsafe("apply_cache_unexpected")
                entries[e] = _ident(est)
            else:
                raise Unsafe("apply_cache_unexpected")
        caches[att] = {"dir": _ident(cst), "entries": entries, "partial": partial}
    for f, s in payloads.items():
        if s.st_nlink != links[(s.st_dev, s.st_ino)]:
            raise Unsafe("hard_link_ambiguity")              # a link outside the known attempt caches
    ident["caches"] = caches
    ident["journals"] = {att: _journal_digest(host, pa.JOURNAL_DIR, att) for att in sorted(refs)}
    nbytes = mst.st_size + sum(s.st_size for s in payloads.values())
    return ident, nbytes


def inspect_temporary(host, sroot, sdev, name):
    path = os.path.join(sroot, name)
    tst = _lstat(path)
    _check_obj(tst, sdev, host, "dir")
    ident = {"dir": _ident(tst), "files": {}, "archives": None}
    nbytes = 0
    for e in _listdir(path):
        est = _lstat(os.path.join(path, e))
        if e == ps.MANIFEST:
            _check_obj(est, sdev, host, "file")
            if est.st_nlink != 1:
                raise Unsafe("hard_link_ambiguity")
            ident["files"][e] = _ident(est)
            nbytes += est.st_size
        elif e == ps.PAYLOAD_DIR:
            _check_obj(est, sdev, host, "dir")
            ident["archives"] = {"dir": _ident(est), "files": {}}
            for f in _listdir(os.path.join(path, e)):
                fst = _lstat(os.path.join(path, e, f))
                _check_obj(fst, sdev, host, "file")
                if fst.st_nlink != 1:
                    raise Unsafe("hard_link_ambiguity")
                ident["archives"]["files"][f] = _ident(fst)
                nbytes += fst.st_size
        else:
            raise Unsafe("unsafe_object")
    return ident, nbytes


# ---------------------------------------------------------------------------
# Plan (read-only)

def compute_plan(host):
    """(plan document, private identities). Read-only."""
    graph = read_graph(host)
    sroot, sst = _root(host, pa.STAGING_DIR)
    by_tx = {}
    for att, doc in graph["attempts"].items():
        tx = doc["digests"]["transaction_digest"]
        if tx and doc["_state"] != "nothing_to_apply":
            by_tx.setdefault(tx[7:], []).append(att)
    may_own_temporary = any(doc["_state"] == "nonterminal" and doc["phase"] in (
        "prepared", "locked", "source_activated", "metadata_refreshed", "transaction_revalidated")
        for doc in graph["attempts"].values())
    candidates, unrecognized, private = [], [], {}
    for n in _listdir(sroot) if sst is not None else []:
        lst = os.lstat(os.path.join(sroot, n))
        kind = "bundle" if BUNDLE_NAME.match(n) else "temporary" if TEMP_NAME.match(n) else None
        if kind is None:
            unrecognized.append({"name": _name(n), "type": "symlink" if stat.S_ISLNK(lst.st_mode) else
                                 "directory" if stat.S_ISDIR(lst.st_mode) else "file" if stat.S_ISREG(lst.st_mode)
                                 else "other"})
            continue
        refs = sorted(by_tx.get(n, [])) if kind == "bundle" else []
        entry = {"kind": kind, "name": n, "referenced_by": refs, "bytes": 0}
        if not graph["complete"]:
            cls, reason = "protected_unknown", "reference_graph_incomplete"
        elif not graph["settled"]:
            cls, reason = "protected_active", "nonterminal_recovery"
        elif kind == "temporary":
            cls, reason = ("protected_active", "temporary_possibly_owned") if may_own_temporary else \
                ("eligible_abandoned", "abandoned_temporary")
        elif not refs:
            cls, reason = "protected_unknown", "unreferenced"
        else:
            cls, reason = min((ATTEMPT_CLASS[graph["attempts"][a]["_state"]] for a in refs),
                              key=lambda x: STRENGTH[x[0]])
        if cls in ELIGIBLE:
            try:
                if kind == "bundle":
                    ident, nbytes = inspect_bundle(host, sroot, sst.st_dev, n, refs, graph)
                else:
                    ident, nbytes = inspect_temporary(host, sroot, sst.st_dev, n)
                entry["bytes"] = nbytes
                private[n] = ident
            except Unsafe as u:
                cls, reason = "invalid_or_ambiguous", u.reason
            except OSError:
                cls, reason = "invalid_or_ambiguous", "unsafe_object"
        entry.update(classification=cls, reason=reason,
                     apply_caches=sorted(private.get(n, {}).get("caches", {})) if kind == "bundle" else [])
        candidates.append(entry)
    summary = {c: sum(1 for x in candidates if x["classification"] == c) for c in CLASSIFICATIONS}
    summary["eligible_bytes"] = sum(x["bytes"] for x in candidates if x["classification"] in ELIGIBLE)
    summary["unrecognized"] = len(unrecognized)
    plan = {
        "schema": PLAN_SCHEMA_ID,
        "roots": {k: "/" + v for k, v in ROOTS.items()},
        "graph": {
            "complete": graph["complete"], "settled": graph["settled"],
            "unreadable": sorted(graph["unreadable"]),
            "attempts": [{"attempt_id": a, "state": d["_state"], "phase": d["phase"],
                          "transaction_digest": d["digests"]["transaction_digest"],
                          "bundle_digest": d["digests"]["bundle_digest"],
                          "journal_digest": _journal_digest(host, pa.JOURNAL_DIR, a)}
                         for a, d in sorted(graph["attempts"].items())],
            "recoveries": [{"recovery_id": r, "phase": d["phase"],
                            "attempt_id": (d.get("original") or {}).get("attempt_id"),
                            "journal_digest": _journal_digest(host, pr.RECOVERY_DIR, r)}
                           for r, d in sorted(graph["recoveries"].items())],
        },
        "candidates": candidates,
        "unrecognized": sorted(unrecognized, key=lambda x: x["name"]),
        "summary": summary,
    }
    check_document(plan, PLAN_SCHEMA_FILE)
    return plan, private


def plan(host):
    """Read-only plan under the shared lock (a concurrent apply/recovery/GC is refused)."""
    if host.euid() != 0:
        raise pa.ApplyError("not_root", "package GC planning requires effective UID 0 (journals are root-only)")
    lock = pa.TransactionLock(host).acquire()
    try:
        return compute_plan(host)[0]
    finally:
        lock.release()


def check_document(doc, schema_file):
    import json
    import jsonschema
    with open(os.path.join(resolver.SCHEMA_DIR, schema_file), encoding="utf-8") as f:
        schema = json.load(f)
    if any(True for _ in jsonschema.Draft202012Validator(schema).iter_errors(doc)):
        raise GCError("contract_failure", f"document does not satisfy {schema_file}")
    resolver.canonical_json(doc)
    return doc


# ---------------------------------------------------------------------------
# Deletion (identity-checked, through directory descriptors, never following links)

def _open_at(dfd, name, want):
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dfd)
    if _ident(os.fstat(fd)) != want:
        os.close(fd)
        raise Unsafe("unsafe_object")
    return fd


def _expect_entry(dfd, name, want):
    st = _lstat(name, dir_fd=dfd)
    if _ident(st) != want:
        raise Unsafe("unsafe_object")
    return st


def _expect_partial(cfd, ident, meta):
    """Immediately before its rmdir: the same directory, still exactly the verified owner,
    group and mode (re-read, not remembered), and still empty."""
    st = _expect_entry(cfd, CACHE_PARTIAL, ident)
    if meta is None or (st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)) != meta:
        raise Unsafe("unsafe_object")
    pfd = _open_at(cfd, CACHE_PARTIAL, ident)
    try:
        if os.listdir(pfd):
            raise Unsafe("apply_cache_unexpected")
    finally:
        os.close(pfd)


def _exact_listing(dfd, names):
    if sorted(os.listdir(dfd)) != sorted(names):
        raise Unsafe("unsafe_object")


def delete_bundle(host, name, ident):
    """Delete one verified bundle and the payload links of its attempt caches. Every object's
    identity must equal the verified one; any difference aborts before (or during) removal."""
    sroot, _ = _root(host, pa.STAGING_DIR)
    jroot, _ = _root(host, pa.JOURNAL_DIR)
    for att, digest in ident["journals"].items():
        if _journal_digest(host, pa.JOURNAL_DIR, att) != digest:
            raise Unsafe("binding_mismatch")                 # a referencing journal changed since planning
    rfd = os.open(sroot, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    jfd = os.open(jroot, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    opened = []
    try:
        bfd = _open_at(rfd, name, ident["bundle"])
        opened.append(bfd)
        afd = _open_at(bfd, ps.PAYLOAD_DIR, ident["archives"])
        opened.append(afd)
        _exact_listing(bfd, [ps.MANIFEST, ps.PAYLOAD_DIR])
        _exact_listing(afd, list(ident["payloads"]))
        _expect_entry(bfd, ps.MANIFEST, ident["manifest"])
        for f, want in ident["payloads"].items():
            _expect_entry(afd, f, want)
        cfds = {}
        for att, c in ident["caches"].items():
            adir = os.open(att, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=jfd)
            opened.append(adir)
            cfd = _open_at(adir, pa.APPLY_CACHE, c["dir"])
            opened.append(cfd)
            present = sorted(os.listdir(cfd))
            if sorted(set(present) - {CACHE_LOCK}) != sorted(c["entries"]):
                raise Unsafe("apply_cache_unexpected")
            for e, want in c["entries"].items():
                if e == CACHE_PARTIAL:
                    _expect_partial(cfd, want, c["partial"])
                else:
                    _expect_entry(cfd, e, want)
            cfds[att] = cfd
        # every identity re-proven: remove payload links from the caches, then the bundle
        for att, cfd in cfds.items():
            for e in sorted(ident["caches"][att]["entries"]):
                if e == CACHE_PARTIAL:
                    os.rmdir(e, dir_fd=cfd)                  # non-recursive: must be empty
                else:
                    os.unlink(e, dir_fd=cfd)
            os.fsync(cfd)
        for f in sorted(ident["payloads"]):
            os.unlink(f, dir_fd=afd)
        os.fsync(afd)
        os.rmdir(ps.PAYLOAD_DIR, dir_fd=bfd)
        os.unlink(ps.MANIFEST, dir_fd=bfd)
        os.rmdir(name, dir_fd=rfd)
        os.fsync(rfd)
    finally:
        for fd in reversed(opened):
            os.close(fd)
        os.close(rfd)
        os.close(jfd)


def delete_temporary(host, name, ident):
    sroot, _ = _root(host, pa.STAGING_DIR)
    rfd = os.open(sroot, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    opened = []
    try:
        tfd = _open_at(rfd, name, ident["dir"])
        opened.append(tfd)
        _exact_listing(tfd, list(ident["files"]) + ([ps.PAYLOAD_DIR] if ident["archives"] else []))
        for f, want in ident["files"].items():
            _expect_entry(tfd, f, want)
        afd = None
        if ident["archives"]:
            afd = _open_at(tfd, ps.PAYLOAD_DIR, ident["archives"]["dir"])
            opened.append(afd)
            _exact_listing(afd, list(ident["archives"]["files"]))
            for f, want in ident["archives"]["files"].items():
                _expect_entry(afd, f, want)
            for f in sorted(ident["archives"]["files"]):
                os.unlink(f, dir_fd=afd)
            os.rmdir(ps.PAYLOAD_DIR, dir_fd=tfd)
        for f in sorted(ident["files"]):
            os.unlink(f, dir_fd=tfd)
        os.rmdir(name, dir_fd=rfd)
        os.fsync(rfd)
    finally:
        for fd in reversed(opened):
            os.close(fd)
        os.close(rfd)


# ---------------------------------------------------------------------------
# Receipt and the GC operation

class Receipt:
    def __init__(self, host, plan_doc):
        self.host = host
        self.dir = pa._ensure_dir(host, RECEIPT_DIR, 0o700)
        self.id = host.attempt_id()
        if not ATTEMPT_NAME.match(self.id):
            raise GCError("contract_failure", "receipt identifier is malformed")
        retained = [{"kind": c["kind"], "name": c["name"], "classification": c["classification"],
                     "reason": c["reason"]} for c in plan_doc["candidates"] if c["classification"] not in ELIGIBLE]
        self.doc = {"schema": RECEIPT_SCHEMA_ID, "receipt_id": self.id, "plan_digest": resolver.digest(plan_doc),
                    "phase": "deleting", "result": None,
                    "eligible": [{"kind": c["kind"], "name": c["name"], "classification": c["classification"],
                                  "bytes": c["bytes"]} for c in plan_doc["candidates"]
                                 if c["classification"] in ELIGIBLE],
                    "removed": [], "failed": [], "not_attempted": [], "bytes_reclaimed": 0,
                    "retained": retained, "protected_count": len(retained),
                    "unrecognized_count": plan_doc["summary"]["unrecognized"]}
        self.write()

    def write(self):
        check_document(self.doc, RECEIPT_SCHEMA_FILE)
        pa.durable_replace(self.host, self.dir, f"{self.id}.json", resolver.canonical_json(self.doc) + b"\n", 0o600)


def collect_garbage(host):
    """Delete only provably obsolete package payload state. Returns the final receipt,
    or {"result": "nothing_to_collect", ...} when nothing is eligible (nothing written)."""
    if host.euid() != 0:
        raise pa.ApplyError("not_root", "package GC requires effective UID 0")
    lock = pa.TransactionLock(host).acquire()          # the same lock as apply and recovery
    try:
        plan_doc, private = compute_plan(host)        # recomputed under the lock; never caller input
        eligible = [c for c in plan_doc["candidates"] if c["classification"] in ELIGIBLE]
        if not eligible:
            return {"result": "nothing_to_collect", "plan_digest": resolver.digest(plan_doc),
                    "protected_count": len(plan_doc["candidates"]),
                    "unrecognized_count": plan_doc["summary"]["unrecognized"]}
        receipt = Receipt(host, plan_doc)           # durable BEFORE the first deletion (fails closed)
        for i, c in enumerate(eligible):
            try:
                if c["kind"] == "bundle":
                    delete_bundle(host, c["name"], private[c["name"]])
                else:
                    delete_temporary(host, c["name"], private[c["name"]])
            except (Unsafe, OSError) as e:
                reason = e.reason if isinstance(e, Unsafe) else "unsafe_object"
                receipt.doc["failed"].append({"kind": c["kind"], "name": c["name"], "reason": reason})
                receipt.doc["not_attempted"] = [x["name"] for x in eligible[i + 1:]]
                break                                   # no further deletion; next run re-inspects
            receipt.doc["removed"].append({"kind": c["kind"], "name": c["name"], "bytes": c["bytes"]})
            receipt.doc["bytes_reclaimed"] += c["bytes"]
            receipt.write()
        if receipt.doc["failed"]:
            receipt.doc.update(phase="partial" if receipt.doc["removed"] else "failed",
                               result="partial" if receipt.doc["removed"] else "failed")
        else:
            receipt.doc.update(phase="complete", result="complete")
        receipt.write()
        return receipt.doc
    finally:
        lock.release()
