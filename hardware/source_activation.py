"""LUBARCHY Debian source activation contract (M1-J).

Realises the accepted Debian source policy (D-013: Debian `main` +
`non-free-firmware`, suites trixie / trixie-updates / trixie-security,
official Debian infrastructure, Debian archive keyring) as a deterministic,
verifiable plan: inspect the APT source state -> normalized pre-state ->
lubarchy.packages.source-activation/v1 contract -> rendered LUBARCHY-owned
deb822 source file.

This module never writes the system. It reads /etc/apt (read-only, without
following symlinks), renders the owned source file into caller-supplied
temporary directories only, and runs `apt-get update` only against an
isolated APT configuration inside a workspace. There is no activate
operation: writing /etc/apt is future privileged work.

Ownership model (docs/package-staging.md):

- LUBARCHY owns exactly one file, /etc/apt/sources.list.d/lubarchy-debian.sources.
  Every other source file is foreign (installer- or administrator-owned) and
  is never modified.
- Recognised topologies:
  * complement_foreign_baseline: foreign sources define exactly the three
    authorised Debian suites with policy components only (the Debian
    installer's layout); the owned file adds the missing policy components
    (in practice `non-free-firmware`) for the same URIs, with Signed-By.
  * owned_complete: no foreign Debian sources; the owned file defines all
    suites with `main non-free-firmware` over https.
- Anything else fails closed: other hosts, suites, components or options,
  any `trusted` option, deb-src, cdrom, symlinks or non-regular files, malformed
  files, duplicate targets, mixed URI schemes for one archive, partial
  foreign topologies.
"""

import hashlib
import os
import re
import stat
import subprocess

import package_transaction as pt
import resolver

SCHEMA_ID = "lubarchy.packages.source-activation/v1"
SCHEMA_FILE = "source-activation-v1.schema.json"

POLICY_DECISION = "D-013"
POLICY_COMPONENTS = tuple(pt.AUTHORISED_COMPONENTS)            # ("main", "non-free-firmware")
ARCHIVES = (  # (host, path, suites) -- official Debian infrastructure only
    ("deb.debian.org", "/debian", ("trixie", "trixie-updates")),
    ("security.debian.org", "/debian-security", ("trixie-security",)),
)
SCHEMES = ("http", "https")
DEFAULT_SCHEME = "https"                                        # as build/live/auto/config
KEYRING = "/usr/share/keyrings/debian-archive-keyring.pgp"
ACCEPTED_SIGNED_BY = tuple(pt.AUTHORISED_SIGNED_BY)            # .gpg is Debian's compatibility symlink

# Paths relative to the inspected root ("/" in production; a fixture root in tests).
SOURCE_LIST = "etc/apt/sources.list"
SOURCE_PARTS = "etc/apt/sources.list.d"
OWNED_NAME = "lubarchy-debian.sources"
OWNED_PATH = "/" + SOURCE_PARTS + "/" + OWNED_NAME
OWNED_MODE = "0644"
OWNED_OWNER = "root:root"
# APT's default Dir::Ignore-Files-Silently (APT 3.0.3): such files are never read by APT.
IGNORED_SUFFIXES = re.compile(r"(~|\.disabled|\.bak|\.dpkg-[a-z]+|\.ucf-[a-z]+|\.save|\.orig|\.distUpgrade)$")
MAX_SOURCE_BYTES = 256 * 1024

# Paths a workspace may never be inside (system package-manager and boot state).
PROTECTED_PREFIXES = ("/etc", "/usr", "/boot", "/bin", "/sbin", "/lib", "/lib64", "/var/lib/apt", "/var/lib/dpkg",
                      "/var/cache/apt", "/var/lib/lubarchy", "/var/log", "/proc", "/sys", "/dev", "/run")

APT_GET = pt.APT_GET
DPKG = pt.DPKG
COMMAND_ENV = pt.COMMAND_ENV
UPDATE_TIMEOUT_SECONDS = 900

EXIT_OK = 0
EXIT_MALFORMED = 2
EXIT_STALE = 3
EXIT_POLICY = 4
EXIT_AMBIGUOUS = 5
EXIT_METADATA = 6
EXIT_CONTRACT = 7
EXIT_USAGE = 64
ERROR_EXIT = {
    "input_malformed": EXIT_MALFORMED,
    "malformed_source": EXIT_MALFORMED,
    "stale_activation": EXIT_STALE,
    "unauthorised_source": EXIT_POLICY,
    "trusted_rejected": EXIT_POLICY,
    "signed_by_missing": EXIT_POLICY,
    "ambiguous_ownership": EXIT_AMBIGUOUS,
    "unsafe_path": EXIT_AMBIGUOUS,
    "metadata_failure": EXIT_METADATA,
    "contract_failure": EXIT_CONTRACT,
    "backend_failure": EXIT_CONTRACT,
}


class SourceError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message

    @property
    def exit_status(self):
        return ERROR_EXIT.get(self.code, EXIT_CONTRACT)


# ---------------------------------------------------------------------------
# Safe reading

def _lstat(path):
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        raise SourceError("unsafe_path", f"{path} cannot be inspected") from None


def _no_symlink_components(path):
    """Every existing component of an absolute path must be a real directory/file, not a symlink."""
    cur = "/"
    for part in [p for p in path.split("/") if p]:
        cur = os.path.join(cur, part)
        st = _lstat(cur)
        if st is None:
            return
        if stat.S_ISLNK(st.st_mode):
            raise SourceError("unsafe_path", f"{cur} is a symlink")


def read_regular(path, limit=MAX_SOURCE_BYTES):
    """Read a regular file without following symlinks (O_NOFOLLOW + fstat)."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        raise SourceError("unsafe_path", f"{os.path.basename(path)} cannot be opened as a regular file") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise SourceError("unsafe_path", f"{os.path.basename(path)} is not a regular file")
        chunks, total = [], 0
        while True:
            b = os.read(fd, 65536)
            if not b:
                break
            total += len(b)
            if total > limit:
                raise SourceError("malformed_source", f"{os.path.basename(path)} is too large")
            chunks.append(b)
        return b"".join(chunks)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Parsing

def _archive_of(uri):
    """Validated (scheme, host, path, suites) for an official Debian archive URI, or raise."""
    m = re.fullmatch(r"([A-Za-z][A-Za-z0-9+.-]*)://([^/@:?#\s]+)(/[^?#\s]*)?", uri)
    if not m:
        raise SourceError("unauthorised_source", "source URI is not a plain http(s) Debian archive URI")
    scheme, host, path = m.group(1).lower(), m.group(2).lower(), (m.group(3) or "").rstrip("/")
    for h, p, suites in ARCHIVES:
        if host == h and path == p:
            if scheme not in SCHEMES:
                raise SourceError("unauthorised_source", f"{host}: scheme {scheme} is not accepted")
            return scheme, host, path, suites
    raise SourceError("unauthorised_source", f"{host}{path} is not an authorised Debian archive")


def _entry(uri, suite, components, signed_by):
    scheme, host, path, suites = _archive_of(uri)
    if suite not in suites:
        raise SourceError("unauthorised_source", f"suite {suite} is not authorised for {host}")
    comps = sorted(set(components))
    if not comps or len(comps) != len(components):
        raise SourceError("malformed_source", f"{suite}: empty or repeated component list")
    for c in comps:
        if c not in POLICY_COMPONENTS:
            raise SourceError("unauthorised_source", f"component {c} is not authorised")
    if signed_by is not None and signed_by not in ACCEPTED_SIGNED_BY:
        raise SourceError("unauthorised_source", "Signed-By is not the Debian archive keyring")
    return {"uri": f"{scheme}://{host}{path}", "suite": suite, "components": comps, "signed_by": signed_by}


def parse_one_line(text):
    """sources.list(5) one-line format -> entries. Only plain `deb` lines with
    an optional [signed-by=KEYRING] are accepted."""
    entries = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.fullmatch(r"(\S+)\s+(?:\[([^\]]*)\]\s+)?(\S+)\s+(\S+)((?:\s+\S+)*)", line)
        if not m:
            raise SourceError("malformed_source", "unparseable sources.list line")
        kind, opts, uri, suite, comps = m.groups()
        if kind != "deb":
            raise SourceError("malformed_source", f"unsupported source type {kind}")
        signed_by = None
        for opt in (opts or "").split():
            key, sep, value = opt.partition("=")
            key = key.lower()
            if not sep:
                raise SourceError("malformed_source", "malformed source option")
            if key == "trusted":
                raise SourceError("trusted_rejected", "trusted= is never accepted")
            if key != "signed-by" or signed_by is not None:
                raise SourceError("malformed_source", f"unsupported source option {key}")
            signed_by = value
        if uri.startswith("cdrom:"):
            raise SourceError("malformed_source", "cdrom sources are not supported")
        entries.append(_entry(uri, suite, comps.split(), signed_by))
    return entries


DEB822_FIELDS = ("Types", "URIs", "Suites", "Components", "Signed-By")


def parse_deb822(text):
    """sources.list(5) deb822 format -> entries. Fields limited to Types (deb),
    URIs, Suites, Components and Signed-By (a keyring path)."""
    entries = []
    stanzas, cur = [], []
    for raw in text.splitlines():
        if raw.startswith("#"):
            continue
        if not raw.strip():
            if cur:
                stanzas.append(cur)
                cur = []
            continue
        cur.append(raw)
    if cur:
        stanzas.append(cur)
    for st in stanzas:
        fields = {}
        for line in st:
            if line[0] in " \t":
                raise SourceError("malformed_source", "multi-line deb822 values are not supported")
            key, sep, value = line.partition(":")
            if not sep or not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", key):
                raise SourceError("malformed_source", "malformed deb822 field")
            canon = {f.lower(): f for f in DEB822_FIELDS}.get(key.lower())
            if key.lower() == "trusted":
                raise SourceError("trusted_rejected", "Trusted: is never accepted")
            if canon is None:
                raise SourceError("malformed_source", f"unsupported deb822 field {key}")
            if canon in fields:
                raise SourceError("malformed_source", f"duplicate deb822 field {canon}")
            fields[canon] = value.strip()
        missing = [f for f in ("Types", "URIs", "Suites", "Components") if not fields.get(f)]
        if missing:
            raise SourceError("malformed_source", f"deb822 stanza lacks {', '.join(missing)}")
        if fields["Types"].split() != ["deb"]:
            raise SourceError("malformed_source", "only Types: deb is supported")
        signed_by = fields.get("Signed-By")
        if signed_by is not None and (not signed_by or len(signed_by.split()) != 1):
            raise SourceError("malformed_source", "Signed-By must be one keyring path")
        for uri in fields["URIs"].split():
            for suite in fields["Suites"].split():
                entries.append(_entry(uri, suite, fields["Components"].split(), signed_by))
    return entries


# ---------------------------------------------------------------------------
# Inspection -> normalized pre-state

def inspect(root="/"):
    """Normalized APT source state of `root` (read-only). Production uses "/";
    tests pass a fixture root. Raises SourceError on anything unrecognised."""
    root = os.path.abspath(root)
    _no_symlink_components(os.path.join(root, SOURCE_PARTS))
    files, ignored = [], []
    sl = os.path.join(root, SOURCE_LIST)
    st = _lstat(sl)
    if st is not None:
        if not stat.S_ISREG(st.st_mode):
            raise SourceError("unsafe_path", "sources.list is not a regular file")
        files.append(("/" + SOURCE_LIST, "foreign", "one-line", read_regular(sl)))
    parts = os.path.join(root, SOURCE_PARTS)
    st = _lstat(parts)
    if st is not None:
        if not stat.S_ISDIR(st.st_mode):
            raise SourceError("unsafe_path", "sources.list.d is not a directory")
        for name in sorted(os.listdir(parts)):
            p = os.path.join(parts, name)
            pst = _lstat(p)
            if pst is None:
                continue
            if IGNORED_SUFFIXES.search(name):
                ignored.append(name)
                continue
            if not stat.S_ISREG(pst.st_mode):
                raise SourceError("unsafe_path", f"sources.list.d/{name} is not a regular file")
            if name.endswith(".list"):
                fmt = "one-line"
            elif name.endswith(".sources"):
                fmt = "deb822"
            else:
                raise SourceError("ambiguous_ownership", f"sources.list.d/{name} has an unrecognised name")
            files.append(("/" + SOURCE_PARTS + "/" + name, "owned" if name == OWNED_NAME else "foreign", fmt,
                          read_regular(p)))
    out = []
    for path, cls, fmt, data in files:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise SourceError("malformed_source", f"{path} is not UTF-8") from None
        entries = parse_one_line(text) if fmt == "one-line" else parse_deb822(text)
        rec = {"path": path, "class": cls, "format": fmt,
               "entries": sorted(entries, key=lambda e: (e["uri"], e["suite"], e["components"]))}
        if cls == "owned":
            rec["sha256"] = hashlib.sha256(data).hexdigest()
        out.append(rec)
    return {"files": out, "ignored_files": ignored}


def pre_state_digest(state):
    return resolver.digest(state)


# ---------------------------------------------------------------------------
# Compilation

def _archive_key(uri):
    _scheme, host, path, _suites = _archive_of(uri)
    return host + path


def _effective_targets(state):
    """(uri, suite, component) -> list of (path, signed_by); rejects duplicates and scheme mixing."""
    targets, schemes = {}, {}
    for f in state["files"]:
        for e in f["entries"]:
            schemes.setdefault(_archive_key(e["uri"]), set()).add(e["uri"].split("://")[0])
            for c in e["components"]:
                key = (e["uri"], e["suite"], c)
                if key in targets:
                    raise SourceError("ambiguous_ownership", f"{e['suite']}/{c} is defined more than once")
                targets[key] = (f["path"], f["class"], e["signed_by"])
    for archive, s in schemes.items():
        if len(s) != 1:
            raise SourceError("ambiguous_ownership", f"{archive} is configured with mixed URI schemes")
    return targets


def policy():
    return {"decision": POLICY_DECISION, "components": list(POLICY_COMPONENTS),
            "archives": [{"host": h, "path": p, "suites": list(s)} for h, p, s in ARCHIVES],
            "schemes": list(SCHEMES), "default_scheme": DEFAULT_SCHEME,
            "signed_by": list(ACCEPTED_SIGNED_BY), "keyring": KEYRING}


def compile_activation(state, architecture, keyring_sha256):
    """Pre-state -> source-activation contract (deterministic, no timestamps)."""
    if not pt.ARCHITECTURE.match(architecture or ""):
        raise SourceError("backend_failure", "unexpected native architecture")
    if not re.fullmatch(r"[0-9a-f]{64}", keyring_sha256 or ""):
        raise SourceError("contract_failure", "Debian archive keyring identity unavailable")
    targets = _effective_targets(state)
    foreign = {k: v for k, v in targets.items() if v[1] == "foreign"}
    owned_file = next((f for f in state["files"] if f["class"] == "owned"), None)

    covered = {(_archive_key(u), s) for (u, s, _c) in foreign}
    required = {(h + p, s) for h, p, suites in ARCHIVES for s in suites}
    if covered and covered != required:
        raise SourceError("ambiguous_ownership", "foreign sources cover only part of the Debian suites")
    model = "complement_foreign_baseline" if covered else "owned_complete"

    # Per archive: one URI (the foreign one when foreign sources define the
    # archive, else https). APT merges Signed-By per (URI, suite), so the
    # effective keyring is decided per suite: an explicit foreign value, else
    # the owned stanza's keyring; a suite with neither fails closed.
    stanzas, post = [], []
    for host, path, suites in ARCHIVES:
        key = host + path
        f_uris = {u for (u, _s, _c) in foreign if _archive_key(u) == key}
        uri = next(iter(f_uris)) if f_uris else f"{DEFAULT_SCHEME}://{host}{path}"
        groups = {}
        for s in suites:
            explicit = {sb for (u, su, _c), (_p, _cls, sb) in targets.items() if u == uri and su == s and sb}
            if len(explicit) > 1:
                raise SourceError("ambiguous_ownership", f"{s} has conflicting Signed-By values")
            have = {c for (u, su, c) in foreign if u == uri and su == s}
            missing = tuple(c for c in POLICY_COMPONENTS if c not in have)
            keyring = next(iter(explicit)) if explicit else (KEYRING if missing else None)
            if keyring is None:
                raise SourceError("signed_by_missing", f"{s}: no Signed-By binds the suite to the archive keyring")
            if missing:
                groups.setdefault((missing, keyring), []).append(s)
            for c in POLICY_COMPONENTS:
                post.append({"uri": uri, "suite": s, "component": c, "signed_by": keyring})
        for (missing, keyring), ss in groups.items():
            stanzas.append({"uri": uri, "suites": ss, "components": list(missing), "signed_by": keyring})

    rendered = render_entries(stanzas) if stanzas else None
    if rendered is None:
        if owned_file is not None:
            raise SourceError("ambiguous_ownership", "an owned source file exists but none is required")
        action = "already_satisfied"
    elif owned_file is None:
        action = "create_owned_source"
    elif owned_file["sha256"] == hashlib.sha256(rendered).hexdigest():
        action = "already_satisfied"
    else:
        action = "replace_owned_source"
    post.sort(key=lambda t: (t["uri"], t["suite"], t["component"]))
    contract = {
        "schema": SCHEMA_ID,
        "architecture": architecture,
        "source_policy": policy(),
        "keyring_sha256": keyring_sha256,
        "pre_state": state,
        "pre_state_digest": pre_state_digest(state),
        "model": model,
        "ownership": {"owned_path": OWNED_PATH, "owned_mode": OWNED_MODE, "owned_owner": OWNED_OWNER,
                      "foreign_paths": sorted(f["path"] for f in state["files"] if f["class"] == "foreign"),
                      "foreign_files_modified": False},
        "action": action,
        "owned_source": None if rendered is None else {
            "stanzas": stanzas, "sha256": hashlib.sha256(rendered).hexdigest(), "size": len(rendered)},
        "post_state": {"targets": post},
        "post_state_digest": resolver.digest(post),
        "safety": {"system_written": False, "foreign_files_modified": False, "trusted_option": False,
                   "signed_by_required": True, "keys_downloaded": False, "components": list(POLICY_COMPONENTS)},
    }
    check_activation(contract)
    return contract


def activation_digest(contract):
    return resolver.digest(contract)


def check_activation(contract):
    """Schema + semantic self-check of a source-activation contract."""
    import json
    import jsonschema
    with open(os.path.join(resolver.SCHEMA_DIR, SCHEMA_FILE), encoding="utf-8") as f:
        schema = json.load(f)
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    if any(True for _ in validator.iter_errors(contract)):
        raise SourceError("contract_failure", "contract does not satisfy source-activation v1")
    if contract["source_policy"] != policy():
        raise SourceError("unauthorised_source", "contract source policy is not the accepted Debian policy")
    if contract["pre_state_digest"] != pre_state_digest(contract["pre_state"]):
        raise SourceError("contract_failure", "pre-state digest mismatch")
    if contract["post_state_digest"] != resolver.digest(contract["post_state"]["targets"]):
        raise SourceError("contract_failure", "post-state digest mismatch")
    for t in contract["post_state"]["targets"]:
        _entry(t["uri"], t["suite"], [t["component"]], t["signed_by"])
    owned = contract["owned_source"]
    if owned is not None:
        for s in owned["stanzas"]:
            for suite in s["suites"]:
                _entry(s["uri"], suite, s["components"], s["signed_by"])
            if s["signed_by"] is None:
                raise SourceError("signed_by_missing", "owned source without Signed-By")
        data = render_entries(owned["stanzas"])
        if hashlib.sha256(data).hexdigest() != owned["sha256"] or len(data) != owned["size"]:
            raise SourceError("contract_failure", "owned source does not match its rendering")
    owned_pre = next((f for f in contract["pre_state"]["files"] if f["class"] == "owned"), None)
    if owned is None:
        consistent = contract["action"] == "already_satisfied" and owned_pre is None
    elif owned_pre is None:
        consistent = contract["action"] == "create_owned_source"
    else:
        consistent = contract["action"] == ("already_satisfied" if owned_pre["sha256"] == owned["sha256"]
                                            else "replace_owned_source")
    if not consistent:
        raise SourceError("contract_failure", "action inconsistent with the owned source and pre-state")
    try:
        resolver.canonical_json(contract)
    except resolver.ResolverError:
        raise SourceError("contract_failure", "contract is not canonical JSON") from None
    return contract


# ---------------------------------------------------------------------------
# Rendering

HEADER = (b"# Managed by LUBARCHY (lubarchy.packages.source-activation/v1). Do not edit.\n"
          b"# Debian source policy D-013: official Debian archives, Debian archive keyring,\n"
          b"# components main and non-free-firmware only.\n")


def render_entries(stanzas):
    """Canonical deb822 bytes: fixed header, fixed field order, one stanza per
    (URI, component set). Never takes caller text: every value is re-validated."""
    out = [HEADER]
    for s in stanzas:
        for suite in s["suites"]:
            _entry(s["uri"], suite, s["components"], s["signed_by"])
        if not s["signed_by"]:
            raise SourceError("signed_by_missing", "owned stanza without Signed-By")
        out.append(b"\n")
        out.append(("Types: deb\nURIs: %s\nSuites: %s\nComponents: %s\nSigned-By: %s\n" % (
            s["uri"], " ".join(s["suites"]), " ".join(s["components"]), s["signed_by"])).encode("ascii"))
    return b"".join(out)


def render(contract):
    """Rendered owned source bytes for a verified contract (None if no owned file is needed)."""
    check_activation(contract)
    return None if contract["owned_source"] is None else render_entries(contract["owned_source"]["stanzas"])


# ---------------------------------------------------------------------------
# Workspaces (never system paths)

def check_workspace(path, create=True):
    """An absolute, symlink-free directory outside every protected system
    prefix, not group/other writable. Created (0700) when absent."""
    if not isinstance(path, str) or not os.path.isabs(path) or "\0" in path:
        raise SourceError("unsafe_path", "workspace must be an absolute path")
    norm = os.path.normpath(path)
    if norm != path.rstrip("/") or norm == "/":
        raise SourceError("unsafe_path", "workspace path is not normalized")
    for p in PROTECTED_PREFIXES:
        if norm == p or norm.startswith(p + "/"):
            raise SourceError("unsafe_path", f"workspace may not be under {p}")
    _no_symlink_components(norm)
    st = _lstat(norm)
    if st is None:
        if not create:
            raise SourceError("unsafe_path", "workspace does not exist")
        parent = _lstat(os.path.dirname(norm))
        if parent is None or not stat.S_ISDIR(parent.st_mode):
            raise SourceError("unsafe_path", "workspace parent is not a directory")
        os.mkdir(norm, 0o700)
        st = os.lstat(norm)
    if not stat.S_ISDIR(st.st_mode):
        raise SourceError("unsafe_path", "workspace is not a directory")
    if st.st_uid != os.geteuid() or st.st_mode & 0o022:
        raise SourceError("unsafe_path", "workspace must be owned by the caller and not group/other writable")
    return norm


def write_new_file(directory, name, data, mode=0o644):
    """Create directory/name exclusively (no symlink following), fsync, then fsync the directory."""
    if "/" in name or name in (".", ".."):
        raise SourceError("unsafe_path", "invalid file name")
    dfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                     mode, dir_fd=dfd)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(dfd)
    finally:
        os.close(dfd)


def render_to(contract, output_dir):
    """Write the rendered owned source into an empty workspace directory (never /etc)."""
    data = render(contract)
    out = check_workspace(output_dir)
    if os.listdir(out):
        raise SourceError("unsafe_path", "output directory must be empty")
    if data is not None:
        write_new_file(out, OWNED_NAME, data)
    return None if data is None else os.path.join(out, OWNED_NAME)


# ---------------------------------------------------------------------------
# Isolated authenticated metadata (read-only for the system)

def _run(argv, env, cwd=None, timeout=UPDATE_TIMEOUT_SECONDS):
    allowed = ([APT_GET, "update"], [APT_GET, "indextargets"], [DPKG, "--print-architecture"])
    if list(argv) not in allowed:
        raise SourceError("contract_failure", "command is not an allowlisted operation")
    try:
        r = subprocess.run(list(argv), env=env, cwd=cwd, shell=False, stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise SourceError("backend_failure", f"{os.path.basename(argv[0])} could not run") from None
    return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace")


def native_architecture():
    rc, out = _run([DPKG, "--print-architecture"], dict(COMMAND_ENV))
    if rc != 0:
        raise SourceError("backend_failure", "dpkg --print-architecture failed")
    return out.strip()


def keyring_sha256(path=KEYRING):
    return hashlib.sha256(read_regular(path, limit=4 * 1024 * 1024)).hexdigest()


def isolated_config(contract, workspace, root="/"):
    """Build an isolated APT configuration in `workspace` that reproduces the
    contract's post-state: the foreign source files (read in place or copied
    byte-for-byte after O_NOFOLLOW checks) plus the rendered owned file, empty
    trusted.gpg.d (only Signed-By keyrings authenticate), no preferences, no
    hooks, private lists and cache. Returns the apt.conf path."""
    check_activation(contract)
    ws = check_workspace(workspace)
    if os.listdir(ws):
        raise SourceError("unsafe_path", "workspace must be empty")
    for d in ("etc/sources.list.d", "etc/apt.conf.d", "etc/preferences.d", "etc/trusted.gpg.d", "lists/partial",
              "cache/archives/partial", "log", "state"):
        os.makedirs(os.path.join(ws, d), mode=0o700)
    fresh = inspect(root)
    if pre_state_digest(fresh) != contract["pre_state_digest"]:
        raise SourceError("stale_activation", "source pre-state changed since the contract was compiled")
    source_list = "/dev/null"
    for f in fresh["files"]:
        if f["class"] != "foreign":
            continue
        src = os.path.join(os.path.abspath(root), f["path"].lstrip("/"))
        if f["path"] == "/" + SOURCE_LIST:
            write_new_file(os.path.join(ws, "etc"), "sources.list", read_regular(src))
            source_list = os.path.join(ws, "etc", "sources.list")
        else:
            write_new_file(os.path.join(ws, "etc/sources.list.d"), os.path.basename(f["path"]), read_regular(src))
    data = render(contract)
    if data is not None:
        write_new_file(os.path.join(ws, "etc/sources.list.d"), OWNED_NAME, data)
    conf = (f'Dir::Etc "{ws}/etc"; Dir::Etc::SourceList "{source_list}"; '
            f'Dir::Etc::SourceParts "{ws}/etc/sources.list.d"; Dir::Etc::Parts "{ws}/etc/apt.conf.d";\n'
            f'Dir::Etc::Preferences "/dev/null"; Dir::Etc::PreferencesParts "{ws}/etc/preferences.d";\n'
            f'Dir::Etc::TrustedParts "{ws}/etc/trusted.gpg.d";\n'
            f'Dir::State::Lists "{ws}/lists"; Dir::State::status "/var/lib/dpkg/status";\n'
            f'Dir::State::extended_states "/var/lib/apt/extended_states"; Dir::Cache "{ws}/cache";\n'
            f'Dir::Log "{ws}/log";\n'
            'Acquire::Languages "none"; Debug::NoLocking "true";\n'
            'Acquire::AllowInsecureRepositories "false"; Acquire::AllowDowngradeToInsecureRepositories "false";\n'
            'Acquire::AllowWeakRepositories "false";\n')
    write_new_file(ws, "apt.conf", conf.encode("utf-8"), mode=0o600)
    return os.path.join(ws, "apt.conf")


def _inrelease_for(filename):
    base = os.path.basename(filename)
    m = re.fullmatch(r"(.+_dists_[A-Za-z0-9.+-]+)_.+_Packages(?:\.[a-z0-9]+)?", base)
    if not m:
        raise SourceError("metadata_failure", "unexpected index file name")
    return os.path.join(os.path.dirname(filename), m.group(1) + "_InRelease")


EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def _inrelease_path(lists_dir, uri, suite):
    host_path = uri.split("://", 1)[1]
    return os.path.join(lists_dir, host_path.replace("/", "_") + "_dists_" + suite + "_InRelease")


def _empty_index_listed(lists_dir, uri, suite, component, architecture):
    """Release header fields if the (already authenticated) InRelease lists
    component's binary-<arch> Packages index with size 0 and the empty
    SHA-256; otherwise None."""
    try:
        text = read_regular(_inrelease_path(lists_dir, uri, suite), limit=1 << 26).decode("utf-8")
    except (SourceError, UnicodeDecodeError):
        return None
    section, found, header = None, False, {}
    for line in text.splitlines():
        if line and not line[0].isspace():
            section, _, value = line.partition(":")
            header.setdefault(section, value.strip())
            continue
        if section != "SHA256":
            continue
        parts = line.split()
        if len(parts) == 3 and parts[2] in (f"{component}/binary-{architecture}/Packages",
                                            f"updates/{component}/binary-{architecture}/Packages"):
            found = parts[0] == EMPTY_SHA256 and parts[1] == "0"
            if not found:
                return None
    ident = (header.get("Origin"), header.get("Label"), header.get("Codename"))
    return header if found and ident in pt.AUTHORISED_RELEASES and header.get("Codename") == suite else None


def metadata_evidence(contract, indextargets_text):
    """Authenticated metadata evidence of an isolated update: every Packages
    target must be an authorised, Trusted, Signed-By-keyring Debian target and
    the target set must equal the contract post-state exactly."""
    targets = pt.parse_indextargets(indextargets_text)
    want = {(t["uri"].split("://")[1], t["suite"], t["component"]) for t in contract["post_state"]["targets"]}
    got, records = set(), []
    for desc, t in sorted(targets.items()):
        if not pt.authorised_target(t):
            raise SourceError("metadata_failure", f"unauthenticated or unauthorised index target ({desc})")
        base = t.get("Base-URI", "")
        m = re.fullmatch(r"([a-z]+)://([^/]+)(/.*?)/dists/([^/]+)/", base)
        if not m:
            raise SourceError("metadata_failure", "unexpected index base URI")
        got.add((m.group(2) + m.group(3), m.group(4), t["Component"]))
        fn = t.get("Filename", "")
        records.append({"uri": f"{m.group(1)}://{m.group(2)}{m.group(3)}", "suite": m.group(4),
                        "component": t["Component"], "architecture": t.get("Architecture", ""),
                        "origin": t["Origin"], "label": t["Label"], "codename": t["Codename"],
                        "signed_by": t["Signed-By"],
                        "packages_sha256": hashlib.sha256(read_regular(fn, limit=1 << 30)).hexdigest(),
                        "inrelease_sha256": hashlib.sha256(read_regular(_inrelease_for(fn), limit=1 << 26)).hexdigest(),
                        "_filename": fn})
    if got - want:
        raise SourceError("metadata_failure", "authenticated index targets include targets outside the post-state")
    lists_dir = os.path.dirname(records[0]["_filename"]) if records else None
    for archive, suite, comp in sorted(want - got):
        # APT keeps no list file for an empty index, so it reports no target.
        # Accept that only if the authenticated InRelease of the suite lists
        # the component's Packages index as empty (size 0, SHA-256 of b"").
        uri = next(t["uri"] for t in contract["post_state"]["targets"]
                   if t["uri"].split("://")[1] == archive and t["suite"] == suite)
        header = None if lists_dir is None else _empty_index_listed(lists_dir, uri, suite, comp,
                                                                     contract["architecture"])
        if header is None:
            raise SourceError("metadata_failure", f"{suite}/{comp}: no authenticated index target")
        signed_by = next(t["signed_by"] for t in contract["post_state"]["targets"]
                         if t["uri"] == uri and t["suite"] == suite and t["component"] == comp)
        records.append({"uri": uri, "suite": suite, "component": comp, "architecture": contract["architecture"],
                        "origin": header["Origin"], "label": header["Label"], "codename": header["Codename"],
                        "signed_by": signed_by,
                        "packages_sha256": EMPTY_SHA256, "inrelease_sha256": hashlib.sha256(
                            read_regular(_inrelease_path(lists_dir, uri, suite), limit=1 << 26)).hexdigest(),
                        "empty_index": True})
    for r in records:
        r.pop("_filename", None)
        r.setdefault("empty_index", False)
    records.sort(key=lambda r: (r["uri"], r["suite"], r["component"], r["architecture"]))
    return {"targets": records, "repository_metadata_digest": resolver.digest(records)}


def isolated_update(contract, workspace, root="/"):
    """Isolated authenticated `apt-get update` for the contract's post-state.
    Writes only inside `workspace`; any warning or error fails closed."""
    conf = isolated_config(contract, workspace, root)
    env = dict(COMMAND_ENV, APT_CONFIG=conf)
    rc, out = _run([APT_GET, "update"], env, cwd=os.path.dirname(conf))
    bad = [line for line in out.splitlines() if line.startswith(("W:", "E:", "Err:"))]
    if rc != 0 or bad:
        raise SourceError("metadata_failure", f"isolated apt-get update failed or warned ({len(bad)} lines)")
    rc, text = _run([APT_GET, "indextargets"], env)
    if rc != 0:
        raise SourceError("metadata_failure", "apt-get indextargets failed")
    ev = metadata_evidence(contract, text)
    ev["update_output_lines"] = len(out.splitlines())
    return conf, ev


# ---------------------------------------------------------------------------
# Verification

def compile_system(root="/", architecture=None, keyring=None):
    """Fresh inspect + compile of the system (or a fixture root in tests)."""
    state = inspect(root)
    return compile_activation(state, architecture or native_architecture(),
                              keyring if keyring is not None else keyring_sha256())


def verify_activation(contract, root="/", architecture=None, keyring=None):
    """Recompile from fresh state; the contract is valid only if identical."""
    check_activation(contract)
    fresh = compile_system(root, architecture or contract["architecture"], keyring)
    if resolver.canonical_json(fresh) != resolver.canonical_json(contract):
        changed = sorted(k for k in contract if contract.get(k) != fresh.get(k))
        raise SourceError("stale_activation", f"source activation is stale: {', '.join(changed)} changed")
    return fresh
