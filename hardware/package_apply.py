"""LUBARCHY privileged hardware-package apply executor (M1-K).

The privileged operation is "apply the current qualified hardware plan", never
"install package X". Everything is derived from source-controlled policy and
fresh local state:

  LUBARCHY transaction lock -> journal (prepared, locked)
  -> source activation (only the LUBARCHY-owned Debian source file, D-013)
  -> authenticated metadata refresh (apt-get update)
  -> fresh probe -> production resolve -> fresh package transaction
  -> root-owned staging (/var/lib/lubarchy/package-staging/<digest>)
  -> final stale-state revalidation and immediate bundle re-hash
  -> journal "applying" (durable) -> APT apply (offline, direct targets only)
  -> package-state, source-state and package-file validation -> complete

APT/dpkg installation is NOT atomic. The journal records durable phase
evidence; it is not a transactional rollback. After APT has started, a
failure is recorded as `failed` and the executor stops: it never runs remove,
purge, `apt-get -f install`, dpkg repair or any rollback command. A journal
left in a non-terminal phase, or a failed attempt whose APT phase had
started, blocks every later invocation with `recovery_required`. Interrupted
transaction recovery is later work.

The executor requires effective UID 0 and never escalates privilege itself:
no sudo, setuid, sudoers, polkit rule or daemon. Privilege is supplied
externally (a later installer/system service).
"""

import copy
import datetime
import fcntl
import hashlib
import os
import stat
import subprocess

import package_staging as ps
import package_transaction as pt
import resolver
import source_activation as sa

JOURNAL_SCHEMA_ID = "lubarchy.packages.apply-journal/v1"
JOURNAL_SCHEMA_FILE = "apply-journal-v1.schema.json"

# Fixed system locations (relative to the host root; "/" in production).
LOCK_DIR = "run/lubarchy"
LOCK_NAME = "package-transaction.lock"
LUBARCHY_STATE = "var/lib/lubarchy"
JOURNAL_DIR = "var/lib/lubarchy/package-transactions"
STAGING_DIR = "var/lib/lubarchy/package-staging"
JOURNAL_FILE = "journal.json"
APPLY_CACHE = "apply-cache"
assert "/" + LOCK_DIR + "/" + LOCK_NAME == pt.TRANSACTION_LOCK
assert "/" + STAGING_DIR == ps.STAGING_ROOT

APT_GET = pt.APT_GET
DPKG = pt.DPKG
DPKG_QUERY = pt.DPKG_QUERY
# Literal sanitized environment for every privileged command (never inherited).
APPLY_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LANGUAGE": "C", "LC_ALL": "C",
             "DEBIAN_FRONTEND": "noninteractive", "HOME": "/root"}
APPLY_BASE_ARGV = (APT_GET, "install", "--yes", "--quiet", "--no-install-recommends", "--no-remove",
                   "--no-download")
# Caller-controlled APT options are impossible. The only option LUBARCHY
# generates itself is the archive cache of the current attempt.
INTERNAL_OPTION = "Dir::Cache::archives"
FORBIDDEN_APT_ARGUMENTS = tuple(a for a in pt.FORBIDDEN_APT_ARGUMENTS if a not in ("--no-download", "-o"))
COMMAND_TIMEOUT_SECONDS = 3600

PHASES = ("prepared", "locked", "source_activated", "metadata_refreshed", "transaction_revalidated",
          "bundle_staged", "bundle_verified", "applying", "applied", "validated", "complete", "failed")
TERMINAL = ("complete", "failed")
APPLY_PHASES = ("applying", "applied", "validated")

EXIT_OK = 0
EXIT_MALFORMED = 2
EXIT_STALE = 3
EXIT_POLICY = 4
EXIT_UNSAFE = 5
EXIT_PAYLOAD = 6
EXIT_CONTRACT = 7
EXIT_BUSY = 8
EXIT_RECOVERY = 9
EXIT_APPLY_FAILED = 10
EXIT_USAGE = 64
EXIT_NOT_ROOT = 77
ERROR_EXIT = {
    "input_malformed": EXIT_MALFORMED,
    "stale_transaction": EXIT_STALE, "stale_activation": EXIT_STALE, "stale_bundle": EXIT_STALE,
    "plan_rejected": EXIT_POLICY, "unauthorised_source": EXIT_POLICY, "trusted_rejected": EXIT_POLICY,
    "signed_by_missing": EXIT_POLICY,
    "unsafe_change_set": EXIT_UNSAFE, "unsafe_path": EXIT_UNSAFE, "ambiguous_ownership": EXIT_UNSAFE,
    "nothing_to_stage": EXIT_UNSAFE, "malformed_source": EXIT_MALFORMED,
    "payload_mismatch": EXIT_PAYLOAD, "acquisition_failure": EXIT_PAYLOAD, "provenance_failure": EXIT_PAYLOAD,
    "metadata_failure": EXIT_PAYLOAD,
    "contract_failure": EXIT_CONTRACT, "backend_failure": EXIT_CONTRACT, "source_activation_failed": EXIT_CONTRACT,
    "metadata_refresh_failed": EXIT_CONTRACT, "journal_failure": EXIT_CONTRACT,
    "busy": EXIT_BUSY, "recovery_required": EXIT_RECOVERY,
    "apply_failed": EXIT_APPLY_FAILED, "validation_failed": EXIT_APPLY_FAILED, "recovery_failed": EXIT_APPLY_FAILED,
    "not_root": EXIT_NOT_ROOT,
}


class ApplyError(Exception):
    """Messages carry check names and package names only, never command output."""

    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message

    @property
    def exit_status(self):
        return ERROR_EXIT.get(self.code, EXIT_CONTRACT)


def _wrap(e):
    return ApplyError(e.code, e.message)


# ---------------------------------------------------------------------------
# Host: every side effect of the executor goes through it

class RealHost:
    """Production host: the real root filesystem, fixed binaries, the trusted
    probe, the system package manager. Has no parameters."""

    root = "/"
    owner_uid = 0
    owner_gid = 0

    def euid(self):
        return os.geteuid()

    def fchown(self, fd):
        os.fchown(fd, 0, 0)

    def now(self):
        return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def attempt_id(self):
        return os.urandom(8).hex()

    def inventory(self):
        import probe
        try:
            return probe.Probe().inventory()
        except probe.ProbeError as e:
            raise ApplyError("contract_failure", f"probe failed ({e.code})") from None

    def rules(self):
        return resolver.load_production_rules()

    def backend(self):
        return pt.SystemBackend()

    def architecture(self):
        return sa.native_architecture()

    def keyring_sha256(self):
        return sa.keyring_sha256()

    def run(self, argv):
        """The executor's only command runner: exact allowlist, literal
        environment, shell=False, no stdin."""
        if not allowed_command(argv, self.root):
            raise ApplyError("contract_failure", "command is not an allowlisted executor operation")
        try:
            r = subprocess.run(list(argv), env=dict(APPLY_ENV), shell=False, stdin=subprocess.DEVNULL,
                               capture_output=True, timeout=COMMAND_TIMEOUT_SECONDS, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise ApplyError("backend_failure", f"{os.path.basename(argv[0])} could not run") from None
        return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace")

    def acquirer(self):
        return SystemAcquirer()

    def inspector(self):
        return ps.DebInspector()


class SystemAcquirer:
    """Exact payload acquisition through the system APT configuration (after
    activation and metadata refresh). Download targets are internal."""

    def download(self, op, dest):
        rc, _out = ps._run([APT_GET, "--quiet", "download", ps.acquisition_target(op)], dict(ps.COMMAND_ENV), cwd=dest)
        if rc != 0:
            raise ps.StagingError("acquisition_failure", f"{op['package']} could not be acquired")


def allowed_command(argv, root="/"):
    """Exact allowlist of executor commands."""
    argv = list(argv)
    if any(not isinstance(a, str) or "\n" in a or "\0" in a for a in argv):
        return False
    fixed = ([APT_GET, "update"], [APT_GET, "check"], [DPKG, "--audit"])
    if argv in [list(f) for f in fixed]:
        return True
    if len(argv) == 3 and argv[:2] == [DPKG_QUERY, "--listfiles"]:
        return bool(pt.PACKAGE_NAME.match(argv[2].split(":")[0]))
    # M1-L bounded recovery: configure with trigger processing suppressed (fixed internal option),
    # or trigger processing; exact package names only (never "all pending" forms or other options)
    for head in ([DPKG, "--configure", "--no-triggers"], [DPKG, "--triggers-only"]):
        if len(argv) > len(head) and argv[:len(head)] == head:
            names = argv[len(head):]
            return len(set(names)) == len(names) and all(pt.PACKAGE_NAME.match(n) for n in names)
    return allowed_apply_argv(argv, root)


def allowed_apply_argv(argv, root="/"):
    base = list(APPLY_BASE_ARGV)
    if argv[:len(base)] != base or len(argv) < len(base) + 3:
        return False
    if argv[len(base)] != "-o" or not argv[len(base) + 1].startswith(INTERNAL_OPTION + "="):
        return False
    cache = argv[len(base) + 1].split("=", 1)[1]
    journal = os.path.join(root, JOURNAL_DIR)
    if not (cache.startswith(journal + "/") and cache.endswith("/" + APPLY_CACHE + "/")):
        return False
    attempt = cache[len(journal) + 1:-len("/" + APPLY_CACHE + "/")]
    if not (len(attempt) == 16 and all(c in "0123456789abcdef" for c in attempt)):
        return False
    targets = argv[len(base) + 2:]
    for t in targets:
        name, _, rest = t.partition(":")
        arch, _, ver = rest.partition("=")
        if not (pt.PACKAGE_NAME.match(name) and pt.ARCHITECTURE.match(arch) and pt.VERSION.match(ver)):
            return False
    return not any(a in FORBIDDEN_APT_ARGUMENTS for a in argv)


# ---------------------------------------------------------------------------
# Filesystem helpers (no symlink following; owner/mode checked)

def _p(host, rel):
    return os.path.join(host.root, rel)


def _ensure_dir(host, rel, mode):
    """Create (or check) a host-owned directory chain under the host root:
    every component a real directory, never a symlink; the created/checked
    leaf owned by the host owner and not group/other writable."""
    path = _p(host, rel)
    cur = host.root
    for part in rel.split("/"):
        cur = os.path.join(cur, part)
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            old = os.umask(0o077)
            try:
                os.mkdir(cur, mode)
            finally:
                os.umask(old)
            os.chmod(cur, mode)
            st = os.lstat(cur)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise ApplyError("unsafe_path", f"/{rel}: a path component is not a real directory")
    st = os.lstat(path)
    if st.st_uid != host.owner_uid or st.st_mode & 0o022:
        raise ApplyError("unsafe_path", f"/{rel} must be owned by root and not group/other writable")
    return path


def _open_dir(path):
    try:
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise ApplyError("unsafe_path", f"{os.path.basename(path)} is not a directory (or is a symlink)") from None


def durable_replace(host, directory, name, data, mode):
    """temp file (O_EXCL|O_NOFOLLOW) -> write -> owner/mode -> fsync -> atomic
    rename -> directory fsync. Never follows symlinks."""
    dfd = _open_dir(directory)
    tmp = f".{name}.{host.attempt_id()}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dfd)
        try:
            os.write(fd, data)
            host.fchown(fd)
            os.fchmod(fd, mode)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        os.fsync(dfd)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=dfd)
        except OSError:
            pass
        raise
    finally:
        os.close(dfd)


# ---------------------------------------------------------------------------
# Lock

class TransactionLock:
    """The single LUBARCHY transaction lock (/run/lubarchy/package-transaction.lock):
    root-owned parent, regular file, O_NOFOLLOW, exclusive non-blocking flock.
    The lock file is never deleted; APT/dpkg take their own locks afterwards."""

    def __init__(self, host):
        self.host = host
        self.fd = None

    def acquire(self):
        d = _ensure_dir(self.host, LOCK_DIR, 0o755)
        try:
            fd = os.open(os.path.join(d, LOCK_NAME), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        except OSError:
            raise ApplyError("unsafe_path", "transaction lock is not a regular file (or is a symlink)") from None
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != self.host.owner_uid or st.st_mode & 0o077:
            os.close(fd)
            raise ApplyError("unsafe_path", "transaction lock has an unexpected type, owner or mode")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise ApplyError("busy", "another LUBARCHY package transaction holds the lock") from None
        self.fd = fd
        return self

    def release(self):
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


# ---------------------------------------------------------------------------
# Journal

class Journal:
    """Durable per-attempt journal: /var/lib/lubarchy/package-transactions/<attempt>/journal.json.
    Every phase change is a durable atomic replace. Records digests, phases
    with UTC times, result and failure code; never raw command output,
    environment or caller text."""

    def __init__(self, host):
        self.host = host
        self.root = _ensure_dir(host, JOURNAL_DIR, 0o700)
        self.attempt = host.attempt_id()
        if len(self.attempt) != 16 or any(c not in "0123456789abcdef" for c in self.attempt):
            raise ApplyError("journal_failure", "attempt identifier is malformed")
        self.dir = os.path.join(self.root, self.attempt)
        os.mkdir(self.dir, 0o700)
        self.doc = {"schema": JOURNAL_SCHEMA_ID, "attempt_id": self.attempt, "phase": "prepared", "result": None,
                    "failure": None, "apply_started": False, "phases": [], "source_activation": None,
                    "digests": {"source_activation_digest": None, "repository_metadata_digest": None,
                                "transaction_digest": None, "bundle_digest": None,
                                "installed_state_digest_before": None},
                    "expected": {"direct": [], "closure": []}, "recovery_evidence": None, "post_state": None}
        self.transition("prepared")

    def transition(self, phase, **updates):
        if phase not in PHASES:
            raise ApplyError("journal_failure", f"unknown phase {phase}")
        cur = self.doc["phase"]
        if self.doc["phases"] and cur in TERMINAL:
            raise ApplyError("journal_failure", f"journal is already terminal ({cur})")
        if phase not in ("failed",) and self.doc["phases"] and PHASES.index(phase) <= PHASES.index(cur):
            raise ApplyError("journal_failure", f"phase {phase} cannot follow {cur}")
        doc = copy.deepcopy(self.doc)
        for k, v in updates.items():
            if k == "digests":
                doc["digests"].update(v)
            else:
                doc[k] = v
        if phase == "applying":
            if not doc.get("recovery_evidence"):
                raise ApplyError("journal_failure", "recovery evidence must be durable before applying")
            doc["apply_started"] = True
        doc["phase"] = phase
        doc["phases"].append({"phase": phase, "at": self.host.now()})
        check_journal(doc)
        durable_replace(self.host, self.dir, JOURNAL_FILE, resolver.canonical_json(doc) + b"\n", 0o600)
        self.doc = doc                 # in memory only what is durable on disk

    def fail(self, error):
        if self.doc["phase"] in TERMINAL:
            return
        self.transition("failed", result="failed", failure={"code": error.code, "phase": self.doc["phase"]})


def check_journal(doc):
    import json
    import jsonschema
    with open(os.path.join(resolver.SCHEMA_DIR, JOURNAL_SCHEMA_FILE), encoding="utf-8") as f:
        schema = json.load(f)
    if any(True for _ in jsonschema.Draft202012Validator(schema).iter_errors(doc)):
        raise ApplyError("journal_failure", "journal does not satisfy apply-journal v1")
    return doc


def prior_attempts(host):
    """(attempt, phase, apply_started) of every journal; unreadable entries fail closed."""
    root = _p(host, JOURNAL_DIR)
    try:
        st = os.lstat(root)
    except FileNotFoundError:
        return []
    if not stat.S_ISDIR(st.st_mode):
        raise ApplyError("recovery_required", "journal directory is not a directory")
    out = []
    for name in sorted(os.listdir(root)):
        p = os.path.join(root, name, JOURNAL_FILE)
        try:
            data = sa.read_regular(p, limit=1 << 20)
            doc = check_journal(resolver.parse_strict(data, label="journal"))
        except (sa.SourceError, resolver.ResolverError, ApplyError):
            raise ApplyError("recovery_required", f"journal {name} is unreadable or malformed") from None
        out.append((name, doc["phase"], doc["apply_started"]))
    return out


def require_no_pending(host):
    resolved, blocking = _pr.resolution_state(host)
    if blocking:
        raise ApplyError("recovery_required", blocking)
    for name, phase, started in prior_attempts(host):
        if name in resolved:
            continue
        if phase not in TERMINAL:
            raise ApplyError("recovery_required", f"attempt {name} stopped in phase {phase}")
        if phase == "failed" and started:
            raise ApplyError("recovery_required", f"attempt {name} failed after APT started")


# ---------------------------------------------------------------------------
# Source activation (only the LUBARCHY-owned file)

def activate_source(host, contract):
    """Write the rendered owned source durably; re-inspect and require the
    contract post-state. On failure restore the owned file's exact pre-attempt
    state. Foreign files are never written."""
    parts = _p(host, sa.SOURCE_PARTS)
    st = os.lstat(parts)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or st.st_uid != host.owner_uid or st.st_mode & 0o022:
        raise ApplyError("unsafe_path", "sources.list.d must be a root-owned directory")
    owned = os.path.join(parts, sa.OWNED_NAME)
    try:
        before = sa.read_regular(owned)
    except sa.SourceError:
        if os.path.lexists(owned):
            raise ApplyError("unsafe_path", "owned source exists but is not a regular file") from None
        before = None
    data = sa.render(contract)
    foreign_before = [f for f in contract["pre_state"]["files"] if f["class"] == "foreign"]
    try:
        durable_replace(host, parts, sa.OWNED_NAME, data, 0o644)
        after = sa.compile_activation(sa.inspect(host.root), contract["architecture"], contract["keyring_sha256"])
        if after["action"] != "already_satisfied" or after["post_state_digest"] != contract["post_state_digest"]:
            raise ApplyError("source_activation_failed", "re-inspected source state differs from the contract")
        if [f for f in after["pre_state"]["files"] if f["class"] == "foreign"] != foreign_before:
            raise ApplyError("source_activation_failed", "foreign source state changed")
        if sa.read_regular(owned) != data:
            raise ApplyError("source_activation_failed", "owned source bytes differ from the rendering")
        return after
    except BaseException as e:
        _restore_owned(host, parts, before)
        if isinstance(e, (sa.SourceError, ApplyError)):
            raise ApplyError("source_activation_failed", e.message) from None
        if isinstance(e, OSError):
            raise ApplyError("source_activation_failed", f"owned source write failed ({e.errno})") from None
        raise


def _restore_owned(host, parts, before):
    try:
        if before is None:
            dfd = _open_dir(parts)
            try:
                os.unlink(sa.OWNED_NAME, dir_fd=dfd)
                os.fsync(dfd)
            except FileNotFoundError:
                pass
            finally:
                os.close(dfd)
        else:
            durable_replace(host, parts, sa.OWNED_NAME, before, 0o644)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Validation

def installed_snapshot(backend, native):
    state, holds = pt.parse_installed(backend.installed_text())
    state = pt.apply_marks(state, pt.parse_auto(backend.auto_text(), native), native)
    return {(e["package"], e["architecture"]): e for e in state}, holds


def expected_post_state(before, tx):
    want = {k: dict(v) for k, v in before.items()}
    for o in tx["operations"]:
        if o["action"] != "install":
            continue
        want[(o["package"], o["architecture"])] = {"package": o["package"], "architecture": o["architecture"],
                                                   "version": o["version"], "status": "install ok installed",
                                                   "automatic": o["automatic_after"]}
    return want


def compare_post_state(before, after, holds_before, holds_after, tx):
    """Exact comparison: the transaction's closure installed with its versions,
    architectures and marks, and nothing else changed."""
    want = expected_post_state(before, tx)
    problems = []
    for k in sorted(set(want) | set(after)):
        w, a = want.get(k), after.get(k)
        name = f"{k[0]}:{k[1]}"
        if a is None:
            problems.append(f"{name} removed or missing")
        elif w is None:
            problems.append(f"{name} unexpectedly installed")
        else:
            if a["version"] != w["version"]:
                c = pt.version_compare(a["version"], w["version"])
                problems.append(f"{name} {'downgraded' if c < 0 else 'version changed'}")
            if a["status"] != w["status"]:
                problems.append(f"{name} status {a['status']}")
            if a["automatic"] != w["automatic"]:
                problems.append(f"{name} mark {'automatic' if a['automatic'] else 'manual'}")
    if holds_before != holds_after:
        problems.append("held packages changed")
    return problems


def result_document(after, tx):
    return [{"package": o["package"], "architecture": o["architecture"], "version": after[k]["version"],
             "automatic": after[k]["automatic"], "status": after[k]["status"], "relation": o["relation"]}
            for o in tx["operations"] for k in [(o["package"], o["architecture"])] if k in after]


def package_files_present(host, names):
    """Every non-directory path dpkg lists for each package exists (lstat)."""
    missing = 0
    for n in names:
        rc, out = host.run([DPKG_QUERY, "--listfiles", n])
        if rc != 0:
            return False
        for path in out.splitlines():
            if not path.startswith("/") or path == "/.":
                continue
            full = os.path.join(host.root, path.lstrip("/"))
            try:
                os.lstat(full)
            except FileNotFoundError:
                missing += 1
    return missing == 0


# ---------------------------------------------------------------------------
# The executor

def fresh_state(host, rules):
    """Fresh source contract, metadata evidence, inventory and transaction."""
    backend = host.backend()
    contract = sa.compile_activation(sa.inspect(host.root), host.architecture(), host.keyring_sha256())
    metadata = sa.metadata_evidence(contract, backend.indextargets_text())
    inventory = host.inventory()
    tx = pt.compile_transaction(inventory, rules, backend)
    return contract, metadata, inventory, tx, backend


def apply_current_plan(host):
    """Apply the current qualified hardware plan. Returns the final journal document."""
    if host.euid() != 0:
        raise ApplyError("not_root", "the package apply executor requires effective UID 0")
    lock = TransactionLock(host).acquire()
    journal = None
    try:
        require_no_pending(host)
        _ensure_dir(host, LUBARCHY_STATE, 0o755)
        journal = Journal(host)
        journal.transition("locked")
        rules = host.rules()

        # Source activation (pre-state compiled fresh from the real /etc/apt).
        contract = sa.compile_activation(sa.inspect(host.root), host.architecture(), host.keyring_sha256())
        activation = {"action": contract["action"], "model": contract["model"],
                      "pre_state_digest": contract["pre_state_digest"],
                      "post_state_digest": contract["post_state_digest"]}
        if contract["action"] != "already_satisfied":
            contract = activate_source(host, contract)
        journal.transition("source_activated", source_activation=activation,
                           digests={"source_activation_digest": sa.activation_digest(contract)})

        # Authenticated metadata refresh of the real system.
        rc, out = host.run([APT_GET, "update"])
        warnings = [line for line in out.splitlines() if line.startswith(("W:", "E:", "Err:"))]
        if rc != 0 or warnings:
            raise ApplyError("metadata_refresh_failed", f"apt-get update failed or warned ({len(warnings)} lines)")
        backend = host.backend()
        metadata = sa.metadata_evidence(contract, backend.indextargets_text())
        journal.transition("metadata_refreshed",
                           digests={"repository_metadata_digest": metadata["repository_metadata_digest"]})

        # Fresh transaction after activation (nothing compiled earlier is authority).
        inventory = host.inventory()
        tx = pt.compile_transaction(inventory, rules, backend)
        installs = [o for o in tx["operations"] if o["action"] == "install"]
        expected = {"direct": pt.direct_targets(tx["operations"], tx["architecture"]),
                    "closure": [{k: o[k] for k in ("package", "architecture", "version", "relation")}
                                for o in installs]}
        journal.transition("transaction_revalidated", expected=expected,
                           digests={"transaction_digest": pt.transaction_digest(tx),
                                    "installed_state_digest_before": tx["installed_state_digest"]})
        if not installs:
            journal.transition("complete", result="nothing_to_apply")
            return journal.doc

        # Root-owned staging (an existing bundle of this transaction is reused only if it
        # re-verifies against the fresh bindings; a stale one is removed and restaged).
        staging = _ensure_dir(host, STAGING_DIR, 0o755)
        bundle, manifest = _reuse_or_clear_bundle(host, staging, tx, sa.activation_digest(contract),
                                                  metadata["repository_metadata_digest"])
        if bundle is None:
            bundle, manifest, _results = ps._stage_into(staging, tx, sa.activation_digest(contract),
                                                        metadata["repository_metadata_digest"],
                                                        host.acquirer(), host.inspector())
        journal.transition("bundle_staged", digests={"bundle_digest": manifest["bundle_digest"]})

        # Final stale-state revalidation, then the immediate bundle re-hash.
        f_contract, f_metadata, f_inventory, f_tx, backend = fresh_state(host, rules)
        if f_contract["action"] != "already_satisfied" or sa.activation_digest(f_contract) != \
                sa.activation_digest(contract):
            raise ApplyError("stale_activation", "source state changed before apply")
        if resolver.canonical_json(f_tx) != resolver.canonical_json(tx):
            changed = sorted(k for k in tx if tx.get(k) != f_tx.get(k))
            raise ApplyError("stale_transaction", f"transaction is stale: {', '.join(changed)} changed")
        fresh = {"transaction_digest": pt.transaction_digest(f_tx),
                 "source_activation_digest": sa.activation_digest(f_contract),
                 "repository_metadata_digest": f_metadata["repository_metadata_digest"]}
        ps.final_revalidation(manifest, fresh)
        before, holds_before = installed_snapshot(backend, tx["architecture"])
        cache = _apply_cache(host, journal, bundle, manifest)
        ps.verify_bundle(bundle, host.inspector(), expected=fresh, tx=f_tx)
        ps.verify_payloads(cache, manifest["payloads"], host.inspector(), check_modes=True)
        # M1-L: durable pre-apply recovery evidence (dpkg log cursor, trigger-interest records)
        # bound with the verified bundle, before `applying`.
        journal.transition("bundle_verified", recovery_evidence=_pr.bind_recovery_evidence(host, before))
        os.mkdir(os.path.join(cache, "partial"), 0o700)

        # Apply: journal first, then exactly one APT run. No recovery after this point.
        argv = apply_argv(manifest, cache, host.root)
        journal.transition("applying")
        rc, out = host.run(argv)
        fetched = sum(1 for line in out.splitlines() if line.startswith("Get:"))
        if rc != 0 or fetched:
            raise ApplyError("apply_failed", f"apt-get install exited {rc} (fetched {fetched} archives)")
        journal.transition("applied")

        # Validation.
        problems = []
        rc, out = host.run([DPKG, "--audit"])
        if rc != 0 or out.strip():
            problems.append("dpkg --audit is not clean")
        rc, _out = host.run([APT_GET, "check"])
        if rc != 0:
            problems.append("apt-get check failed")
        after, holds_after = installed_snapshot(host.backend(), tx["architecture"])
        problems += compare_post_state(before, after, holds_before, holds_after, tx)
        post_contract = sa.compile_activation(sa.inspect(host.root), host.architecture(), host.keyring_sha256())
        if sa.activation_digest(post_contract) != sa.activation_digest(contract):
            problems.append("source post-state differs")
        owned = os.path.join(_p(host, sa.SOURCE_PARTS), sa.OWNED_NAME)
        if contract["owned_source"] is not None:
            ost = os.lstat(owned)
            if (not stat.S_ISREG(ost.st_mode) or ost.st_uid != host.owner_uid or ost.st_mode & 0o7777 != 0o644
                    or hashlib.sha256(sa.read_regular(owned)).hexdigest() != contract["owned_source"]["sha256"]):
                problems.append("owned source file differs")
        if not package_files_present(host, [o["package"] for o in installs]):
            problems.append("package files missing")
        if problems:
            raise ApplyError("validation_failed", "; ".join(problems))
        journal.transition("validated", post_state={"packages": result_document(after, tx),
                                                    "reboot_required": os.path.exists(
                                                        _p(host, "run/reboot-required"))})
        journal.transition("complete", result="complete")
        return journal.doc
    except (ApplyError, pt.TransactionError, sa.SourceError, ps.StagingError, resolver.ResolverError, OSError) as e:
        if isinstance(e, OSError):
            err = ApplyError("backend_failure", f"filesystem operation failed ({e.errno})")
        else:
            err = e if isinstance(e, ApplyError) else ApplyError(e.code, getattr(e, "message", str(e)))
        if journal is not None:
            try:
                journal.fail(err)      # no automatic recovery, no package rollback
            except (ApplyError, OSError):
                pass                   # journal stays at its last durable (non-terminal) phase: recovery_required
        raise err from None
    finally:
        lock.release()


def _reuse_or_clear_bundle(host, staging, tx, activation, metadata):
    """(bundle, manifest) of an existing, fully re-verified bundle of this exact
    transaction; (None, None) if there is none or a stale one was removed."""
    path = os.path.join(staging, ps.bundle_name(pt.transaction_digest(tx)))
    if not os.path.lexists(path):
        return None, None
    try:
        manifest, _results = ps.verify_bundle(path, host.inspector(), tx=tx, expected={
            "transaction_digest": pt.transaction_digest(tx), "source_activation_digest": activation,
            "repository_metadata_digest": metadata})
        return path, manifest
    except ps.StagingError as e:
        if e.code != "stale_bundle":
            raise
    ps._remove_bundle_dir(path)          # stale bindings: entry-by-entry removal of a valid bundle layout
    return None, None


def _apply_cache(host, journal, bundle, manifest):
    """The attempt's APT archive cache: hard links to the verified bundle payloads
    (same root-owned filesystem; no copy, no download)."""
    cache = os.path.join(journal.dir, APPLY_CACHE)
    os.mkdir(cache, 0o700)
    src = _open_dir(os.path.join(bundle, ps.PAYLOAD_DIR))
    dst = _open_dir(cache)
    try:
        for p in manifest["payloads"]:
            os.link(p["filename"], p["filename"], src_dir_fd=src, dst_dir_fd=dst, follow_symlinks=False)
        os.fsync(dst)
    finally:
        os.close(src)
        os.close(dst)
    return cache


def apply_argv(manifest, cache, root="/"):
    """The one APT invocation: direct targets only, offline, archive cache = verified payloads."""
    ps.check_manifest(manifest)
    argv = list(APPLY_BASE_ARGV) + ["-o", f"{INTERNAL_OPTION}={cache}/"] + list(manifest["direct_targets"])
    if not allowed_apply_argv(argv, root):
        raise ApplyError("contract_failure", "apply argv is not the allowlisted shape")
    return argv


def status(host):
    """Read-only summary of the apply and recovery journals (root only, like apply)."""
    if host.euid() != 0:
        raise ApplyError("not_root", "journal status requires effective UID 0")
    pr = _pr
    resolved, _blocking = pr.resolution_state(host)
    return {"attempts": [{"attempt": a, "phase": p, "apply_started": s, "resolved_by_recovery": a in resolved}
                         for a, p, s in prior_attempts(host)],
            "recoveries": [{"recovery": n, "phase": d["phase"], "classification": d["classification"],
                            "attempt": (d["original"] or {}).get("attempt_id")} for n, d in pr.recovery_records(host)]}


# M1-L recovery journals gate new transactions (imported last: package_recovery
# imports this module and uses it only at call time).
import package_recovery as _pr  # noqa: E402
