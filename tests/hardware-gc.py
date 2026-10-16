#!/usr/bin/python3
"""LUBARCHY package-state garbage collection gate (M1-N; synthetic, non-mutating).

Drives hardware/package_gc.py, `apply-packages.py gc-plan|gc` and the
trusted-deployment launcher against package state created by the REAL apply
executor on the gate-11 fake host (a temporary root): a complete attempt with
its bundle and hard-linked apply cache, plus variants (journal phases, cloned
and corrupted bundles, recovery journals, temporary staging directories and
hostile filesystem objects). It never touches the runner's /var/lib, APT or
dpkg state, needs no root and writes only inside its temporary directory.
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
import tempfile  # noqa: E402

TESTS = pathlib.Path(__file__).resolve().parent
REPO = TESTS.parent
HARDWARE = REPO / "hardware"
sys.path.insert(0, str(HARDWARE))
import package_apply as pa  # noqa: E402
import package_gc as gc  # noqa: E402
import package_recovery as pr  # noqa: E402
import package_staging as ps  # noqa: E402
import resolver  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate11 = _load("gate11", TESTS / "hardware-apply.py")
gate10 = gate11.gate10
sys.path.remove(str(HARDWARE))

PLAN_SCHEMA_SHA256 = "14b69baafe39a926687655601491e2b46c2e9c1ec1e4e0e402b24109e0b243af"
RECEIPT_SCHEMA_SHA256 = "39864e1b93ea67beff9782e3e32a31064bab092d0ddfbb26598a9c734d4bb421"


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
    except pa.ApplyError as e:
        expect(e.code == code, f"expected {code}, got {e.code}: {e.message}")
        return e
    raise Failure(f"expected {code}, but it was accepted")


# ---------------------------------------------------------------------------
# APT sandbox model. Real APT (Debian 13, APT 3.0) changes the attempt cache's `partial/` to its
# sandbox user `_apt`, group root, mode 0700, before acquiring (measured on the builder). An
# unprivileged gate cannot chown, so the observed ownership of exactly those inodes is overlaid on
# GC's single metadata read (gc._lstat). The overlay is keyed by (device, inode): a replaced
# directory loses it, exactly as a real replacement would lose APT's ownership.

REAL_LSTAT = gc._lstat
FAKE_APT_UID = 42 if os.geteuid() != 42 else 43
OVERLAY = {}          # (st_dev, st_ino) of a live synthetic object -> observed (uid, gid, mode)
INODE_ALIAS = {}      # (st_dev, st_ino) of a live object -> inode number it is reported with (forced reuse)
REAL_RMDIR, REAL_UNLINK, REAL_REMOVE, REAL_RENAME, REAL_REPLACE = os.rmdir, os.unlink, os.remove, os.rename, \
    os.replace


def overlay_lstat(path, dir_fd=None):
    st = REAL_LSTAT(path, dir_fd=dir_fd)
    key = (st.st_dev, st.st_ino)
    o, alias = OVERLAY.get(key), INODE_ALIAS.get(key)
    if o is None and alias is None:
        return st
    t = list(st)
    if o is not None:
        t[0], t[4], t[5] = o[2], o[0], o[1]
    if alias is not None:
        t[1] = alias
    return os.stat_result(t)


# Object lifetime: an overlay describes one synthetic filesystem OBJECT, not an inode number. It
# ends exactly when that object's last link is destroyed -- by the gate or by GC itself -- so a
# later object that the filesystem allocates with the same (device, inode) never inherits it.
# (A numeric inode is routinely reused immediately after rmdir/unlink on some filesystems.)

def _ending(path, dir_fd):
    try:
        st = os.lstat(path, dir_fd=dir_fd)
    except OSError:
        return None
    return (st.st_dev, st.st_ino) if stat.S_ISDIR(st.st_mode) or st.st_nlink <= 1 else None


def _forget(key):
    if key is not None:
        OVERLAY.pop(key, None)
        INODE_ALIAS.pop(key, None)


def tracked_rmdir(path, *, dir_fd=None):
    key = _ending(path, dir_fd)
    REAL_RMDIR(path, dir_fd=dir_fd)
    _forget(key)


def tracked_unlink(path, *, dir_fd=None):
    key = _ending(path, dir_fd)
    REAL_UNLINK(path, dir_fd=dir_fd)
    _forget(key)


def tracked_rename(real):
    def rename(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
        key = _ending(dst, dst_dir_fd)
        try:
            same = os.lstat(src, dir_fd=src_dir_fd)[1:3] == os.lstat(dst, dir_fd=dst_dir_fd)[1:3]
        except OSError:
            same = False
        real(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
        if not same:
            _forget(key)                       # the replaced destination object no longer exists
    return rename


TRACKED = {"rmdir": tracked_rmdir, "unlink": tracked_unlink, "remove": tracked_unlink,
           "rename": tracked_rename(REAL_RENAME), "replace": tracked_rename(REAL_REPLACE)}
REAL_OS = {"rmdir": REAL_RMDIR, "unlink": REAL_UNLINK, "remove": REAL_REMOVE, "rename": REAL_RENAME,
           "replace": REAL_REPLACE}


def replace_partial(p, reuse_identity=False):
    """Destroy partial/ and create a new root-owned 0700 directory in its place. With
    reuse_identity the new object is reported with the destroyed object's inode number (its
    device, inode and type then equal the verified identity), deterministically modelling a
    filesystem that reuses the inode immediately."""
    old = os.lstat(p)
    os.rmdir(p)
    expect((old.st_dev, old.st_ino) not in OVERLAY, "overlay outlived its object")
    os.mkdir(p, 0o700)
    new = os.lstat(p)
    if reuse_identity:
        INODE_ALIAS[(new.st_dev, new.st_ino)] = old.st_ino
    seen = gc._lstat(p)
    expect(seen.st_uid == new.st_uid == os.geteuid() and seen.st_gid == new.st_gid and
           stat.S_IMODE(seen.st_mode) == 0o700, "replacement partial/ is not observed root-owned 0700")
    if reuse_identity:
        expect((seen.st_dev, seen.st_ino, stat.S_IFMT(seen.st_mode)) ==
               (old.st_dev, old.st_ino, stat.S_IFMT(old.st_mode)), "forced identity reuse not modelled")


def own(path, uid, gid, perm):
    """Overlay an observed owner/group/permission on one existing object (type bits kept)."""
    st = os.lstat(path)
    OVERLAY[(st.st_dev, st.st_ino)] = (uid, gid, stat.S_IFMT(st.st_mode) | perm)


def write_passwd(host, lines=None):
    if lines is None:
        lines = ["root:x:0:0:root:/root:/bin/bash", f"_apt:x:{FAKE_APT_UID}:65534::/nonexistent:/usr/sbin/nologin",
                 "builder:x:1000:1000:Builder,,,:/home/builder:/bin/bash"]
    path = os.path.join(host.root, gc.PASSWD)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    pathlib.Path(path).write_text("".join(f"{x}\n" for x in lines))
    os.chmod(path, 0o644)


def apt_ran(host, attempt):
    """What APT does to the attempt cache it ran with: partial/ becomes _apt:root, 0700."""
    own(os.path.join(host.root, pa.JOURNAL_DIR, attempt, pa.APPLY_CACHE, gc.CACHE_PARTIAL), FAKE_APT_UID,
        host.owner_gid, 0o700)


# ---------------------------------------------------------------------------
# Synthetic package state built by the real executor, then varied

class State:
    """A fake-host root with one REAL complete apply attempt; helpers add variants."""

    def __init__(self, name):
        self.h = gate11.FakeHost("gc-" + name)
        write_passwd(self.h)
        doc = pa.apply_current_plan(self.h)
        expect(doc["result"] == "complete", "base apply did not complete")
        apt_ran(self.h, doc["attempt_id"])
        self.template = doc
        self.base_attempt = doc["attempt_id"]
        self.base_bundle = doc["digests"]["transaction_digest"][7:]

    def path(self, *parts):
        return os.path.join(self.h.root, *parts)

    def staging(self, *parts):
        return self.path(pa.STAGING_DIR, *parts)

    def journal_dir(self, att):
        return self.path(pa.JOURNAL_DIR, att)

    def clone_bundle(self):
        """A second valid bundle (distinct transaction digest, same verified payloads)."""
        src = self.staging(self.base_bundle)
        m = ps.read_manifest(src)
        m["transaction_digest"] = "sha256:" + hashlib.sha256(os.urandom(16)).hexdigest()
        m["bundle_digest"] = ps.compute_bundle_digest(m)
        dst = self.staging(m["transaction_digest"][7:])
        os.mkdir(dst, 0o755)
        os.mkdir(os.path.join(dst, ps.PAYLOAD_DIR), 0o755)
        for p in m["payloads"]:
            shutil.copyfile(os.path.join(src, ps.PAYLOAD_DIR, p["filename"]),
                            os.path.join(dst, ps.PAYLOAD_DIR, p["filename"]))
            os.chmod(os.path.join(dst, ps.PAYLOAD_DIR, p["filename"]), 0o644)
        pathlib.Path(dst, ps.MANIFEST).write_bytes(ps.manifest_bytes(m))
        os.chmod(os.path.join(dst, ps.MANIFEST), 0o644)
        return m["transaction_digest"][7:]

    def add_attempt(self, state, bundle, cache=True):
        """An apply journal in `state` referencing `bundle` (schema-valid, as the executor writes it)."""
        att = self.h.attempt_id()
        d = copy.deepcopy(self.template)
        m = ps.read_manifest(self.staging(bundle)) if os.path.isdir(self.staging(bundle)) else None
        d["attempt_id"] = att
        d["digests"]["transaction_digest"] = "sha256:" + bundle
        d["digests"]["bundle_digest"] = m["bundle_digest"] if m else None
        order = [p["phase"] for p in self.template["phases"]]
        stop = {"complete": "complete", "applying": "applying", "applied": "applied",
                "bundle_verified": "bundle_verified", "transaction_revalidated": "transaction_revalidated",
                "failed_after": "applying", "failed_before": "bundle_verified"}[state]
        d["phases"] = self.template["phases"][:order.index(stop) + 1]
        d["phase"] = stop
        d["apply_started"] = order.index(stop) >= order.index("applying")
        if state != "complete":
            d["result"], d["post_state"] = None, None
        if state == "transaction_revalidated":
            d["digests"]["bundle_digest"] = None
            d["recovery_evidence"] = None
        if state in ("failed_after", "failed_before"):
            d["phases"] = d["phases"] + [{"phase": "failed", "at": "2026-10-14T00:00:59Z"}]
            d.update(phase="failed", result="failed", failure={"code": "apply_failed", "phase": stop})
        pa.check_journal(d)
        os.mkdir(self.journal_dir(att), 0o700)
        pathlib.Path(self.journal_dir(att), pa.JOURNAL_FILE).write_bytes(resolver.canonical_json(d) + b"\n")
        os.chmod(os.path.join(self.journal_dir(att), pa.JOURNAL_FILE), 0o600)
        if cache and m is not None:
            c = os.path.join(self.journal_dir(att), pa.APPLY_CACHE)
            os.mkdir(c, 0o700)
            for p in m["payloads"]:
                os.link(self.staging(bundle, ps.PAYLOAD_DIR, p["filename"]), os.path.join(c, p["filename"]))
            os.mkdir(os.path.join(c, "partial"), 0o700)
            pathlib.Path(c, "lock").write_text("")
            os.chmod(os.path.join(c, "lock"), 0o640)
            if d["apply_started"]:
                apt_ran(self.h, att)
        return att

    def add_recovery(self, attempt, outcome):
        """A schema-valid recovery journal for `attempt` ending in `outcome`."""
        doc = json.loads(pathlib.Path(self.journal_dir(attempt), pa.JOURNAL_FILE).read_text())
        pa._ensure_dir(self.h, pa.LUBARCHY_STATE, 0o755)
        j = pr.RecoveryJournal(self.h)
        j.transition("locked")
        if outcome == "nonterminal":
            return j.id
        j.transition("inspected", original=pr._original(doc))
        if outcome == "manual":
            j.transition("manual_recovery_required", result="manual_recovery_required",
                         classification="manual_recovery_required", reason="synthetic")
        elif outcome == "failed":
            j.transition("failed", result="failed", failure={"code": "recovery_failed", "phase": "inspected"})
        elif outcome == "abandoned":
            j.transition("classified", classification="no_mutation")
            j.transition("abandoned", result="abandoned", action="finalize", reason="synthetic")
        elif outcome == "complete":
            j.transition("classified", classification="applied_but_unvalidated")
            j.transition("verified", action="validate_only")
            j.transition("validated", post_state={"packages": []})
            j.transition("complete", result="complete")
        return j.id

    def add_temporary(self, files=True):
        t = tempfile.mkdtemp(prefix=".staging-", dir=self.staging())
        if files:
            os.mkdir(os.path.join(t, ps.PAYLOAD_DIR), 0o700)
            pathlib.Path(t, ps.PAYLOAD_DIR, "x_1_all.deb").write_bytes(b"partial download")
        return os.path.basename(t)


def swap(path, make):
    """Replace one file by another object at the same path (path computed once)."""
    os.unlink(path)
    make(path)


def plan(st):
    return gc.compute_plan(st.h)[0]


def cand(p, name):
    return next(c for c in p["candidates"] if c["name"] == name)


def cls(st, name):
    c = cand(plan(st), name)
    return c["classification"], c["reason"]


def tree(root):
    out = {}
    for d, dirs, files in os.walk(root):
        for n in dirs + files:
            p = os.path.join(d, n)
            s = os.lstat(p)
            data = None
            if os.path.isfile(p) and not os.path.islink(p):
                data = hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()
            out[os.path.relpath(p, root)] = (s.st_mode, s.st_uid, data, os.readlink(p) if os.path.islink(p) else None)
    return out


# ---------------------------------------------------------------------------
# Contracts

@check
def schemas_are_strict_meta_valid_and_protected():                               # 48
    import jsonschema
    for name, want in ((gc.PLAN_SCHEMA_FILE, PLAN_SCHEMA_SHA256), (gc.RECEIPT_SCHEMA_FILE, RECEIPT_SCHEMA_SHA256)):
        schema = json.loads((HARDWARE / "schemas" / name).read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)

        def closed(node, path):
            if isinstance(node, dict):
                if node.get("type") == "object" and "properties" in node:
                    expect(node.get("additionalProperties") is False, f"{name}: open object at {path}")
                for k, v in node.items():
                    closed(v, f"{path}/{k}")
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    closed(v, f"{path}/{i}")
        closed(schema, "")
        sha = hashlib.sha256((HARDWARE / "schemas" / name).read_bytes()).hexdigest()
        expect(sha == want, f"{name} changed ({sha})")
        expect(gate10.PROTECTED_SCHEMAS.get(name) == sha, f"{name} not in the gate-10 protected baseline")
    plan_schema = json.loads((HARDWARE / "schemas" / gc.PLAN_SCHEMA_FILE).read_text())
    expect(set(plan_schema["$defs"]["reason"]["enum"]) == set(gc.REASONS) and
           set(plan_schema["$defs"]["classification"]["enum"]) == set(gc.CLASSIFICATIONS), "schema/module codes")
    expect(gc.ROOTS == {"staging": "var/lib/lubarchy/package-staging",
                        "transactions": "var/lib/lubarchy/package-transactions",
                        "recoveries": "var/lib/lubarchy/package-recoveries", "receipts": "var/lib/lubarchy/package-gc"},
           "fixed production roots changed")
    return "gc-plan v1 and gc-receipt v1 meta-valid, closed, protected; codes equal; four fixed roots"


# ---------------------------------------------------------------------------
# Classification (retention graph)

@check
def lifecycle_classification():                                                  # 1-10, 49
    st = State("classify")
    b = st.base_bundle
    expect(cls(st, b) == ("eligible_complete", "complete_validated"), f"complete bundle {cls(st, b)}")
    p1 = plan(st)
    expect(resolver.canonical_json(p1) == resolver.canonical_json(plan(st)), "plan not deterministic")
    expect(cand(p1, b)["apply_caches"] == [st.base_attempt] and cand(p1, b)["bytes"] > 0, "cache/bytes")
    cases = {}
    for state, want in (("applying", ("protected_active", "nonterminal_apply")),
                        ("applied", ("protected_active", "nonterminal_apply")),
                        ("bundle_verified", ("protected_active", "nonterminal_apply")),
                        ("failed_after", ("protected_recovery", "recovery_required")),
                        ("failed_before", ("eligible_abandoned", "failed_before_apply"))):
        s = State("st-" + state)
        x = s.clone_bundle()
        s.add_attempt(state, x)
        expect(cls(s, x) == want, f"{state}: {cls(s, x)}")
        cases[state] = want
    # recovery outcomes on an attempt that failed after APT started
    for outcome, want in (("manual", ("protected_recovery", "manual_recovery_required")),
                          ("failed", ("protected_recovery", "recovery_failed")),
                          ("complete", ("eligible_complete", "resolved_by_recovery")),
                          ("abandoned", ("eligible_abandoned", "resolved_without_mutation"))):
        s = State("rec-" + outcome)
        x = s.clone_bundle()
        att = s.add_attempt("failed_after", x)
        s.add_recovery(att, outcome)
        expect(cls(s, x) == want, f"recovery {outcome}: {cls(s, x)}")
        cases["recovery_" + outcome] = want
    # a nonterminal recovery anywhere: nothing is settled
    s = State("rec-nonterminal")
    att = s.add_attempt("failed_after", s.clone_bundle())
    s.add_recovery(att, "nonterminal")
    expect(cls(s, s.base_bundle) == ("protected_active", "nonterminal_recovery"), "nonterminal recovery")
    # the strongest protection of all references wins
    s = State("mixed")
    s.add_attempt("applying", s.base_bundle, cache=False)
    expect(cls(s, s.base_bundle) == ("protected_active", "nonterminal_apply"), "mixed references")
    # unknown / missing / corrupt journal state
    s = State("unref")
    x = s.clone_bundle()
    expect(cls(s, x) == ("protected_unknown", "unreferenced"), "unreferenced bundle")
    s = State("corrupt-journal")
    pathlib.Path(s.journal_dir(s.base_attempt), pa.JOURNAL_FILE).write_text("{not json")
    p = plan(s)
    expect(cand(p, s.base_bundle)["classification"] == "protected_unknown" and not p["graph"]["complete"] and
           p["graph"]["unreadable"] == [f"package-transactions/{s.base_attempt}"], "corrupt journal")
    s = State("missing-journal")
    os.unlink(os.path.join(s.journal_dir(s.base_attempt), pa.JOURNAL_FILE))
    expect(cls(s, s.base_bundle) == ("protected_unknown", "reference_graph_incomplete"), "missing journal")
    s = State("odd-journal-entry")
    os.mkdir(s.path(pa.JOURNAL_DIR, "not-an-attempt"))
    expect(cls(s, s.base_bundle) == ("protected_unknown", "reference_graph_incomplete"), "unknown journal entry")
    s = State("bad-recovery")
    os.makedirs(s.path(pr.RECOVERY_DIR, "a" * 16))
    pathlib.Path(s.path(pr.RECOVERY_DIR, "a" * 16, "journal.json")).write_text("[]")
    expect(cls(s, s.base_bundle) == ("protected_unknown", "reference_graph_incomplete"), "corrupt recovery journal")
    return f"complete eligible; {len(cases)} attempt/recovery states; nonterminal recovery, mixed references, " \
           "unreferenced, corrupt/missing/unknown journals protected; plan deterministic"


@check
def integrity_failures_are_retained():                                           # 11-20, 22, 23
    n = 0

    def retained(label, mutate, reason):
        nonlocal n
        s = State("int-" + label)
        mutate(s, s.staging(s.base_bundle))
        got = cls(s, s.base_bundle)
        expect(got == ("invalid_or_ambiguous", reason), f"{label}: {got}")
        n += 1
        return s
    payload = lambda s, b: os.path.join(b, ps.PAYLOAD_DIR, sorted(os.listdir(os.path.join(b, ps.PAYLOAD_DIR)))[0])  # noqa: E731
    retained("corrupt-payload", lambda s, b: open(payload(s, b), "r+b").write(b"\xff"), "bundle_verification_failed")
    retained("missing-payload", lambda s, b: os.unlink(payload(s, b)), "bundle_verification_failed")
    retained("extra-payload", lambda s, b: pathlib.Path(b, ps.PAYLOAD_DIR, "x_1_all.deb").write_bytes(b"x"),
             "bundle_verification_failed")
    retained("extra-entry", lambda s, b: pathlib.Path(b, "notes").write_text("x"), "bundle_verification_failed")

    def manifest_edit(s, b):
        m = json.loads(pathlib.Path(b, ps.MANIFEST).read_text())
        m["architecture"] = "i386"
        pathlib.Path(b, ps.MANIFEST).write_bytes(ps.manifest_bytes(m))
    retained("manifest-mismatch", manifest_edit, "bundle_verification_failed")

    def journal_bundle_digest(s, b):
        p = pathlib.Path(s.journal_dir(s.base_attempt), pa.JOURNAL_FILE)
        d = json.loads(p.read_text())
        d["digests"]["bundle_digest"] = "sha256:" + "4" * 64
        p.write_bytes(resolver.canonical_json(d) + b"\n")
    retained("digest-mismatch", journal_bundle_digest, "binding_mismatch")

    def renamed(s, b):
        x = s.clone_bundle()
        shutil.rmtree(b)
        os.rename(s.staging(x), b)                     # a valid bundle under another transaction's name
    retained("name-digest-mismatch", renamed, "bundle_verification_failed")
    retained("symlink-payload", lambda s, b: swap(payload(s, b), lambda p: os.symlink("/etc/passwd", p)),
             "bundle_verification_failed")
    retained("fifo-payload", lambda s, b: swap(payload(s, b), os.mkfifo), "bundle_verification_failed")
    retained("group-writable", lambda s, b: os.chmod(os.path.join(b, ps.PAYLOAD_DIR), 0o775),
             "bundle_verification_failed")
    retained("hard-link-outside", lambda s, b: os.link(payload(s, b), s.path("var/lib/lubarchy/stray.deb")),
             "hard_link_ambiguity")
    retained("cache-extra", lambda s, b: pathlib.Path(s.journal_dir(s.base_attempt), pa.APPLY_CACHE, "x").write_text(""),
             "apply_cache_unexpected")
    retained("cache-partial-not-empty", lambda s, b: pathlib.Path(s.journal_dir(s.base_attempt), pa.APPLY_CACHE,
                                                                  "partial", "x").write_text(""),
             "apply_cache_unexpected")
    def cache_copy(s, b):
        src = payload(s, b)
        swap(os.path.join(s.journal_dir(s.base_attempt), pa.APPLY_CACHE, os.path.basename(src)),
             lambda p: shutil.copyfile(src, p))
    retained("cache-foreign-file", cache_copy, "hard_link_ambiguity")

    # wrong owner: simulated by a host that expects a different owner uid
    s = State("int-owner")
    s.h.owner_uid = os.geteuid() + 1
    rejects("unsafe_root", plan, s)
    # symlinked bundle, symlinked root and mount crossing
    s = State("int-symlink-bundle")
    x = s.clone_bundle()
    os.rename(s.staging(x), s.path("var/lib/lubarchy/elsewhere"))
    os.symlink("../elsewhere", s.staging(x))
    s.add_attempt("complete", s.base_bundle, cache=False)
    att = s.add_attempt("failed_before", s.base_bundle, cache=False)
    p = pathlib.Path(s.journal_dir(att), pa.JOURNAL_FILE)
    d = json.loads(p.read_text())
    d["digests"]["transaction_digest"] = "sha256:" + x
    d["digests"]["bundle_digest"] = None
    p.write_bytes(resolver.canonical_json(d) + b"\n")
    expect(cls(s, x)[0] == "invalid_or_ambiguous", f"symlinked bundle {cls(s, x)}")
    s = State("int-symlink-root")
    os.rename(s.staging(), s.path("var/lib/lubarchy/real-staging"))
    os.symlink("real-staging", s.staging())
    rejects("unsafe_root", plan, s)
    s = State("int-mount")
    st0 = os.lstat(s.staging(s.base_bundle, ps.MANIFEST))
    real_lstat = os.lstat

    def fake_lstat(p, *a, **k):
        r = real_lstat(p, *a, **k)
        if os.path.basename(p) == ps.MANIFEST and s.base_bundle in p:
            return os.stat_result((r.st_mode, r.st_ino, r.st_dev + 1) + tuple(r)[3:])
        return r
    gc.os.lstat = fake_lstat
    try:
        got = cls(s, s.base_bundle)
    finally:
        gc.os.lstat = real_lstat
    expect(got == ("invalid_or_ambiguous", "unsafe_object") and st0.st_dev == os.lstat(
        s.staging(s.base_bundle, ps.MANIFEST)).st_dev, f"mount crossing {got}")
    return f"{n} integrity cases retained (corrupt/missing/extra payload, extra entry, manifest/digest/name " \
           "mismatch, symlink, FIFO, group-writable, hard links, cache anomalies); wrong owner and symlinked root " \
           "refused; symlinked bundle and mount crossing retained"


@check
def temporary_and_unrecognized_entries():                                         # 21, 24, 25
    s = State("temp")
    t = s.add_temporary()
    expect(cls(s, t) == ("eligible_abandoned", "abandoned_temporary"), f"safe temporary {cls(s, t)}")
    s.add_attempt("transaction_revalidated", "ab" * 32, cache=False)
    expect(cls(s, t) == ("protected_active", "temporary_possibly_owned"), "possibly-owned temporary")
    s = State("temp-bad")
    t1 = s.add_temporary()
    os.symlink("/etc", os.path.join(s.staging(t1), "link"))
    t2 = s.add_temporary()
    os.mkdir(os.path.join(s.staging(t2), ps.PAYLOAD_DIR, "nested"))
    t3 = s.add_temporary(files=False)
    os.chmod(s.staging(t3), 0o777)
    t4 = s.add_temporary(files=False)
    os.mkdir(s.path("var/lib/lubarchy/decoy"))
    pathlib.Path(s.path("var/lib/lubarchy/decoy/keep.deb")).write_bytes(b"decoy")
    os.symlink("../../decoy", os.path.join(s.staging(t4), ps.PAYLOAD_DIR))
    for t in (t1, t2, t3, t4):
        expect(cls(s, t)[0] == "invalid_or_ambiguous", f"unsafe temporary {t}")
    t5 = s.add_temporary()
    real_lstat = os.lstat

    def foreign_owner(path, *a, **k):
        r = real_lstat(path, *a, **k)
        if t5 in str(path):
            return os.stat_result((r.st_mode, r.st_ino, r.st_dev, r.st_nlink, r.st_uid + 1) + tuple(r)[5:])
        return r
    gc.os.lstat = foreign_owner
    try:
        expect(cls(s, t5)[0] == "invalid_or_ambiguous", "foreign-owned temporary not retained")
    finally:
        gc.os.lstat = real_lstat
    shutil.rmtree(s.staging(t5))
    os.mkdir(s.staging(".staging-UPPER123"))
    os.mkdir(s.staging("foreign-dir"))
    pathlib.Path(s.staging("SENTINEL")).write_text("keep")
    os.symlink("/etc", s.staging("evil-link"))
    p = plan(s)
    names = {u["name"]: u["type"] for u in p["unrecognized"]}
    expect(names == {".staging-UPPER123": "directory", "SENTINEL": "file", "evil-link": "symlink",
                     "foreign-dir": "directory"} and p["summary"]["unrecognized"] == 4, f"unrecognized {names}")
    before = tree(s.staging())
    receipt = gc.collect_garbage(s.h)
    after = tree(s.staging())
    expect(os.path.exists(s.path("var/lib/lubarchy/decoy/keep.deb")), "symlink target deleted")
    for k in (".staging-UPPER123", "SENTINEL", "evil-link", "foreign-dir", t1, t2, t3, t4):
        expect(k in after and after[k] == before[k], f"{k} touched")
    expect(not any(x["name"] in (t1, t2, t3, t4) for x in receipt["removed"]), "unsafe temporary removed")
    return "safe temporary eligible, possibly-owned protected, unsafe temporaries and unrecognized entries " \
           "(including a look-alike name and a symlink) retained untouched"


# ---------------------------------------------------------------------------
# Execution

@check
def gc_deletes_exactly_the_eligible_state():                                     # 31-46 (deletion, receipt)
    s = State("run")
    h = s.h
    protected = s.clone_bundle()
    patt = s.add_attempt("failed_after", protected)
    s.add_recovery(patt, "manual")
    corrupt = s.clone_bundle()
    s.add_attempt("complete", corrupt, cache=False)
    with open(os.path.join(s.staging(corrupt, ps.PAYLOAD_DIR), sorted(os.listdir(s.staging(corrupt, ps.PAYLOAD_DIR)))[0]),
              "r+b") as f:
        f.write(b"\x00")
    temp = s.add_temporary()
    second = s.clone_bundle()
    satt = s.add_attempt("complete", second)            # a cache with APT's lock file and partial/
    pathlib.Path(s.staging("SENTINEL")).write_text("keep")
    # protected system state that GC must never touch (inside the fake root)
    for rel, data in (("var/cache/apt/archives/foo_1_all.deb", b"apt"), ("var/lib/dpkg/status", b"dpkg"),
                      ("var/lib/apt/extended_states", b"marks"), ("var/lib/dpkg/lock", b""),
                      ("var/lib/dpkg/lock-frontend", b"")):
        os.makedirs(os.path.dirname(s.path(rel)), exist_ok=True)
        pathlib.Path(s.path(rel)).write_bytes(data)
    sys_before = {d: tree(s.path(d)) for d in ("etc", "var/cache", "var/lib/dpkg", "var/lib/apt")}
    journals_before = {p: p.read_bytes() for p in pathlib.Path(s.path("var/lib/lubarchy")).rglob("journal.json")}
    p = plan(s)
    expect({c["name"]: c["classification"] for c in p["candidates"]} ==
           {s.base_bundle: "eligible_complete", second: "eligible_complete", temp: "eligible_abandoned",
            protected: "protected_recovery", corrupt: "invalid_or_ambiguous"}, f"plan {p['candidates']}")
    r = gc.collect_garbage(h)
    expect(r["result"] == "complete" and r["phase"] == "complete", f"receipt {r['result']}")
    removed = {x["name"] for x in r["removed"]}
    expect(removed == {s.base_bundle, second, temp}, f"removed {removed}")
    expect(sorted(os.listdir(os.path.join(s.journal_dir(satt), pa.APPLY_CACHE))) == ["lock"],
           "APT lock file of the attempt cache was not preserved (or payload links remain)")
    expect(r["bytes_reclaimed"] == sum(x["bytes"] for x in r["eligible"]) > 0, "bytes reclaimed")
    expect({x["name"]: x["classification"] for x in r["retained"]} ==
           {protected: "protected_recovery", corrupt: "invalid_or_ambiguous"}, f"retained {r['retained']}")
    expect(not os.path.exists(s.staging(s.base_bundle)) and not os.path.exists(s.staging(temp)), "not deleted")
    expect(os.path.isdir(s.staging(protected)) and os.path.isdir(s.staging(corrupt)) and
           os.path.exists(s.staging("SENTINEL")), "retained state removed")
    cache = os.path.join(s.journal_dir(s.base_attempt), pa.APPLY_CACHE)
    expect(sorted(os.listdir(cache)) == [], f"apply cache payload links not reclaimed: {os.listdir(cache)}")
    pcache = os.path.join(s.journal_dir(patt), pa.APPLY_CACHE)
    expect(sorted(os.listdir(pcache)) != [] and "lock" in os.listdir(pcache), "protected attempt cache touched")
    # the protected recovery payload is still exactly verifiable
    ps.verify_bundle(s.staging(protected), h.inspector(), expected={"transaction_digest": "sha256:" + protected})
    rejects("recovery_required", pa.apply_current_plan, h)
    expect({p: p.read_bytes() for p in pathlib.Path(s.path("var/lib/lubarchy")).rglob("journal.json")
            if "package-gc" not in str(p)} == journals_before, "a journal changed or was deleted")
    expect({d: tree(s.path(d)) for d in sys_before} == sys_before, "APT/dpkg/source state touched")
    stored = json.loads(pathlib.Path(s.path(gc.RECEIPT_DIR, r["receipt_id"] + ".json")).read_text())
    expect(stored == r, "durable receipt differs from the result")
    gc.check_document(stored, gc.RECEIPT_SCHEMA_FILE)
    # idempotence and zero-candidate run
    r2 = gc.collect_garbage(h)
    expect(r2["result"] == "nothing_to_collect" and r2["protected_count"] == 2, f"second run {r2}")
    expect(len(os.listdir(s.path(gc.RECEIPT_DIR))) == 1, "zero-candidate run wrote a receipt")
    expect(resolver.canonical_json(plan(s)) == resolver.canonical_json(plan(s)), "plan not deterministic")
    return "removed exactly the complete bundle (+cache payload links, APT lock kept) and the safe temporary; " \
           "recovery bundle (still verifiable, apply still blocked), corrupt bundle, sentinel, journals, APT/dpkg " \
           "state untouched; receipt durable and exact; second run nothing_to_collect"


@check
def apt_sandbox_partial_contract():                                             # M1-N correction 1-14
    """Only the exact APT sandbox shape of `<attempt>/apply-cache/partial/` is accepted with a
    non-root owner: _apt (resolved from the host's /etc/passwd), group root, mode 0700, empty, and
    only in the cache of an attempt whose APT run started. Nothing else gains non-root ownership."""
    n = 0

    def base():
        s = State(f"sbx-{n}")
        cache = os.path.join(s.journal_dir(s.base_attempt), pa.APPLY_CACHE)
        return s, cache, os.path.join(cache, gc.CACHE_PARTIAL)

    def retained(label, mutate, reason="unsafe_object"):
        nonlocal n
        s, cache, part = base()
        mutate(s, cache, part)
        got = cls(s, s.base_bundle)
        expect(got == ("invalid_or_ambiguous", reason), f"{label}: {got}")
        r = gc.collect_garbage(s.h)
        expect(r["result"] == "nothing_to_collect" and os.path.isdir(s.staging(s.base_bundle)), f"{label}: deleted")
        n += 1
    # 1-2: the exact real APT shape is accepted and a real-shaped complete bundle is reclaimed
    s, cache, part = base()
    st = gc._lstat(part)
    expect((st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)) == (FAKE_APT_UID, s.h.owner_gid, 0o700) and
           gc.apt_sandbox_uid(s.h) == FAKE_APT_UID, "model is not the measured _apt:root 0700 shape")
    expect(cls(s, s.base_bundle) == ("eligible_complete", "complete_validated"), "real-shaped bundle not eligible")
    key = (os.lstat(part).st_dev, os.lstat(part).st_ino)
    expect(key in OVERLAY, "APT ownership not modelled")
    r = gc.collect_garbage(s.h)
    expect(key not in OVERLAY, "the overlay of the partial/ GC removed outlived its object")
    os.mkdir(part, 0o700)                          # a new object at the same path (the inode may be reused)
    expect(gc._lstat(part).st_uid == os.geteuid(), "a new object inherited the removed object's ownership")
    os.rmdir(part)
    expect(r["result"] == "complete" and not os.path.exists(part) and not os.path.exists(s.staging(s.base_bundle))
           and os.listdir(cache) == [], f"real-shaped bundle not reclaimed: {r['result']} {os.listdir(cache)}")
    expect(gc.collect_garbage(s.h)["result"] == "nothing_to_collect", "not idempotent")
    n += 1
    # 3-7: wrong owner, group, mode, contents or type of partial/
    retained("foreign uid", lambda s, c, p: own(p, FAKE_APT_UID + 4000, s.h.owner_gid, 0o700))
    retained("_apt, wrong group", lambda s, c, p: own(p, FAKE_APT_UID, s.h.owner_gid + 1, 0o700))
    for perm in (0o750, 0o755, 0o770, 0o1700, 0o2700, 0o600, 0o711):
        retained(f"_apt, mode {oct(perm)}", lambda s, c, p, perm=perm: own(p, FAKE_APT_UID, s.h.owner_gid, perm))

    def non_empty(s, c, p):
        pathlib.Path(p, "intel-microcode_1_amd64.deb.FAILED").write_bytes(b"x")
    retained("_apt, non-empty", non_empty, "apply_cache_unexpected")

    def nested(s, c, p):
        os.mkdir(os.path.join(p, "partial"), 0o700)
    retained("_apt, nested directory", nested, "apply_cache_unexpected")

    def symlinked(s, c, p):
        os.rmdir(p)
        empty = os.path.join(s.h.root, "empty")
        os.mkdir(empty, 0o700)
        os.symlink(empty, p)
        own(p, FAKE_APT_UID, s.h.owner_gid, 0o700)        # even with the sandbox owner on the link itself
    retained("symlink partial", symlinked)

    def fifo(s, c, p):
        os.rmdir(p)
        os.mkfifo(p, 0o700)
        own(p, FAKE_APT_UID, s.h.owner_gid, 0o700)
    retained("FIFO partial", fifo)

    def file_partial(s, c, p):
        os.rmdir(p)
        pathlib.Path(p).write_text("")
        own(p, FAKE_APT_UID, s.h.owner_gid, 0o700)
    retained("regular-file partial", file_partial)

    def replaced(s, c, p):                         # APT's ownership never carries over to a new object
        replace_partial(p)
    retained("replaced (root-owned) partial after APT ran", replaced)
    retained("replaced (root-owned) partial after APT ran, same inode identity",
             lambda s, c, p: replace_partial(p, reuse_identity=True))

    def mount_crossing(s, c, p):
        st = os.lstat(p)

        def other_dev(path, dir_fd=None):
            x = overlay_lstat(path, dir_fd=dir_fd)
            if (x.st_ino, stat.S_IFMT(x.st_mode)) == (st.st_ino, stat.S_IFDIR):
                t = list(x)
                t[2] = x.st_dev + 1
                return os.stat_result(t)
            return x
        gc._lstat = other_dev

    s, cache, part = base()
    mount_crossing(s, cache, part)
    try:
        got = cls(s, s.base_bundle)
    finally:
        gc._lstat = overlay_lstat
    expect(got == ("invalid_or_ambiguous", "unsafe_object"), f"mount-crossing partial: {got}")
    n += 1
    # 8-9: the sandbox owner anywhere else, or any other non-root cache/bundle object
    for label, where in (("lock", lambda s, c: os.path.join(c, gc.CACHE_LOCK)), ("cache dir", lambda s, c: c),
                         ("cache payload", lambda s, c: os.path.join(c, sorted(e for e in os.listdir(c)
                                                                               if e.endswith(".deb"))[0])),
                         ("bundle dir", lambda s, c: s.staging(s.base_bundle)),
                         ("manifest", lambda s, c: s.staging(s.base_bundle, ps.MANIFEST)),
                         ("archives", lambda s, c: s.staging(s.base_bundle, ps.PAYLOAD_DIR))):
        for uid in (FAKE_APT_UID, FAKE_APT_UID + 4000):
            def mut(s, c, p, where=where, uid=uid):
                target = where(s, c)
                if label == "lock" and not os.path.exists(target):
                    pathlib.Path(target).write_text("")
                    os.chmod(target, 0o640)
                st = os.lstat(target)
                own(target, uid, s.h.owner_gid, stat.S_IMODE(st.st_mode))
            retained(f"uid {uid} on {label}", mut)
    s2 = State(f"sbx-{n}-temp")
    t = s2.add_temporary()
    own(s2.staging(t, ps.PAYLOAD_DIR), FAKE_APT_UID, s2.h.owner_gid, 0o700)
    expect(cls(s2, t) == ("invalid_or_ambiguous", "unsafe_object"), "_apt-owned temporary accepted")
    n += 1
    for kind in ("file", "dir"):
        fake = os.stat_result(((stat.S_IFDIR if kind == "dir" else stat.S_IFREG) | 0o700, 1, 1, 1, FAKE_APT_UID,
                               s2.h.owner_gid, 0, 0, 0, 0))
        try:
            gc._check_obj(fake, 1, s2.h, kind)
            raise Failure(f"_check_obj accepted the sandbox owner for a {kind}")
        except gc.Unsafe:
            pass
    # 12: a root-owned partial/ is the legitimate shape only while APT never ran
    s3 = State(f"sbx-{n}-noapt")
    x = s3.clone_bundle()
    att = s3.add_attempt("failed_before", x)
    part = os.path.join(s3.journal_dir(att), pa.APPLY_CACHE, gc.CACHE_PARTIAL)
    expect(gc._lstat(part).st_uid == s3.h.owner_uid, "no-APT partial not root-owned")
    expect(cls(s3, x) == ("eligible_abandoned", "failed_before_apply"), f"no-APT partial {cls(s3, x)}")
    own(part, FAKE_APT_UID, s3.h.owner_gid, 0o700)
    expect(cls(s3, x) == ("invalid_or_ambiguous", "unsafe_object"), "_apt partial accepted where APT never ran")
    n += 1

    def root_after_apt(s, c, p):
        _forget((os.lstat(p).st_dev, os.lstat(p).st_ino))      # APT's chown never happened
    retained("root-owned partial although APT ran", root_after_apt)
    # 13: the sandbox identity comes only from the host's own root-owned /etc/passwd
    for label, lines in (("no _apt", ["root:x:0:0::/root:/bin/bash"]),
                         ("duplicate _apt", [f"_apt:x:{FAKE_APT_UID}:65534::/:/x", "_apt:x:102:65534::/:/x"]),
                         ("_apt uid 0", ["_apt:x:0:65534::/:/x"]),
                         ("_apt non-system uid", [f"_apt:x:{FAKE_APT_UID + 4000}:65534::/:/x"]),
                         ("_apt aliased uid", [f"_apt:x:{FAKE_APT_UID}:65534::/:/x", f"evil:x:{FAKE_APT_UID}:0::/:/x"]),
                         ("malformed passwd", [f"_apt:x:{FAKE_APT_UID}"]),
                         ("other _apt uid", ["_apt:x:{}:65534::/:/x".format(FAKE_APT_UID + 1)])):
        retained(f"passwd: {label}", lambda s, c, p, lines=lines: write_passwd(s.h, lines))

    def passwd_unsafe(mode):
        def m(s, c, p):
            os.chmod(os.path.join(s.h.root, gc.PASSWD), mode)
        return m
    retained("group-writable passwd", passwd_unsafe(0o664))
    retained("other-writable passwd", passwd_unsafe(0o646))

    def passwd_symlink(s, c, p):
        real = os.path.join(s.h.root, "passwd.real")
        os.rename(os.path.join(s.h.root, gc.PASSWD), real)
        os.symlink(real, os.path.join(s.h.root, gc.PASSWD))
    retained("symlinked passwd", passwd_symlink)
    retained("missing passwd", lambda s, c, p: os.unlink(os.path.join(s.h.root, gc.PASSWD)))
    saved = dict(os.environ)
    os.environ.update({"APT_SANDBOX_USER": "evil", "LUBARCHY_APT_UID": str(FAKE_APT_UID + 1)})
    try:
        s4, cache4, part4 = base()
        expect(cls(s4, s4.base_bundle) == ("eligible_complete", "complete_validated") and
               gc.apt_sandbox_uid(s4.h) == FAKE_APT_UID, "environment changed the sandbox identity")
    finally:
        os.environ.clear()
        os.environ.update(saved)
    import inspect
    expect(list(inspect.signature(gc.apt_sandbox_uid).parameters) == ["host"] and
           list(inspect.signature(gc.collect_garbage).parameters) == ["host"] and
           gc.APT_SANDBOX_USER == "_apt" and gc.PASSWD == "etc/passwd" and gc.PARTIAL_MODE == 0o700,
           "sandbox expectations are not fixed source constants")
    return f"exact _apt:root/0700/empty partial accepted and reclaimed (idempotent); {n} wrong owner/group/mode/" \
           "content/type/replacement/mount, sandbox owner elsewhere, root partial after APT, and unresolvable/" \
           "ambiguous/unsafe passwd cases retained; root partial accepted only where APT never ran; environment " \
           "has no effect"


@check
def apt_sandbox_partial_races_fail_closed():                                    # M1-N correction 10-11
    out = []
    for label, mutate, reason in (
            ("owner change", lambda s, p: own(p, FAKE_APT_UID + 4000, s.h.owner_gid, 0o700), "unsafe_object"),
            ("group change", lambda s, p: own(p, FAKE_APT_UID, s.h.owner_gid + 1, 0o700), "unsafe_object"),
            ("mode change", lambda s, p: own(p, FAKE_APT_UID, s.h.owner_gid, 0o755), "unsafe_object"),
            ("contents appear", lambda s, p: pathlib.Path(p, "x.deb").write_bytes(b"x"), "apply_cache_unexpected"),
            ("replaced", lambda s, p: replace_partial(p), "unsafe_object"),
            ("replaced, same inode identity", lambda s, p: replace_partial(p, reuse_identity=True),
             "unsafe_object")):
        s = State("sbx-race-" + label.replace(" ", "-"))
        part = os.path.join(s.journal_dir(s.base_attempt), pa.APPLY_CACHE, gc.CACHE_PARTIAL)
        real = gc.compute_plan

        def planned_then_changed(host, mutate=mutate, s=s, part=part):
            result = real(host)
            mutate(s, part)
            return result
        gc.compute_plan = planned_then_changed
        try:
            r = gc.collect_garbage(s.h)
        finally:
            gc.compute_plan = real
        expect(r["result"] == "failed" and r["failed"] and r["failed"][0]["reason"] == reason and not r["removed"],
               f"{label}: {r['result']} {r['failed']}")
        expect(os.path.isdir(s.staging(s.base_bundle)) and os.path.lexists(part), f"{label}: something deleted")
        ps.verify_bundle(s.staging(s.base_bundle), s.h.inspector(),
                         expected={"transaction_digest": "sha256:" + s.base_bundle})
        out.append(label)
    return f"partial/ {', '.join(out)} after classification -> durable failed receipt before any unlink; " \
           "bundle intact"


@check
def races_and_partial_failures_fail_closed():                                    # 34-38, 47
    cases = []

    def race(label, mutate, want_reason):
        s = State("race-" + label)
        temp = s.add_temporary() if label.startswith("temp") else None
        real = gc.compute_plan

        def planned_then_changed(host):
            result = real(host)
            mutate(s, temp)
            return result
        gc.compute_plan = planned_then_changed
        try:
            r = gc.collect_garbage(s.h)
        finally:
            gc.compute_plan = real
        expect(r["result"] in ("failed", "partial") and r["failed"] and r["failed"][0]["reason"] == want_reason,
               f"{label}: {r['result']} {r['failed']}")
        b = s.staging(s.base_bundle)
        if os.path.isdir(b) and os.path.exists(os.path.join(b, ps.MANIFEST)):
            m = json.loads(pathlib.Path(b, ps.MANIFEST).read_text())
            expect(all(os.path.lexists(os.path.join(b, ps.PAYLOAD_DIR, x["filename"])) for x in m["payloads"]),
                   f"{label}: payloads were deleted although the race was detected")
        cases.append(label)
        return s, r
    payload = lambda s: s.staging(s.base_bundle, ps.PAYLOAD_DIR, sorted(os.listdir(s.staging(s.base_bundle, ps.PAYLOAD_DIR)))[0])  # noqa: E731

    def journal_changed(s, t):
        p = pathlib.Path(s.journal_dir(s.base_attempt), pa.JOURNAL_FILE)
        p.write_bytes(p.read_bytes().replace(b'"complete"}', b'"complete"} '))
    race("journal-changed", journal_changed, "binding_mismatch")

    def bundle_replaced(s, t):
        x = s.clone_bundle()
        shutil.rmtree(s.staging(s.base_bundle))
        os.rename(s.staging(x), s.staging(s.base_bundle))
    race("bundle-replaced", bundle_replaced, "unsafe_object")

    def file_replaced(s, t):
        path = payload(s)
        data = pathlib.Path(path).read_bytes()
        swap(path, lambda p: pathlib.Path(p).write_bytes(data))
    race("payload-replaced", file_replaced, "unsafe_object")
    race("extra-file", lambda s, t: pathlib.Path(s.staging(s.base_bundle, ps.PAYLOAD_DIR, "x_1_all.deb")).write_bytes(b"x"),
         "unsafe_object")
    race("symlink-substituted", lambda s, t: swap(payload(s), lambda p: os.symlink("/etc/passwd", p)),
         "unsafe_object")
    s, r = race("candidate-disappeared", lambda s, t: shutil.rmtree(s.staging(s.base_bundle)), "unsafe_object")
    race("cache-changed", lambda s, t: pathlib.Path(s.journal_dir(s.base_attempt), pa.APPLY_CACHE, "x").write_text(""),
         "apply_cache_unexpected")
    race("temp-directory-changed", lambda s, t: pathlib.Path(s.staging(t), "new").write_text(""), "unsafe_object")
    # partial: the first candidate removed, the second fails -> partial, never complete; the third not attempted
    s = State("partial")
    t1, t2 = s.add_temporary(), s.add_temporary()
    real = gc.delete_temporary
    calls = []

    def second_fails(host, name, ident):
        calls.append(name)
        if len(calls) == 2:
            raise OSError(5, "injected I/O error")
        return real(host, name, ident)
    gc.delete_temporary = second_fails
    try:
        r = gc.collect_garbage(s.h)
    finally:
        gc.delete_temporary = real
    expect(r["result"] == "partial" and r["phase"] == "partial" and len(r["removed"]) == 1 and
           r["failed"][0]["reason"] == "unsafe_object" and r["bytes_reclaimed"] == r["removed"][0]["bytes"],
           f"partial receipt {r}")
    expect(json.loads(pathlib.Path(s.path(gc.RECEIPT_DIR, r["receipt_id"] + ".json")).read_text())["result"] == "partial",
           "durable partial receipt")
    cases.append("partial")
    # crash after the first deletion: the durable receipt says 'deleting', never complete
    s = State("crash")
    s.add_temporary()

    class Crash(BaseException):
        pass

    def crash(host, name, ident):
        raise Crash()
    gc.delete_temporary = crash
    try:
        gc.collect_garbage(s.h)
        raise Failure("crash not simulated")
    except Crash:
        pass
    finally:
        gc.delete_temporary = real
    stored = [json.loads(p.read_text()) for p in pathlib.Path(s.path(gc.RECEIPT_DIR)).glob("*.json")]
    expect(len(stored) == 1 and stored[0]["phase"] == "deleting" and stored[0]["result"] is None, "crash receipt")
    cases.append("crash")
    # disk full / receipt cannot be written: nothing is deleted
    s = State("disk-full")
    real_replace = pa.durable_replace

    def enospc(*a, **k):
        raise OSError(28, "No space left on device")
    pa.durable_replace = enospc
    try:
        try:
            gc.collect_garbage(s.h)
            raise Failure("GC proceeded without a durable receipt")
        except OSError:
            pass
    finally:
        pa.durable_replace = real_replace
    expect(os.path.isdir(s.staging(s.base_bundle)), "deleted without a durable receipt")
    cases.append("disk-full")
    return f"{len(cases)} cases fail closed: {', '.join(cases)}"


@check
def lock_is_shared_with_apply_and_recovery():                                   # 30-34
    s = State("lock")
    lockfile = pathlib.Path(s.h.root, pa.LOCK_DIR, pa.LOCK_NAME)
    fd = os.open(lockfile, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        rejects("busy", gc.collect_garbage, s.h)        # held by apply, recovery or another GC
        rejects("busy", gc.plan, s.h)
        expect(os.path.isdir(s.staging(s.base_bundle)), "GC deleted while the lock was held")
    finally:
        os.close(fd)
    held = []
    real = gc.compute_plan

    def observe(host):
        held.append(True)
        rejects("busy", pa.apply_current_plan, host)    # apply refused while GC holds the lock
        rejects("busy", pr.recover_interrupted_transaction, host)
        rejects("busy", gc.collect_garbage, host)
        return real(host)
    gc.compute_plan = observe
    try:
        gc.collect_garbage(s.h)
    finally:
        gc.compute_plan = real
    expect(held, "plan not computed under the lock")
    s.h.uid = 1000
    rejects("not_root", gc.collect_garbage, s.h)
    rejects("not_root", gc.plan, s.h)
    return "GC and plan refused while the shared lock is held; apply, recovery and GC refused while GC holds it; " \
           "root only"


@check
def no_caller_control_and_no_unsafe_code():                                     # 26-29, 34, 41-44, 50
    import inspect
    expect(list(inspect.signature(gc.collect_garbage).parameters) == ["host"] and
           list(inspect.signature(gc.plan).parameters) == ["host"], "GC accepts caller parameters")
    cli = gate11._load_cli(HARDWARE / "apply-packages.py")
    saved = cli.pa.RealHost

    class Sentinel:
        def __init__(self):
            raise Failure("CLI constructed a real host")
    cli.pa.RealHost = Sentinel
    try:
        for argv in (["gc", "/var/lib/lubarchy/package-staging"], ["gc", "--older-than", "7d"],
                     ["gc", "0000000000000001"], ["gc", "--force"], ["gc", "-r"], ["gc", "--plan", "plan.json"],
                     ["gc-plan", "--root", "/tmp"], ["gc", "--max-bytes", "1"], ["gc", "--delete", "x"], ["gc", ""]):
            with open(os.devnull, "w") as null, contextlib.redirect_stderr(null):
                expect(cli.main(argv) == 64, f"CLI accepted {argv}")
    finally:
        cli.pa.RealHost = saved
    td = gate11._load_tool(REPO / "tools" / "hwe" / "trusted-deployment.py")
    root = "/root/lubarchy-m1n/" + "a" * 40
    for op in ("gc-plan", "gc"):
        expect(td.privileged_argv(root, op) == ["/usr/bin/env", "-i", "/usr/bin/python3", "-I", "-B",
                                                root + "/tree/hardware/apply-packages.py", op], f"launcher {op}")
    src = (HARDWARE / "package_gc.py").read_text(encoding="utf-8")
    tree_ = ast.parse(src)
    for node in ast.walk(tree_):
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            expect(name not in ("rmtree", "system", "popen", "Popen", "run", "call", "check_call", "check_output",
                                "execv", "removedirs", "walk", "utime", "getmtime", "getctime", "time"),
                   f"package_gc calls {name}()")
    for bad in ("st_mtime", "st_ctime", "st_atime", "time.time", "datetime", "var/cache/apt", "var/lib/dpkg",
                "etc/apt", "sources.list", "shell=True", "subprocess", "environ", "getenv", "argv"):
        expect(bad not in src, f"package_gc refers to {bad}")
    expect("import subprocess" not in src, "GC spawns processes")
    expect("os.unlink(" in src and src.count("dir_fd=") >= 10, "deletion is not descriptor-relative")
    for m in ("os.remove(", "shutil."):
        expect(m not in src, f"package_gc uses {m}")
    expect(not re.search(r"JOURNAL_FILE[^\n]*(unlink|rmdir)", src) and "unlink(pa.JOURNAL_FILE" not in src,
           "package_gc deletes journals")
    return "no caller path/age/ID/force/plan/size/list (exit 64); launcher ops fixed; no rmtree/shell/subprocess/" \
           "time/mtime; no APT cache, dpkg or source paths; descriptor-relative deletion; journals never deleted"


def main():
    sys.stdout.reconfigure(line_buffering=True)
    from importlib.metadata import version
    print(f"python {sys.version.split()[0]}, jsonschema {version('jsonschema')}, dpkg-deb {shutil.which('dpkg-deb')}")
    if shutil.which("dpkg-deb") != ps.DPKG_DEB:
        print(f"FAIL: hardware package state GC needs {ps.DPKG_DEB}", file=sys.stderr)
        return 1
    failures = 0
    old = os.umask(0o022)
    try:
        with tempfile.TemporaryDirectory(prefix="lubarchy-gate14-") as tmp:
            gate11.TMP = gate10.TMP = os.path.realpath(tmp)
            gc._lstat = overlay_lstat
            for name, fn in TRACKED.items():
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
        gc._lstat = REAL_LSTAT
        for name, fn in REAL_OS.items():
            setattr(os, name, fn)
        os.umask(old)
    if failures:
        print(f"FAIL: hardware package state GC ({failures} of {len(CHECKS)} checks failed)", file=sys.stderr)
        return 1
    print(f"PASS: hardware package state GC ({len(CHECKS)} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
