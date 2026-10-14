"""LUBARCHY staged package acquisition (M1-J).

qualified package transaction (lubarchy.hardware.package-transaction/v1)
+ source-activation contract + authenticated metadata digest
-> exact payload acquisition -> payload verification -> finalized bundle
(lubarchy.packages.staged-bundle/v1).

The bundle is the payload set the future privileged executor would hand to
APT offline. Nothing here installs, configures or activates anything: there
is no apply operation, no dpkg install path and no shell. Production staging
writes only into a caller-owned workspace outside every system path; the
future root-owned staging root (STAGING_ROOT) is never written in M1-J.

Trust model (docs/package-staging.md):

- Payload names, versions and architectures come only from the transaction's
  install operations (its complete closure); targets are built internally.
- A payload is accepted only if it is a regular file with the expected APT
  file name, the SHA-256 and size from the authenticated Debian index (bound
  in the transaction), and dpkg-deb metadata (Package, Version, Architecture)
  matching the operation. No extra or missing payload is accepted.
- APT 3.0.3 does not re-hash a pre-existing file in its archive cache (it
  accepted a same-size corrupted payload in builder qualification), so this
  bundle verification is the authoritative integrity control and must run
  immediately before any future apply.
- The bundle binds the transaction digest, the source-activation digest and
  the authenticated repository metadata digest. Any change of any of them
  makes the bundle stale (restage), see final_revalidation().
"""

import hashlib
import os
import re
import stat
import subprocess
import tempfile

import package_transaction as pt
import resolver

SCHEMA_ID = "lubarchy.packages.staged-bundle/v1"
SCHEMA_FILE = "staged-bundle-v1.schema.json"

# Future root-owned staging root. M1-J never writes it.
STAGING_ROOT = "/var/lib/lubarchy/package-staging"
MANIFEST = "manifest.json"
PAYLOAD_DIR = "archives"
DIR_MODE = 0o755
FILE_MODE = 0o644
MAX_MANIFEST_BYTES = 1024 * 1024

APT_GET = pt.APT_GET
DPKG_DEB = "/usr/bin/dpkg-deb"
COMMAND_ENV = pt.COMMAND_ENV
ACQUIRE_TIMEOUT_SECONDS = 900

# The future offline apply (constructed, never executed in M1-J): direct
# targets only, no network, payloads only from the verified bundle.
OFFLINE_APPLY_BASE_ARGV = (APT_GET, "install", "--yes", "--quiet", "--no-install-recommends", "--no-remove",
                           "--no-download")
OFFLINE_FORBIDDEN = tuple(a for a in pt.FORBIDDEN_APT_ARGUMENTS if a not in ("--no-download", "-o"))

PROTECTED_PREFIXES = ("/etc", "/usr", "/boot", "/bin", "/sbin", "/lib", "/lib64", "/var/lib/apt", "/var/lib/dpkg",
                      "/var/cache/apt", "/var/lib/lubarchy", "/var/log", "/proc", "/sys", "/dev", "/run")
FILENAME = re.compile(r"^[a-z0-9][a-z0-9+.-]{0,127}_[0-9][A-Za-z0-9.+~%-]{0,140}_[a-z0-9][a-z0-9-]{0,31}\.deb$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")

EXIT_OK = 0
EXIT_MALFORMED = 2
EXIT_STALE = 3
EXIT_POLICY = 4
EXIT_UNSAFE = 5
EXIT_PAYLOAD = 6
EXIT_CONTRACT = 7
EXIT_USAGE = 64
ERROR_EXIT = {
    "input_malformed": EXIT_MALFORMED,
    "stale_bundle": EXIT_STALE,
    "unauthorised_source": EXIT_POLICY,
    "unsafe_path": EXIT_UNSAFE,
    "nothing_to_stage": EXIT_UNSAFE,
    "payload_mismatch": EXIT_PAYLOAD,
    "acquisition_failure": EXIT_PAYLOAD,
    "contract_failure": EXIT_CONTRACT,
    "backend_failure": EXIT_CONTRACT,
}


class StagingError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message

    @property
    def exit_status(self):
        return ERROR_EXIT.get(self.code, EXIT_CONTRACT)


# ---------------------------------------------------------------------------
# Expected payloads (from the transaction only)

def payload_filename(op):
    """APT's archive file name: name_version_arch.deb, epoch ':' encoded as %3a."""
    name = f"{op['package']}_{op['version'].replace(':', '%3a')}_{op['architecture']}.deb"
    if not FILENAME.match(name):
        raise StagingError("contract_failure", "operation does not map to a plain archive file name")
    return name


def acquisition_target(op):
    """Internally built exact acquisition target name:arch=version. Download
    targets select payloads only; they never become install targets, so they
    do not affect APT's automatic/manual marks."""
    if not pt.PACKAGE_NAME.match(op["package"]) or not pt.VERSION.match(op["version"]):
        raise StagingError("contract_failure", "operation is not a plain name/version")
    if not pt.ARCHITECTURE.match(op["architecture"]):
        raise StagingError("contract_failure", "operation architecture is malformed")
    return f"{op['package']}:{op['architecture']}={op['version']}"


def expected_payloads(tx):
    """The complete change set of a verified transaction: every install operation."""
    pt.check_transaction(tx)
    out = []
    for o in tx["operations"]:
        if o["action"] != "install":
            continue
        out.append({"package": o["package"], "architecture": o["architecture"], "version": o["version"],
                    "relation": o["relation"], "automatic_after": o["automatic_after"],
                    "filename": payload_filename(o), "size": o["size"], "sha256": o["sha256"],
                    "repositories": [{k: r[k] for k in ("origin", "label", "codename", "suite", "component")}
                                     for r in o["repositories"]]})
    names = [p["filename"] for p in out]
    if len(names) != len(set(names)) or len({(p["package"], p["architecture"]) for p in out}) != len(out):
        raise StagingError("contract_failure", "duplicate payload identity")
    if not out:
        raise StagingError("nothing_to_stage", "the transaction has no install operation")
    return sorted(out, key=lambda p: (p["package"], p["architecture"]))


# ---------------------------------------------------------------------------
# Commands (exact allowlist; shell=False; fixed environment)

def _allowed(argv):
    argv = list(argv)
    if any(not isinstance(a, str) or "\n" in a or "\0" in a for a in argv):
        return False
    if len(argv) == 4 and argv[:3] == [APT_GET, "--quiet", "download"]:
        name, _, rest = argv[3].partition(":")
        arch, _, ver = rest.partition("=")
        return bool(pt.PACKAGE_NAME.match(name) and pt.ARCHITECTURE.match(arch) and pt.VERSION.match(ver))
    if len(argv) == 6 and argv[:2] == [DPKG_DEB, "--field"] and argv[3:] == ["Package", "Version", "Architecture"]:
        return bool(re.fullmatch(r"/dev/fd/[0-9]{1,4}", argv[2]))
    return False


def _run(argv, env, cwd=None, pass_fds=(), timeout=ACQUIRE_TIMEOUT_SECONDS):
    if not _allowed(argv):
        raise StagingError("contract_failure", "command is not an allowlisted staging operation")
    try:
        r = subprocess.run(list(argv), env=env, cwd=cwd, shell=False, stdin=subprocess.DEVNULL, capture_output=True,
                           timeout=timeout, check=False, pass_fds=tuple(pass_fds))
    except (OSError, subprocess.TimeoutExpired):
        raise StagingError("backend_failure", f"{os.path.basename(argv[0])} could not run") from None
    return r.returncode, r.stdout.decode("utf-8", "replace")


class AptAcquirer:
    """Exact payload acquisition with an internally generated, isolated APT
    configuration (source_activation.isolated_update). Never caller options."""

    def __init__(self, apt_config):
        if not os.path.isabs(apt_config):
            raise StagingError("contract_failure", "APT configuration must be absolute")
        self._env = dict(COMMAND_ENV, APT_CONFIG=apt_config)

    def download(self, op, dest):
        rc, _out = _run([APT_GET, "--quiet", "download", acquisition_target(op)], self._env, cwd=dest)
        if rc != 0:
            raise StagingError("acquisition_failure", f"{op['package']} could not be acquired")


class DebInspector:
    """dpkg-deb control fields of an already-opened payload (by file descriptor,
    so the inspected inode is the hashed one). Reads metadata only; never
    unpacks or runs maintainer scripts."""

    def fields(self, fd):
        rc, out = _run([DPKG_DEB, "--field", f"/dev/fd/{fd}", "Package", "Version", "Architecture"],
                       dict(COMMAND_ENV), pass_fds=(fd,))
        if rc != 0:
            raise StagingError("payload_mismatch", "payload is not a readable Debian package")
        fields = {}
        for line in out.splitlines():
            k, sep, v = line.partition(": ")
            if sep:
                fields[k] = v.strip()
        return fields


# ---------------------------------------------------------------------------
# Paths

def _lstat(path):
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def check_staging_root(root):
    """A caller-owned workspace root: absolute, normalized, symlink-free, a
    directory owned by the caller, not group/other writable, outside every
    protected system path (including the future STAGING_ROOT)."""
    if not isinstance(root, str) or not os.path.isabs(root) or "\0" in root:
        raise StagingError("unsafe_path", "staging root must be an absolute path")
    norm = os.path.normpath(root)
    if norm != root.rstrip("/") or norm == "/":
        raise StagingError("unsafe_path", "staging root is not normalized")
    for p in PROTECTED_PREFIXES:
        if norm == p or norm.startswith(p + "/"):
            raise StagingError("unsafe_path", f"staging root may not be under {p}")
    cur = "/"
    for part in [x for x in norm.split("/") if x]:
        cur = os.path.join(cur, part)
        st = _lstat(cur)
        if st is None or stat.S_ISLNK(st.st_mode):
            raise StagingError("unsafe_path", f"{cur} is missing or a symlink")
    st = os.lstat(norm)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o022:
        raise StagingError("unsafe_path", "staging root must be a caller-owned directory, not group/other writable")
    return norm


def bundle_name(transaction_digest):
    """Bundle directory name: the transaction digest's 64 hex digits, nothing else."""
    if not isinstance(transaction_digest, str) or not transaction_digest.startswith("sha256:") \
            or not HEX64.match(transaction_digest[7:]):
        raise StagingError("input_malformed", "transaction digest is malformed")
    return transaction_digest[7:]


def _open_dir(path):
    try:
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise StagingError("unsafe_path", f"{os.path.basename(path)} is not a directory (or is a symlink)") from None


# ---------------------------------------------------------------------------
# Payload verification

def _hash_fd(fd):
    h, size = hashlib.sha256(), 0
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        b = os.read(fd, 1 << 20)
        if not b:
            break
        size += len(b)
        h.update(b)
    return h.hexdigest(), size


def verify_payloads(payload_dir, expected, inspector, check_modes=False):
    """Every expected payload exactly once, nothing else; each verified by
    file type, name, size, SHA-256 and dpkg-deb identity."""
    dfd = _open_dir(payload_dir)
    try:
        present = sorted(os.listdir(dfd))
        want = sorted(p["filename"] for p in expected)
        extra = sorted(set(present) - set(want))
        missing = sorted(set(want) - set(present))
        if extra:
            raise StagingError("payload_mismatch", f"unexpected payload(s): {', '.join(extra)}")
        if missing:
            raise StagingError("payload_mismatch", f"missing payload(s): {', '.join(missing)}")
        results = []
        for p in sorted(expected, key=lambda x: x["filename"]):
            try:
                fd = os.open(p["filename"], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0), dir_fd=dfd)
            except OSError:
                raise StagingError("payload_mismatch", f"{p['filename']} is not a regular file (or is a symlink)") \
                    from None
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode):
                    raise StagingError("payload_mismatch", f"{p['filename']} is not a regular file")
                if check_modes and (st.st_mode & 0o7777 != FILE_MODE or st.st_uid != os.geteuid()):
                    raise StagingError("payload_mismatch", f"{p['filename']} has an unexpected owner or mode")
                if st.st_size != p["size"]:
                    raise StagingError("payload_mismatch", f"{p['filename']}: size differs from the transaction")
                digest, size = _hash_fd(fd)
                if size != p["size"]:
                    raise StagingError("payload_mismatch", f"{p['filename']}: size changed while reading")
                if digest != p["sha256"]:
                    raise StagingError("payload_mismatch", f"{p['filename']}: SHA-256 differs from the transaction")
                fields = inspector.fields(fd)
            finally:
                os.close(fd)
            if fields.get("Package") != p["package"]:
                raise StagingError("payload_mismatch", f"{p['filename']}: package name differs")
            if fields.get("Version") != p["version"]:
                raise StagingError("payload_mismatch", f"{p['filename']}: version differs")
            if fields.get("Architecture") != p["architecture"]:
                raise StagingError("payload_mismatch", f"{p['filename']}: architecture differs")
            results.append({"filename": p["filename"], "package": p["package"], "architecture": p["architecture"],
                            "version": p["version"], "relation": p["relation"], "expected_sha256": p["sha256"],
                            "observed_sha256": digest, "expected_size": p["size"], "observed_size": size,
                            "deb_fields": fields})
        return results
    finally:
        os.close(dfd)


# ---------------------------------------------------------------------------
# Manifest

def _core(manifest):
    return {k: v for k, v in manifest.items() if k != "bundle_digest"}


def compute_bundle_digest(manifest):
    return resolver.digest(_core(manifest))


def build_manifest(tx, source_activation_digest, repository_metadata_digest):
    for d in (source_activation_digest, repository_metadata_digest):
        if not isinstance(d, str) or not d.startswith("sha256:") or not HEX64.match(d[7:]):
            raise StagingError("input_malformed", "bundle binding digest is malformed")
    payloads = expected_payloads(tx)
    m = {
        "schema": SCHEMA_ID,
        "transaction_schema": pt.TRANSACTION_SCHEMA_ID,
        "transaction_digest": pt.transaction_digest(tx),
        "repository_state_digest": tx["repository_state_digest"],
        "source_activation_digest": source_activation_digest,
        "repository_metadata_digest": repository_metadata_digest,
        "architecture": tx["architecture"],
        "payloads": payloads,
        "direct_targets": pt.direct_targets(tx["operations"], tx["architecture"]),
        "layout": {"manifest": MANIFEST, "payload_directory": PAYLOAD_DIR,
                   "directory_mode": "0755", "file_mode": "0644"},
        "state": "finalized",
        "safety": {"install_only": True, "explicit_targets": "direct_only",
                   "payload_trust": "sha256_size_from_authenticated_index", "deb_identity_verified": True,
                   "unsigned_local_repository": False, "trusted_option": False, "dpkg_install": False,
                   "network_during_apply": False, "applied": False},
    }
    m["bundle_digest"] = compute_bundle_digest(m)
    check_manifest(m)
    return m


def check_manifest(m):
    import json
    import jsonschema
    with open(os.path.join(resolver.SCHEMA_DIR, SCHEMA_FILE), encoding="utf-8") as f:
        schema = json.load(f)
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    if any(True for _ in validator.iter_errors(m)):
        raise StagingError("contract_failure", "manifest does not satisfy staged-bundle v1")
    if m["bundle_digest"] != compute_bundle_digest(m):
        raise StagingError("payload_mismatch", "manifest does not match its bundle digest")
    keys = [(p["package"], p["architecture"]) for p in m["payloads"]]
    if keys != sorted(set(keys)) or len({p["filename"] for p in m["payloads"]}) != len(keys):
        raise StagingError("contract_failure", "payloads not unique and sorted")
    for p in m["payloads"]:
        if p["filename"] != payload_filename(p) or p["automatic_after"] != (p["relation"] == "dependency"):
            raise StagingError("contract_failure", f"{p['package']}: payload entry inconsistent")
        if p["architecture"] not in (m["architecture"], "all"):
            raise StagingError("contract_failure", f"{p['package']}: foreign architecture")
    direct = sorted(f"{p['package']}:{p['architecture']}={p['version']}" for p in m["payloads"]
                    if p["relation"] == "direct")
    if sorted(m["direct_targets"]) != direct:
        raise StagingError("contract_failure", "direct targets are not exactly the direct payloads")
    return m


def manifest_bytes(m):
    return resolver.canonical_json(m) + b"\n"


# ---------------------------------------------------------------------------
# Staging

def _write_exclusive(dfd, name, data, mode):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), mode,
                 dir_fd=dfd)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def _remove_bundle_dir(path, allow_empty_dirs=False):
    """Strict removal of a bundle/temporary directory: only manifest.json and
    archives/ containing non-directory entries are removed, entry by entry
    through directory descriptors (a symlink is unlinked, never followed).
    Anything else aborts. allow_empty_dirs (own private temporary directory
    only) additionally removes empty directories with a non-recursive rmdir."""
    dfd = _open_dir(path)
    try:
        for name in os.listdir(dfd):
            st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            if name == PAYLOAD_DIR and stat.S_ISDIR(st.st_mode):
                afd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dfd)
                try:
                    for entry in os.listdir(afd):
                        est = os.stat(entry, dir_fd=afd, follow_symlinks=False)
                        if stat.S_ISDIR(est.st_mode):
                            if not allow_empty_dirs:
                                raise StagingError("unsafe_path", "unexpected directory inside a bundle")
                            os.rmdir(entry, dir_fd=afd)
                            continue
                        os.unlink(entry, dir_fd=afd)
                finally:
                    os.close(afd)
                os.rmdir(name, dir_fd=dfd)
            elif name == MANIFEST and not stat.S_ISDIR(st.st_mode):
                os.unlink(name, dir_fd=dfd)
            else:
                raise StagingError("unsafe_path", f"unexpected entry {name} in a bundle directory")
    finally:
        os.close(dfd)
    os.rmdir(path)


def stage_bundle(tx, source_activation_digest, repository_metadata_digest, root, acquirer, inspector):
    """Acquire, verify and atomically finalize the bundle for `tx` under the
    caller-owned workspace `root`. Returns (bundle_path, manifest, results)."""
    return _stage_into(check_staging_root(root), tx, source_activation_digest, repository_metadata_digest,
                       acquirer, inspector)


def _stage_into(root, tx, source_activation_digest, repository_metadata_digest, acquirer, inspector):
    """Staging into an already validated root (a checked caller workspace, or
    since M1-K the root-owned STAGING_ROOT validated by package_apply)."""
    manifest = build_manifest(tx, source_activation_digest, repository_metadata_digest)
    final = os.path.join(root, bundle_name(manifest["transaction_digest"]))
    if _lstat(final) is not None:
        raise StagingError("unsafe_path", "a bundle for this transaction already exists")
    old = os.umask(0o077)
    try:
        tmp = tempfile.mkdtemp(prefix=".staging-", dir=root)
    finally:
        os.umask(old)
    try:
        payload_dir = os.path.join(tmp, PAYLOAD_DIR)
        os.mkdir(payload_dir, 0o700)
        for p in manifest["payloads"]:
            acquirer.download(p, payload_dir)
        results = verify_payloads(payload_dir, manifest["payloads"], inspector)
        dfd = _open_dir(tmp)
        try:
            _write_exclusive(dfd, MANIFEST, manifest_bytes(manifest), FILE_MODE)
        finally:
            os.close(dfd)
        afd = _open_dir(payload_dir)
        try:
            for name in os.listdir(afd):
                os.chmod(name, FILE_MODE, dir_fd=afd, follow_symlinks=False)
            os.fsync(afd)
        finally:
            os.close(afd)
        os.chmod(payload_dir, DIR_MODE)
        os.chmod(tmp, DIR_MODE)
        if _lstat(final) is not None:
            raise StagingError("unsafe_path", "a bundle for this transaction appeared while staging")
        os.rename(tmp, final)                          # atomic finalization
        rfd = _open_dir(root)
        try:
            os.fsync(rfd)
        finally:
            os.close(rfd)
    except BaseException:
        if _lstat(tmp) is not None:
            try:
                _remove_bundle_dir(tmp, allow_empty_dirs=True)
            except (OSError, StagingError):
                pass          # residue stays in the caller's workspace as .staging-*; the original error wins
        raise
    verify_bundle(final, inspector, expected={"transaction_digest": manifest["transaction_digest"]}, tx=tx)
    return final, manifest, results


def read_manifest(bundle):
    dfd = _open_dir(bundle)
    try:
        try:
            fd = os.open(MANIFEST, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0), dir_fd=dfd)
        except OSError:
            raise StagingError("payload_mismatch", "manifest is missing or not a regular file") from None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise StagingError("payload_mismatch", "manifest is not a regular file")
            data = os.read(fd, MAX_MANIFEST_BYTES + 1)
        finally:
            os.close(fd)
    finally:
        os.close(dfd)
    if len(data) > MAX_MANIFEST_BYTES:
        raise StagingError("input_malformed", "manifest too large")
    try:
        m = resolver.parse_strict(data, label="manifest")
    except resolver.ResolverError:
        raise StagingError("payload_mismatch", "manifest is not strict JSON") from None
    if data != manifest_bytes(m):
        raise StagingError("payload_mismatch", "manifest is not canonical")
    return m


def verify_bundle(bundle, inspector, expected=None, tx=None):
    """Verify a finalized bundle: layout, manifest (schema, canonical bytes,
    bundle digest), binding digests, and every payload. `expected` may carry
    transaction_digest / source_activation_digest / repository_metadata_digest;
    `tx` (when given) must produce exactly the manifest's payload list."""
    st = _lstat(bundle)
    if st is None or not stat.S_ISDIR(st.st_mode):
        raise StagingError("unsafe_path", "bundle is not a directory (or is a symlink)")
    if st.st_mode & 0o022 or st.st_uid != os.geteuid():
        raise StagingError("payload_mismatch", "bundle directory has an unexpected owner or mode")
    entries = sorted(os.listdir(bundle))
    if entries != sorted([MANIFEST, PAYLOAD_DIR]):
        raise StagingError("payload_mismatch", "bundle layout differs (extra or missing entries)")
    m = read_manifest(bundle)
    check_manifest(m)
    if os.path.basename(os.path.normpath(bundle)) != bundle_name(m["transaction_digest"]):
        raise StagingError("payload_mismatch", "bundle directory name does not match its transaction digest")
    for key, value in (expected or {}).items():
        if key not in ("transaction_digest", "source_activation_digest", "repository_metadata_digest"):
            raise StagingError("input_malformed", f"unsupported expectation {key}")
        if m[key] != value:
            raise StagingError("stale_bundle", f"{key} differs from the bundle")
    if tx is not None:
        if pt.transaction_digest(tx) != m["transaction_digest"]:
            raise StagingError("stale_bundle", "transaction differs from the bundle")
        if expected_payloads(tx) != m["payloads"]:
            raise StagingError("payload_mismatch", "bundle payload list differs from the transaction")
    pst = os.lstat(os.path.join(bundle, PAYLOAD_DIR))
    if not stat.S_ISDIR(pst.st_mode) or pst.st_mode & 0o022:
        raise StagingError("payload_mismatch", "payload directory has an unexpected mode")
    results = verify_payloads(os.path.join(bundle, PAYLOAD_DIR), m["payloads"], inspector, check_modes=True)
    return m, results


# ---------------------------------------------------------------------------
# Freshness (final revalidation rule)

def final_revalidation(manifest, fresh):
    """Immediately before any future apply the executor recomputes, from
    fresh state: the package transaction (fresh probe, resolve, installed
    state, refreshed authenticated metadata), the source-activation contract
    and the repository metadata digest. The staged bundle is acceptable only
    if all three equal the bundle's bindings; otherwise it is stale and must
    be discarded and restaged (a newer security update, a moved mirror or any
    closure change). Staging prevents payload substitution; fresh resolution
    prevents installing a superseded transaction."""
    keys = ("transaction_digest", "source_activation_digest", "repository_metadata_digest")
    if set(fresh) != set(keys):
        raise StagingError("input_malformed", "final revalidation needs exactly the three fresh digests")
    changed = [k for k in keys if fresh[k] != manifest[k]]
    if changed:
        raise StagingError("stale_bundle", f"restage required: {', '.join(changed)} changed")
    return "accept"


# ---------------------------------------------------------------------------
# Future offline apply boundary (constructed and tested only; never executed)

def offline_apply_argv(manifest, bundle):
    """Future executor argv: direct targets only, --no-download, APT's archive
    cache pointed at the verified bundle payload directory. Dependencies stay
    APT-selected (automatic). M1-J never runs it."""
    check_manifest(manifest)
    if not os.path.isabs(bundle) or os.path.basename(os.path.normpath(bundle)) != \
            bundle_name(manifest["transaction_digest"]):
        raise StagingError("unsafe_path", "bundle path does not belong to this manifest")
    argv = list(OFFLINE_APPLY_BASE_ARGV) + ["-o", f"Dir::Cache::archives={os.path.normpath(bundle)}/{PAYLOAD_DIR}/"]
    for t in manifest["direct_targets"]:
        name, _, rest = t.partition(":")
        arch, _, ver = rest.partition("=")
        if not (pt.PACKAGE_NAME.match(name) and arch in (manifest["architecture"], "all") and pt.VERSION.match(ver)):
            raise StagingError("contract_failure", "direct target is not a plain name:arch=version")
        argv.append(t)
    for a in argv:
        if a in OFFLINE_FORBIDDEN:
            raise StagingError("contract_failure", f"forbidden APT argument {a}")
    return argv


# ---------------------------------------------------------------------------
# Cleanup (test/evidence workspaces only)

def remove_bundle(root, transaction_digest):
    """Remove exactly root/<transaction hex> in a caller-owned workspace (never
    STAGING_ROOT or a system path), entry by entry, without following links."""
    root = check_staging_root(root)
    path = os.path.join(root, bundle_name(transaction_digest))
    st = _lstat(path)
    if st is None:
        return False
    if not stat.S_ISDIR(st.st_mode):
        raise StagingError("unsafe_path", "bundle path is not a directory")
    _remove_bundle_dir(path)
    return True
