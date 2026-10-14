"""LUBARCHY package transaction compiler (M1-I).

fresh local inventory (trusted probe) -> production resolver -> canonical
plan v1 -> install-only package transaction v1
(lubarchy.hardware.package-transaction/v1).

This module prepares and verifies transactions. It never mutates the system:
it installs, upgrades, removes and configures nothing, activates no package
source, takes no package-manager lock and has no 'apply' operation. APT is
used only through read-only queries and `apt-get --simulate`, which does not
lock and therefore proves resolution, not apply safety.

Authority model (docs/package-transactions.md):

- Package names come only from the freshly recomputed production plan.
  A caller may at most supply expected digests; an incoming plan or package
  list is never authority.
- Every package APT selects (direct requirement or dependency) must come
  from an authorised Debian repository identity and component, with its
  exact version, architecture, SHA-256 and size bound into the transaction.
- Install-only: any upgrade, downgrade, removal, purge, reinstall,
  architecture change or held-package change rejects the transaction.
- The transaction binds the inventory, rule set, plan, installed package
  state, held packages, package-manager configuration and repository state.
  Any drift makes it stale.

Commands run with a fixed absolute path, a fixed environment built from
scratch (no caller APT_CONFIG, DEBIAN_FRONTEND, PATH, proxy or locale),
shell=False and a timeout.
"""

import os
import re
import subprocess

import resolver

TRANSACTION_SCHEMA_ID = "lubarchy.hardware.package-transaction/v1"
SCHEMA_FILE = "package-transaction-v1.schema.json"

# Fixed executables (read-only use only).
APT_GET = "/usr/bin/apt-get"
APT_CACHE = "/usr/bin/apt-cache"
APT_CONFIG_BIN = "/usr/bin/apt-config"
DPKG = "/usr/bin/dpkg"
DPKG_QUERY = "/usr/bin/dpkg-query"
APT_MARK = "/usr/bin/apt-mark"
COMMAND_TIMEOUT_SECONDS = 300
COMMAND_ENV = {"LC_ALL": "C", "LANG": "C", "LANGUAGE": "C", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}

# Authorised Debian HWE source policy (ADR-0001 / D-013): official Debian
# archive identities, Debian 13 suites, components main + non-free-firmware.
AUTHORISED_COMPONENTS = tuple(sorted(resolver.AUTHORISED_SOURCES))
AUTHORISED_RELEASES = (  # (Origin, Label, Codename)
    ("Debian", "Debian", "trixie"),
    ("Debian", "Debian", "trixie-updates"),
    ("Debian", "Debian-Security", "trixie-security"),
)
AUTHORISED_SIGNED_BY = ("/usr/share/keyrings/debian-archive-keyring.gpg",
                        "/usr/share/keyrings/debian-archive-keyring.pgp")
STATUS_FILE_SOURCE = "/var/lib/dpkg/status"

# Package-manager configuration that must hold for a transaction to compile.
REQUIRED_CONFIG = {
    "Acquire::AllowInsecureRepositories": ("0", "false"),
    "Acquire::AllowDowngradeToInsecureRepositories": ("0", "false"),
}
FORBIDDEN_CONFIG_TRUE = ("APT::Get::AllowUnauthenticated", "APT::Get::Force-Yes")
CONFIG_TRUE = ("1", "true", "yes")

PACKAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9+.-]{1,127}$")
VERSION = re.compile(r"^(?:[0-9]+:)?[0-9][A-Za-z0-9.+~-]{0,127}$")
ARCHITECTURE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")

# Exit status contract of hardware/compile-transaction.py (also used by
# TransactionError.exit_status).
EXIT_READY = 0
EXIT_MALFORMED = 2
EXIT_STALE = 3
EXIT_POLICY = 4
EXIT_UNSAFE = 5
EXIT_PROVENANCE = 6
EXIT_CONTRACT = 7
EXIT_USAGE = 64
ERROR_EXIT = {
    "input_malformed": EXIT_MALFORMED,
    "stale_transaction": EXIT_STALE,
    "plan_rejected": EXIT_POLICY,
    "unauthorised_source": EXIT_POLICY,
    "unsafe_change_set": EXIT_UNSAFE,
    "provenance_failure": EXIT_PROVENANCE,
    "contract_failure": EXIT_CONTRACT,
    "backend_failure": EXIT_CONTRACT,
}

# Future transaction states. M1-I implements only 'prepared' (compile) and
# 'revalidated' (verify); there is no 'applying' implementation.
STATES = ("prepared", "revalidated", "applying", "applied", "validated", "rejected", "stale", "failed")
IMPLEMENTED_STATES = ("prepared", "revalidated", "rejected", "stale")

# Future serialization lock (documented contract; never created here).
TRANSACTION_LOCK = "/run/lubarchy/package-transaction.lock"

# Future apply argv policy (constructed, never executed in M1-I).
APPLY_BASE_ARGV = (APT_GET, "install", "--yes", "--quiet", "--no-install-recommends", "--no-remove")
FORBIDDEN_APT_ARGUMENTS = ("--allow-unauthenticated", "--allow-downgrades", "--allow-remove-essential",
                           "--allow-change-held-packages", "--force-yes", "--fix-broken", "-f",
                           "--allow-insecure-repositories", "--target-release", "-t", "-o", "-c",
                           "--option", "--config-file", "--reinstall", "--purge", "--auto-remove",
                           "--autoremove", "--install-recommends", "--install-suggests", "--assume-no",
                           "--download-only", "--simulate", "-s", "--dry-run", "--no-download")


class TransactionError(Exception):
    """A transaction that cannot be compiled or verified. Messages carry
    package names and check names only."""

    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message

    @property
    def exit_status(self):
        return ERROR_EXIT.get(self.code, EXIT_CONTRACT)


# ---------------------------------------------------------------------------
# Versions (dpkg ordering)

def _order(c):
    if c.isdigit():
        return 0
    if c.isalpha():
        return ord(c)
    if c == "~":
        return -1
    return ord(c) + 256 if c else 0


def _cmp_part(a, b):
    while a or b:
        na = re.match(r"^\D*", a).group(0)
        nb = re.match(r"^\D*", b).group(0)
        a, b = a[len(na):], b[len(nb):]
        for i in range(max(len(na), len(nb))):
            ca = _order(na[i]) if i < len(na) else 0
            cb = _order(nb[i]) if i < len(nb) else 0
            if ca != cb:
                return -1 if ca < cb else 1
        da = re.match(r"^\d*", a).group(0)
        db = re.match(r"^\d*", b).group(0)
        a, b = a[len(da):], b[len(db):]
        if int(da or 0) != int(db or 0):
            return -1 if int(da or 0) < int(db or 0) else 1
    return 0


def version_compare(a, b):
    def split(v):
        epoch, rest = (v.split(":", 1) if ":" in v else ("0", v))
        upstream, _, rev = rest.rpartition("-") if "-" in rest else (rest, "", "0")
        return int(epoch), upstream, rev
    ea, ua, ra = split(a)
    eb, ub, rb = split(b)
    if ea != eb:
        return -1 if ea < eb else 1
    return _cmp_part(ua, ub) or _cmp_part(ra, rb)


# ---------------------------------------------------------------------------
# Parsers for read-only APT/dpkg output (C locale)

def parse_installed(text):
    """dpkg-query -W -f '${Package}\\t${Architecture}\\t${Version}\\t${Status}\\n'
    -> (installed state, held packages). Holds are taken from every entry,
    including not-installed packages with a 'hold' selection."""
    state, holds = [], set()
    for line in text.splitlines():
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != 4:
            raise TransactionError("backend_failure", "unexpected dpkg-query output")
        name, arch, version, status = parts
        words = status.split()
        if len(words) != 3:
            raise TransactionError("backend_failure", "unexpected dpkg status")
        if words[0] == "hold":
            holds.add(f"{name}:{arch}")
        if words[2] == "not-installed":
            continue
        state.append({"package": name, "architecture": arch, "version": version, "status": status})
    state.sort(key=lambda e: (e["package"], e["architecture"]))
    keys = [(e["package"], e["architecture"]) for e in state]
    if len(keys) != len(set(keys)):
        raise TransactionError("backend_failure", "duplicate installed package entry")
    return state, sorted(holds)


def parse_auto(text, native):
    """apt-mark showauto -> set of (name, arch|None). Native/all packages are
    listed by name, foreign ones as name:arch."""
    out = set()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        name, _, arch = line.partition(":")
        if not PACKAGE_NAME.match(name) or (arch and not ARCHITECTURE.match(arch)):
            raise TransactionError("backend_failure", "unexpected apt-mark output")
        out.add((name, arch or None))
    return out


def apply_marks(installed, auto, native):
    """Annotate installed entries with APT's automatic/manual state."""
    for e in installed:
        foreign = e["architecture"] not in (native, "all")
        e["automatic"] = ((e["package"], e["architecture"]) in auto) if foreign else ((e["package"], None) in auto
                                                                                    or (e["package"], native) in auto)
    return installed


def parse_config(text):
    """apt-config dump -> {key: value} for scalar entries."""
    out = {}
    for line in text.splitlines():
        m = re.match(r'^(\S+) "(.*)";$', line)
        if m:
            out.setdefault(m.group(1), m.group(2))
    return out


HOOK_KEY = re.compile(r"(Pre-Invoke|Post-Invoke|Pre-Install-Pkgs|Post-Invoke-Success|Pre-Invoke-Success)", re.I)


def config_facts(text):
    """Security-relevant package-manager configuration (hooks and trust flags)."""
    lines = sorted({line for line in text.splitlines()
                    if HOOK_KEY.search(line.split(" ", 1)[0])
                    or line.split(" ", 1)[0] in REQUIRED_CONFIG or line.split(" ", 1)[0] in FORBIDDEN_CONFIG_TRUE
                    or line.split(" ", 1)[0].startswith(("APT::Get::Allow", "DPkg::Options", "Dpkg::Options",
                                                          "APT::Install-Recommends", "APT::Install-Suggests"))})
    return lines


def check_config(text):
    cfg = parse_config(text)
    for key, allowed in REQUIRED_CONFIG.items():
        if cfg.get(key, "0").lower() not in allowed:
            raise TransactionError("unauthorised_source", f"package-manager configuration {key} is not fail-closed")
    for key in FORBIDDEN_CONFIG_TRUE:
        if cfg.get(key, "0").lower() in CONFIG_TRUE:
            raise TransactionError("unauthorised_source", f"package-manager configuration {key} is enabled")


def parse_indextargets(text):
    """apt-get indextargets (deb822 records) -> {Description: record} for Packages targets."""
    out = {}
    for block in text.strip().split("\n\n"):
        rec = {}
        for line in block.splitlines():
            if ":" in line and not line.startswith(" "):
                k, _, v = line.partition(":")
                rec[k.strip()] = v.strip()
        if rec.get("Identifier") == "Packages" and "Description" in rec:
            out[rec["Description"]] = rec
    return out


def parse_policy(text):
    """apt-cache policy PKG... -> {pkg: {"installed", "candidate", "versions": {ver: [source...]}}}"""
    out, pkg, ver = {}, None, None
    for line in text.splitlines():
        m = re.match(r"^(\S+):$", line)
        if m:
            pkg = m.group(1)
            out[pkg] = {"installed": None, "candidate": None, "versions": {}}
            ver = None
            continue
        if pkg is None:
            continue
        m = re.match(r"^  (Installed|Candidate): (.+)$", line)
        if m:
            out[pkg][m.group(1).lower()] = None if m.group(2) == "(none)" else m.group(2)
            continue
        m = re.match(r"^ (?:\*\*\*|   ) (\S+) (-?\d+)$", line)
        if m:
            ver = m.group(1)
            out[pkg]["versions"][ver] = []
            continue
        m = re.match(r"^\s+(-?\d+) (\S.*)$", line)
        if m and ver is not None:
            out[pkg]["versions"][ver].append({"priority": int(m.group(1)), "source": m.group(2).strip()})
    return out


def parse_show(text):
    """apt-cache show PKG=VER -> list of stanzas (dicts)."""
    out = []
    for block in text.strip().split("\n\n"):
        rec, key = {}, None
        for line in block.splitlines():
            if line.startswith(" ") and key:
                continue
            k, _, v = line.partition(":")
            key = k
            rec[k] = v.strip()
        if "Package" in rec:
            out.append(rec)
    return out


SIM_LINE = re.compile(r"^(Inst|Remv|Purg|Conf) (\S+)(?: \[(\S+)\])?(?: \((\S+) (.*?)\[([a-z0-9-]+)\]\))?(.*)$")


def parse_simulation(text):
    """apt-get --simulate output -> list of {"op", "package", "architecture", "version", "old_version"}."""
    if re.search(r"^E: ", text, re.M):
        raise TransactionError("unsafe_change_set", "APT could not resolve the requirement set")
    acts = []
    for line in text.splitlines():
        if not line.startswith(("Inst ", "Remv ", "Purg ", "Conf ")):
            continue
        m = SIM_LINE.match(line)
        if not m:
            raise TransactionError("backend_failure", "unexpected simulation line")
        op, pkg, old, new, _origin, arch, _rest = m.groups()
        name, _, qual = pkg.partition(":")
        if op == "Conf":
            continue
        acts.append({"op": op, "package": name, "architecture": arch or qual or None,
                     "version": new, "old_version": old})
    return acts


# ---------------------------------------------------------------------------
# Backends

def read_only_operation(argv):
    """Exact allowlist of the read-only package-manager operations this module may run."""
    argv = list(argv)
    if any(not isinstance(a, str) or "\n" in a or "\0" in a for a in argv):
        return False
    exe, args = argv[0], argv[1:]
    if exe == DPKG:
        return args == ["--print-architecture"]
    if exe == APT_MARK:
        return args == ["showauto"]
    if exe == DPKG_QUERY:
        return len(args) == 3 and args[:2] == ["-W", "-f"]
    if exe == APT_CONFIG_BIN:
        return args == ["dump"]
    if exe == APT_CACHE:
        return len(args) >= 2 and args[0] in ("policy", "show") and all(not a.startswith("-") for a in args[1:])
    if exe == APT_GET:
        if args == ["indextargets"]:
            return True
        if args[:2] != ["--simulate", "--quiet"]:
            return False
        rest = args[2:]
        flags = [a for a in rest if a.startswith("-")]
        if set(flags) - {"--no-install-recommends", "--no-remove"} or "install" not in rest:
            return False
        return rest.index("install") == len(flags) and all(not a.startswith("-") for a in rest[rest.index("install") + 1:])
    return False


def run_read_only(argv, env, timeout=COMMAND_TIMEOUT_SECONDS):
    if not isinstance(argv, (list, tuple)) or not argv or not os.path.isabs(argv[0]):
        raise TransactionError("contract_failure", "commands must be argv lists with an absolute executable")
    if not read_only_operation(argv):
        raise TransactionError("contract_failure", "command is not an allowlisted read-only operation")
    try:
        r = subprocess.run(list(argv), env=env, shell=False, stdin=subprocess.DEVNULL, capture_output=True,
                           timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise TransactionError("backend_failure", f"{os.path.basename(argv[0])} could not run") from None
    try:
        out = r.stdout.decode("utf-8")
    except UnicodeDecodeError:
        raise TransactionError("backend_failure", "non-UTF-8 package-manager output") from None
    return r.returncode, out


class SystemBackend:
    """Read-only access to the system package manager.

    Production (hardware/compile-transaction.py) uses SystemBackend() with no
    argument: the system's own APT configuration and dpkg database, a fixed
    environment and fixed binaries. `apt_config` is never caller input: it is
    used for isolated builder qualification (tools/hwe/qualify-transaction.py)
    and, since M1-J, by hardware/stage-packages.py with the isolated
    configuration it generates itself from a verified source-activation
    contract (source_activation.isolated_update)."""

    def __init__(self, apt_config=None):
        self._env = dict(COMMAND_ENV)
        if apt_config is not None:
            if not os.path.isabs(apt_config):
                raise TransactionError("contract_failure", "qualification APT configuration must be absolute")
            self._env["APT_CONFIG"] = apt_config

    def environment(self):
        return dict(self._env)

    def _run(self, argv, ok=(0,)):
        rc, out = run_read_only(argv, self._env)
        if rc not in ok:
            raise TransactionError("backend_failure", f"{os.path.basename(argv[0])} exited {rc}")
        return out

    def architecture(self):
        return self._run([DPKG, "--print-architecture"]).strip()

    def installed_text(self):
        return self._run([DPKG_QUERY, "-W", "-f", "${Package}\\t${Architecture}\\t${Version}\\t${Status}\\n"])

    def auto_text(self):
        return self._run([APT_MARK, "showauto"])

    def config_text(self):
        return self._run([APT_CONFIG_BIN, "dump"])

    def indextargets_text(self):
        return self._run([APT_GET, "indextargets"])

    def policy_text(self, names):
        return self._run([APT_CACHE, "policy", *names])

    def show_text(self, name, version):
        return self._run([APT_CACHE, "show", f"{name}={version}"])

    def simulate_text(self, args):
        rc, out = run_read_only([APT_GET, "--simulate", "--quiet", *args], self._env)
        return out if rc == 0 else out + "\nE: apt-get exited non-zero\n"


# ---------------------------------------------------------------------------
# Plan acceptance

def accept_plan(plan):
    """Return the direct requirements [(name, source)] of an acceptable plan."""
    if plan.get("status") not in ("resolved", "partial"):
        raise TransactionError("plan_rejected", f"plan status {plan.get('status')} is not installable")
    if plan["conflicts"]:
        raise TransactionError("plan_rejected", "plan has conflicts")
    if plan["profiles"] or plan["warnings"]:
        raise TransactionError("plan_rejected", "plan profiles/warnings have no apply semantics yet")
    if plan["source_policy"] != {"authorised": list(AUTHORISED_COMPONENTS)}:
        raise TransactionError("unauthorised_source", "plan source policy is not the current production policy")
    reqs = []
    for p in plan["packages"]:
        if p["disposition"] != "require":
            raise TransactionError("plan_rejected", f"package effect {p['disposition']} for {p['name']} is not supported")
        if p.get("source_status") != "authorised" or p.get("source") not in AUTHORISED_COMPONENTS:
            raise TransactionError("unauthorised_source", f"{p['name']} source requires policy review")
        if not PACKAGE_NAME.match(p["name"]):
            raise TransactionError("plan_rejected", "plan package name is not a plain package name")
        reqs.append((p["name"], p["source"]))
    for s in plan["sources"]:
        if s["status"] != "authorised":
            raise TransactionError("unauthorised_source", f"source {s['source']} requires policy review")
    return sorted(reqs)


# ---------------------------------------------------------------------------
# Provenance

def _release_identity(target):
    return {"origin": target.get("Origin", ""), "label": target.get("Label", ""),
            "codename": target.get("Codename", ""), "suite": target.get("Suite", ""),
            "component": target.get("Component", ""), "base_uri": target.get("Base-URI", "")}


def authorised_target(target):
    ident = (target.get("Origin"), target.get("Label"), target.get("Codename"))
    return (ident in AUTHORISED_RELEASES and target.get("Component") in AUTHORISED_COMPONENTS
            and target.get("Trusted") == "yes" and target.get("Signed-By") in AUTHORISED_SIGNED_BY)


def provenance(name, version, policy, targets, show_text):
    """Authorised repository identity + SHA-256/size for name=version, or raise."""
    entry = policy.get(name)
    if entry is None or version not in entry["versions"]:
        raise TransactionError("provenance_failure", f"{name} {version} is not in the package universe")
    sources = [s["source"] for s in entry["versions"][version] if s["source"] != STATUS_FILE_SOURCE]
    if not sources:
        raise TransactionError("provenance_failure", f"{name} {version} is not available from any repository")
    identities = []
    for src in sources:
        target = targets.get(src)
        if target is None:
            raise TransactionError("provenance_failure", f"{name} {version}: repository index is not identifiable")
        if not authorised_target(target):
            raise TransactionError("unauthorised_source", f"{name} {version} is offered by an unauthorised repository")
        identities.append(_release_identity(target))
    stanzas = [s for s in parse_show(show_text) if s.get("Package") == name and s.get("Version") == version]
    hashes = {s.get("SHA256") for s in stanzas}
    sizes = {s.get("Size") for s in stanzas}
    archs = {s.get("Architecture") for s in stanzas}
    if len(hashes) != 1 or not SHA256.match(next(iter(hashes)) or "") or len(sizes) != 1 or len(archs) != 1:
        raise TransactionError("provenance_failure", f"{name} {version}: no single authenticated SHA-256/size")
    identities = sorted({tuple(sorted(i.items())) for i in identities})
    return {"repositories": [dict(i) for i in identities], "sha256": next(iter(hashes)),
            "size": int(next(iter(sizes))), "architecture": next(iter(archs))}


# ---------------------------------------------------------------------------
# Compilation

def _canon(value):
    return resolver.canonical_json(value)


def _digest(value):
    return resolver.digest(value)


def classify(acts, installed, native):
    """Install-only safety classification of an APT change set."""
    inst = {e["package"]: e for e in installed}
    unsafe = []
    ops = []
    for a in acts:
        if a["op"] in ("Remv", "Purg"):
            unsafe.append(("removal", a["package"]))
            continue
        arch = a["architecture"]
        if arch not in (native, "all"):
            unsafe.append(("architecture_change", a["package"]))
            continue
        cur = inst.get(a["package"])
        if a["old_version"] is not None or cur is not None:
            old = a["old_version"] or (cur or {}).get("version")
            if cur is not None and cur["architecture"] not in (arch, "all") and arch != "all":
                unsafe.append(("architecture_change", a["package"]))
            elif old is None:
                unsafe.append(("upgrade", a["package"]))
            else:
                c = version_compare(a["version"], old)
                unsafe.append(("upgrade" if c > 0 else "downgrade" if c < 0 else "reinstall", a["package"]))
            continue
        ops.append(a)
    return ops, sorted(set(unsafe))


def compile_transaction(inventory, rules, backend, expected=None):
    """Compile the install-only transaction for a fresh inventory.

    `inventory` must come from the trusted probe in production; `rules` from
    resolver.load_production_rules(); `backend` a SystemBackend. `expected`
    may carry caller-expected digests (inventory_digest, rule_set_digest,
    plan_digest); it never carries packages."""
    expected = expected or {}
    if set(expected) - {"inventory_digest", "rule_set_digest", "plan_digest", "transaction_digest"}:
        raise TransactionError("input_malformed", "unsupported expectation (only digests are accepted)")
    plan = resolver.check_plan(resolver.resolve(inventory, rules))
    if plan["status"] == "invalid":
        raise TransactionError("plan_rejected", "inventory rejected (plan status invalid)")
    plan_digest = _digest(plan)
    facts = {"inventory_digest": plan["inventory_digest"], "rule_set_digest": plan["rule_set_digest"],
             "plan_digest": plan_digest}
    for key, value in facts.items():
        if key in expected and expected[key] != value:
            raise TransactionError("stale_transaction", f"{key} differs from the freshly recomputed value")
    reqs = accept_plan(plan)

    native = backend.architecture()
    if not ARCHITECTURE.match(native):
        raise TransactionError("backend_failure", "unexpected native architecture")
    config = backend.config_text()
    check_config(config)
    installed, holds = parse_installed(backend.installed_text())
    installed = apply_marks(installed, parse_auto(backend.auto_text(), native), native)
    targets = parse_indextargets(backend.indextargets_text())

    names = [n for n, _ in reqs]
    ops, unsafe, closure_names = [], [], []
    satisfied = []
    if names:
        direct_policy = parse_policy(backend.policy_text(names))
        for n in names:
            if (direct_policy.get(n) or {}).get("candidate") is None:
                raise TransactionError("provenance_failure", f"{n} has no candidate in the configured repositories")
        sim = parse_simulation(backend.simulate_text(["--no-install-recommends", "--no-remove", "install", *names]))
        ops, unsafe = classify(sim, installed, native)
        if unsafe:
            kinds = sorted({k for k, _ in unsafe})
            raise TransactionError("unsafe_change_set", f"APT requires unsupported changes: {', '.join(kinds)}")
        closure_names = sorted({a["package"] for a in ops})
        inst_by_name = {e["package"]: e for e in installed}
        for n in names:
            if n not in closure_names:
                cur = inst_by_name.get(n)
                if cur is None or cur["status"] not in ("install ok installed", "hold ok installed"):
                    raise TransactionError("unsafe_change_set", f"{n} is neither selected nor installed")
                satisfied.append(cur)
        for n in [a["package"] for a in ops] + [s["package"] for s in satisfied]:
            if f"{n}:{native}" in holds or f"{n}:all" in holds:
                raise TransactionError("unsafe_change_set", f"held package {n} is part of the transaction")
    policy = parse_policy(backend.policy_text(sorted(set(closure_names) | set(names)))) if names else {}

    operations = []
    direct = set(names)
    for a in sorted(ops, key=lambda x: x["package"]):
        if not PACKAGE_NAME.match(a["package"]) or not VERSION.match(a["version"] or ""):
            raise TransactionError("backend_failure", "APT selected a malformed package name or version")
        prov = provenance(a["package"], a["version"], policy, targets, backend.show_text(a["package"], a["version"]))
        if prov["architecture"] != a["architecture"]:
            raise TransactionError("provenance_failure", f"{a['package']}: architecture differs from the index")
        relation = "direct" if a["package"] in direct else "dependency"
        operations.append({"package": a["package"], "architecture": a["architecture"], "version": a["version"],
                           "action": "install", "relation": relation,
                           "automatic_before": None, "automatic_after": relation == "dependency",
                           "repositories": prov["repositories"], "sha256": prov["sha256"], "size": prov["size"]})
    for cur in sorted(satisfied, key=lambda e: e["package"]):
        if cur["automatic"]:
            raise TransactionError("unsafe_change_set", f"direct requirement {cur['package']} is installed but marked "
                                   "automatic; the manual-mark transition is not supported yet")
        cand = (policy.get(cur["package"]) or {}).get("candidate")
        if cand != cur["version"]:
            raise TransactionError("unsafe_change_set", f"{cur['package']} installed version is not the candidate")
        prov = provenance(cur["package"], cur["version"], policy, targets,
                          backend.show_text(cur["package"], cur["version"]))
        operations.append({"package": cur["package"], "architecture": cur["architecture"],
                           "version": cur["version"], "action": "already_satisfied", "relation": "direct",
                           "automatic_before": False, "automatic_after": False,
                           "repositories": prov["repositories"], "sha256": prov["sha256"], "size": prov["size"]})
    operations.sort(key=lambda o: (o["package"], o["architecture"]))
    declared = dict(reqs)
    for o in operations:
        if o["relation"] == "direct" and declared[o["package"]] not in {r["component"] for r in o["repositories"]}:
            raise TransactionError("provenance_failure",
                                   f"{o['package']} is not offered by the component the plan requires")

    # Exact binding: only DIRECT requirements become explicit APT targets
    # (name:arch=version, built internally), so APT keeps direct packages
    # manual and dependencies automatic. The simulation of exactly that
    # request must reproduce the complete compiled closure (packages,
    # architectures, versions, install-only).
    installs = [o for o in operations if o["action"] == "install"]
    if installs:
        targets_ = direct_targets(operations, native)
        if not targets_:
            raise TransactionError("unsafe_change_set", "install operations without a direct requirement")
        exact = parse_simulation(backend.simulate_text(["--no-install-recommends", "--no-remove", "install", *targets_]))
        exact_ops, exact_unsafe = classify(exact, installed, native)
        got = sorted((a["package"], a["architecture"], a["version"]) for a in exact_ops)
        want = sorted((o["package"], o["architecture"], o["version"]) for o in installs)
        if exact_unsafe or got != want:
            raise TransactionError("unsafe_change_set", "direct-target request does not reproduce the compiled closure")

    used = sorted({tuple(sorted(r.items())) for o in operations for r in o["repositories"]})
    repository_state = {"repositories": [dict(r) for r in used],
                        "packages": [{k: o[k] for k in ("package", "architecture", "version", "sha256", "size")}
                                     for o in operations]}
    installed_digest_input = [{k: e[k] for k in ("package", "architecture", "version", "status", "automatic")}
                              for e in installed]
    tx = {
        "schema": TRANSACTION_SCHEMA_ID,
        "plan_schema": resolver.PLAN_SCHEMA_ID,
        "architecture": native,
        "inventory_digest": facts["inventory_digest"],
        "rule_set_digest": facts["rule_set_digest"],
        "plan_digest": plan_digest,
        "plan_status": plan["status"],
        "source_policy": {"components": list(AUTHORISED_COMPONENTS),
                          "releases": [{"origin": o, "label": lab, "codename": c} for o, lab, c in AUTHORISED_RELEASES],
                          "signed_by": list(AUTHORISED_SIGNED_BY)},
        "repository_state_digest": _digest(repository_state),
        "installed_state_digest": _digest(installed_digest_input),
        "installed_package_count": len(installed),
        "automatic_package_count": sum(1 for e in installed if e["automatic"]),
        "held_packages": holds,
        "package_manager_config_digest": _digest(config_facts(config)),
        "direct_requirements": [{"package": n, "source": s} for n, s in reqs],
        "operations": operations,
        "unresolved": plan["unresolved"],
        "safety": {"install_only": True, "install_recommends": False, "allow_removals": False,
                   "upgrades": 0, "downgrades": 0, "removals": 0, "reinstalls": 0,
                   "held_changes": 0, "architecture_changes": 0,
                   "exact_version_closure_verified": True,
                   "explicit_targets": "direct_only",
                   "package_marks": "direct_manual_dependency_automatic",
                   "existing_marks_preserved": True},
    }
    check_transaction(tx)
    if "transaction_digest" in expected and expected["transaction_digest"] != transaction_digest(tx):
        raise TransactionError("stale_transaction", "transaction differs from the freshly compiled transaction")
    return tx


def transaction_digest(tx):
    return _digest(tx)


def check_transaction(tx):
    """Schema + semantic self-check of a transaction document."""
    import json
    import jsonschema
    with open(os.path.join(resolver.SCHEMA_DIR, SCHEMA_FILE), encoding="utf-8") as f:
        schema = json.load(f)
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    if any(True for _ in validator.iter_errors(tx)):
        raise TransactionError("contract_failure", "transaction does not satisfy package-transaction v1")
    ops = tx["operations"]
    keys = [(o["package"], o["architecture"]) for o in ops]
    if keys != sorted(set(keys)):
        raise TransactionError("contract_failure", "operations not unique and sorted")
    direct = {r["package"] for r in tx["direct_requirements"]}
    for o in ops:
        if (o["relation"] == "direct") != (o["package"] in direct):
            raise TransactionError("contract_failure", f"{o['package']}: relation inconsistent")
        if o["architecture"] not in (tx["architecture"], "all"):
            raise TransactionError("contract_failure", f"{o['package']}: foreign architecture")
        if o["automatic_after"] != (o["relation"] == "dependency"):
            raise TransactionError("contract_failure", f"{o['package']}: package mark inconsistent with its relation")
        if o["action"] == "install" and o["automatic_before"] is not None:
            raise TransactionError("contract_failure", f"{o['package']}: a new install has no prior mark")
        if o["action"] == "already_satisfied" and (o["relation"] != "direct" or o["automatic_before"] is not False):
            raise TransactionError("contract_failure", f"{o['package']}: already_satisfied must be a manual direct package")
        for r in o["repositories"]:
            if ((r["origin"], r["label"], r["codename"]) not in AUTHORISED_RELEASES
                    or r["component"] not in AUTHORISED_COMPONENTS):
                raise TransactionError("unauthorised_source", f"{o['package']}: unauthorised repository identity")
    if not direct <= {o["package"] for o in ops}:
        raise TransactionError("contract_failure", "a direct requirement has no operation")
    try:
        _canon(tx)
    except resolver.ResolverError:
        raise TransactionError("contract_failure", "transaction is not canonical JSON") from None
    return tx


def verify_transaction(tx, inventory, rules, backend):
    """Recompile from fresh state; the transaction is valid only if identical."""
    check_transaction(tx)
    fresh = compile_transaction(inventory, rules, backend,
                                expected={k: tx[k] for k in ("inventory_digest", "rule_set_digest")})
    if _canon(fresh) != _canon(tx):
        changed = sorted(k for k in tx if tx.get(k) != fresh.get(k))
        raise TransactionError("stale_transaction", f"transaction is stale: {', '.join(changed)} changed")
    return fresh


# ---------------------------------------------------------------------------
# Future apply boundary (constructed and tested only; never executed here)

def direct_target(op, native):
    """Internally constructed explicit APT target for one DIRECT install
    operation: name:architecture=version (Architecture: all packages are
    addressed as name:all=version). APT 3.0.3 rejects foreign qualifiers but
    treats native and :all interchangeably, so the closure comparison, not
    the qualifier, binds native vs all. Never caller text."""
    if op["action"] != "install" or op["relation"] != "direct":
        raise TransactionError("contract_failure", f"{op['package']} is not a direct install operation")
    if not PACKAGE_NAME.match(op["package"]) or not VERSION.match(op["version"]):
        raise TransactionError("contract_failure", "operation is not a plain name/version")
    if op["architecture"] not in (native, "all") or not ARCHITECTURE.match(op["architecture"]):
        raise TransactionError("contract_failure", f"{op['package']}: architecture is not native or all")
    return f"{op['package']}:{op['architecture']}={op['version']}"


def direct_targets(operations, native):
    """Explicit targets of a transaction: direct install operations only.
    Dependencies are never explicit targets; APT selects them, and the
    pre-mutation simulation must reproduce them exactly."""
    return [direct_target(o, native) for o in operations if o["action"] == "install" and o["relation"] == "direct"]


def apply_argv(tx):
    """The future executor's argv for a verified transaction (list, shell=False).
    M1-I never runs it. The executor must run it only after a fresh
    recompile (verify_transaction) immediately before mutation."""
    check_transaction(tx)
    args = direct_targets(tx["operations"], tx["architecture"])
    argv = list(APPLY_BASE_ARGV) + args
    for a in argv:
        if a in FORBIDDEN_APT_ARGUMENTS:
            raise TransactionError("contract_failure", f"forbidden APT argument {a}")
    return argv


def apply_environment():
    """The future executor's environment: built from scratch, never inherited."""
    env = dict(COMMAND_ENV)
    env["DEBIAN_FRONTEND"] = "noninteractive"
    return env
