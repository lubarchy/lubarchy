#!/usr/bin/python3
"""LUBARCHY hardware package apply gate (M1-K; synthetic, non-mutating).

Exercises hardware/package_apply.py and hardware/apply-packages.py through a
fake host rooted in a temporary directory: a Debian-installer source tree,
the M1-I synthetic universe (non-free-firmware visible only after a
simulated `apt-get update`), synthetic .deb payloads (verified with the real
read-only `dpkg-deb --field`) and a simulated APT install that changes only
the fake dpkg state. It never writes /etc, /run or /var/lib/lubarchy, never
runs apt-get or a mutating dpkg, needs no root, never contacts a network and
never inspects the runner's hardware or package state.
"""

import sys

sys.dont_write_bytecode = True

import ast  # noqa: E402
import contextlib  # noqa: E402
import copy  # noqa: E402
import fcntl  # noqa: E402
import hashlib  # noqa: E402
import importlib.util  # noqa: E402
import inspect  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import pathlib  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402

TESTS = pathlib.Path(__file__).resolve().parent
REPO = TESTS.parent
HARDWARE = REPO / "hardware"
sys.path.insert(0, str(HARDWARE))
import package_apply as pa  # noqa: E402
import package_staging as ps  # noqa: E402
import package_transaction as pt  # noqa: E402
import resolver  # noqa: E402
import source_activation as sa  # noqa: E402


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, TESTS / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate9 = _load("gate9", "hardware-transaction.py")
gate10 = _load("gate10", "hardware-staging.py")
sys.path.remove(str(HARDWARE))

JOURNAL_SCHEMA_SHA256 = "904469c0bb857803f4c2a909760a7382d03756d8cc67ad3d2e12bdb468314c7d"
KEYRING_SHA = gate10.KEYRING_SHA
BASELINE = ("deb https://deb.debian.org/debian trixie main\n"
            "deb https://security.debian.org/debian-security trixie-security main\n"
            "deb https://deb.debian.org/debian trixie-updates main\n")
NFF_REPOS = ("trixie-non-free-firmware", "trixie-security-non-free-firmware")


class Failure(Exception):
    pass


def expect(cond, message):
    if not cond:
        raise Failure(message)


CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


TMP = None
_n = [0]


def tmpdir(name):
    _n[0] += 1
    d = os.path.join(TMP, f"{_n[0]:03d}-{name}")
    os.makedirs(d, mode=0o755)
    return d


def rejects(code, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except pa.ApplyError as e:
        expect(e.code == code, f"expected {code}, got {e.code}: {e.message}")
        return e
    raise Failure(f"expected {code}, but it was accepted")


# ---------------------------------------------------------------------------
# Fake host

class FakeBackend(gate9.FakeBackend):
    def __init__(self, host, u):
        super().__init__(u)
        self.host = host

    def policy_text(self, names):
        self.host.log.append(("policy", self.host.updated))
        return super().policy_text(names)

    def indextargets_text(self):
        out = []
        for key in sorted(self.u["repositories"]):
            rec = dict(self.u["repositories"][key], Filename=self.host.list_file(self.u["repositories"][key]))
            out.append("\n".join(f"{k}: {v}" for k, v in sorted(rec.items())))
        return "\n\n".join(out) + "\n"


class FakeHost:
    """Temporary root + fake package manager. Injection points: `fail` flags
    and `hooks` keyed by (event, count) run before the event is served."""

    def __init__(self, name="host", sources=BASELINE, identity=None, extra=None, installed=()):
        self.root = tmpdir(name)
        self.owner_uid = os.geteuid()
        self.owner_gid = os.getegid()
        self.uid = 0
        self.u_after, self.debs = gate10.staging_universe(identity=identity, extra=extra)
        self.u_after["installed"].extend(copy.deepcopy(list(installed)))
        self.updated = False
        self.fail = {}
        self.hooks = {}
        self.counts = {}
        self.log = []
        self.commands = []
        self.journal_at_apply = None
        self._attempt = 0
        self._clock = 0
        self.inv = gate9.inventory("fw-01-intel-iwlwifi")
        for d in ("etc/apt/sources.list.d", "run", "var/lib/apt/lists", "usr/share/doc"):
            os.makedirs(os.path.join(self.root, d), exist_ok=True)
        if sources is not None:
            pathlib.Path(self.root, "etc/apt/sources.list").write_text(sources)

    # -- universe views
    def universe(self):
        if self.updated:
            return self.u_after
        u = copy.deepcopy(self.u_after)
        u["repositories"] = {k: v for k, v in u["repositories"].items() if k not in NFF_REPOS}
        for n in list(u["packages"]):
            u["packages"][n] = [v for v in u["packages"][n] if not set(v["repositories"]) & set(NFF_REPOS)]
        return u

    def _event(self, name):
        self.counts[name] = self.counts.get(name, 0) + 1
        fn = self.hooks.get((name, self.counts[name]))
        if fn:
            fn(self)

    def list_file(self, repo):
        host_path = repo["Base-URI"].split("://")[1].split("/dists/")[0].replace("/", "_")
        return os.path.join(self.root, "var/lib/apt/lists",
                            f"{host_path}_dists_{repo['Codename']}_{repo['Component']}_binary-amd64_Packages")

    def _write_lists(self):
        lists = os.path.join(self.root, "var/lib/apt/lists")
        suites = {}
        for repo in self.u_after["repositories"].values():
            names = sorted(n for n, vs in self.u_after["packages"].items() for v in vs
                           if any(self.u_after["repositories"][r] is repo for r in v["repositories"]))
            pathlib.Path(self.list_file(repo)).write_text("".join(f"Package: {n}\n\n" for n in names))
            host_path = repo["Base-URI"].split("://")[1].split("/dists/")[0].replace("/", "_")
            suites[(host_path, repo["Codename"])] = repo["Label"]
        for (host_path, suite), label in suites.items():
            pathlib.Path(lists, f"{host_path}_dists_{suite}_InRelease").write_text(
                f"Origin: Debian\nLabel: {label}\nCodename: {suite}\nSHA256:\n"
                f" {sa.EMPTY_SHA256}        0 non-free-firmware/binary-amd64/Packages\n")

    # -- Host interface
    def euid(self):
        return self.uid

    def fchown(self, fd):
        if self.fail.get("source_write") and self.log_source_write():
            self.fail["source_write"] = False                 # inject exactly once (the activation write)
            raise OSError(5, "injected write failure")

    def log_source_write(self):
        return any(n.endswith(".tmp") and sa.OWNED_NAME in n
                   for n in os.listdir(os.path.join(self.root, "etc/apt/sources.list.d")))

    def now(self):
        self._clock += 1
        return f"2026-10-14T00:00:{self._clock % 60:02d}Z"

    def attempt_id(self):
        self._attempt += 1
        return f"{self._attempt:016x}"

    def inventory(self):
        self._event("inventory")
        return copy.deepcopy(self.inv)

    def rules(self):
        return gate9.RULES

    def backend(self):
        self._event("backend")
        return FakeBackend(self, self.universe())

    def architecture(self):
        return "amd64"

    def keyring_sha256(self):
        return KEYRING_SHA

    def acquirer(self):
        host = self

        class Acq(gate10.FakeAcquirer):
            def download(self, op, dest):
                if host.fail.get("acquire"):
                    raise ps.StagingError("acquisition_failure", f"{op['package']} could not be acquired")
                super().download(op, dest)
        return Acq(self.debs)

    def inspector(self):
        return ps.DebInspector()

    def run(self, argv):
        expect(pa.allowed_command(argv, self.root), f"executor ran a non-allowlisted command {argv}")
        self.commands.append(list(argv))
        if argv == [pa.APT_GET, "update"]:
            self._event("update")
            if self.fail.get("update"):
                return 100, "E: Failed to fetch (injected)\n"
            self.updated = True
            self._write_lists()
            return 0, "Hit:1 https://deb.debian.org/debian trixie InRelease\n"
        if argv == [pa.DPKG, "--audit"]:
            return 0, self.fail.get("audit", "")
        if argv == [pa.APT_GET, "check"]:
            return (1, "E: broken\n") if self.fail.get("check") else (0, "")
        if argv[:2] == [pa.DPKG_QUERY, "--listfiles"]:
            name = argv[2]
            if self.fail.get("missing_file") == name:
                return 0, f"/.\n/usr/share/doc/{name}/missing-file\n"
            return 0, f"/.\n/usr\n/usr/share/doc/{name}/copyright\n"
        if argv[:2] == [pa.APT_GET, "install"]:
            return self._apt_install(argv)
        raise Failure(f"unexpected command {argv}")

    def _apt_install(self, argv):
        journal = pathlib.Path(self.root, pa.JOURNAL_DIR)
        docs = [json.loads(p.read_text()) for p in journal.glob("*/journal.json")]
        self.journal_at_apply = [d["phase"] for d in docs]
        if self.fail.get("apt"):
            return 100, "E: Sub-process /usr/bin/dpkg returned an error code (1)\n"
        cache = argv[argv.index("-o") + 1].split("=", 1)[1]
        targets = argv[argv.index("-o") + 2:]
        u = self.u_after
        tx = pt.compile_transaction(self.inv, gate9.RULES, FakeBackend(self, u))
        direct = {t.split(":")[0] for t in targets}
        for o in tx["operations"]:
            if o["action"] != "install":
                continue
            fn = ps.payload_filename(o)
            expect(os.path.isfile(os.path.join(cache, fn)), f"APT would need to download {fn}")
            u["installed"].append({"package": o["package"], "architecture": o["architecture"],
                                   "version": o["version"], "status": "install ok installed",
                                   "automatic": o["package"] not in direct})
            doc = pathlib.Path(self.root, "usr/share/doc", o["package"])
            doc.mkdir(parents=True, exist_ok=True)
            (doc / "copyright").write_text("synthetic\n")
        for label, mutate in self.fail.get("post", {}).items():
            mutate(u)
        return 0, "Selecting previously unselected package (synthetic)\n"


def _load_tool(path):
    spec = importlib.util.spec_from_file_location("trusted_deployment", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_cli(path):
    spec = importlib.util.spec_from_file_location("apply_cli", path)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(HARDWARE))
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(HARDWARE))
    return mod


def run_apply(host):
    return pa.apply_current_plan(host)


def journals(host):
    return [json.loads(p.read_text()) for p in sorted(pathlib.Path(host.root, pa.JOURNAL_DIR).glob("*/journal.json"))]


def owned(host):
    return pathlib.Path(host.root, "etc/apt/sources.list.d", sa.OWNED_NAME)


def failed_with(host, code, phase_reached=None, apt_ran=False):
    e = rejects(code, run_apply, host)
    j = journals(host)[-1]
    expect(j["phase"] == "failed" and j["result"] == "failed" and j["failure"]["code"] == code,
           f"journal {j['phase']} {j['failure']}")
    if phase_reached:
        expect(j["failure"]["phase"] == phase_reached, f"failed in phase {j['failure']['phase']}")
    ran = any(c[:2] == [pa.APT_GET, "install"] for c in host.commands)
    expect(ran == apt_ran, f"APT install {'did not run' if apt_ran else 'ran'}")
    return e, j


# ---------------------------------------------------------------------------
# Success path

@check
def qualified_plan_applies_end_to_end():                            # 10, 27, 31-35, 39-41, 46-48
    h = FakeHost("success")
    sources_before = pathlib.Path(h.root, "etc/apt/sources.list").read_bytes()
    doc = run_apply(h)
    expect(doc["phase"] == "complete" and doc["result"] == "complete", f"result {doc['phase']}")
    phases = [p["phase"] for p in doc["phases"]]
    expect(phases == ["prepared", "locked", "source_activated", "metadata_refreshed", "transaction_revalidated",
                      "bundle_staged", "bundle_verified", "applying", "applied", "validated", "complete"],
           f"phases {phases}")
    expect(h.journal_at_apply == ["applying"], f"journal on disk at APT time: {h.journal_at_apply}")
    c = sa.compile_activation(sa.inspect(h.root), "amd64", KEYRING_SHA)
    expect(owned(h).read_bytes() == sa.render(sa.compile_activation(
        sa.inspect(gate10.src_root("render-ref", BASELINE)), "amd64", KEYRING_SHA)), "owned source bytes")
    expect(c["action"] == "already_satisfied", "post-state not satisfied")
    expect(oct(owned(h).stat().st_mode & 0o7777) == "0o644", "owned mode")
    expect(pathlib.Path(h.root, "etc/apt/sources.list").read_bytes() == sources_before, "foreign source changed")
    installs = [c for c in h.commands if c[:2] == [pa.APT_GET, "install"]]
    expect(len(installs) == 1, f"{len(installs)} APT installs")
    argv = installs[0]
    expect(argv[:7] == list(pa.APPLY_BASE_ARGV) and argv[7] == "-o" and argv[8].startswith("Dir::Cache::archives="),
           f"argv {argv[:9]}")
    expect(argv[9:] == ["firmware-iwlwifi:all=20250410-2", "intel-microcode:amd64=3.20251111.1~deb13u1"],
           f"targets {argv[9:]}")
    for need in ("--no-download", "--no-install-recommends", "--no-remove"):
        expect(need in argv, f"{need} missing")
    expect(not any(a.startswith("iucode-tool") for a in argv), "dependency became an explicit target")
    for bad in pa.FORBIDDEN_APT_ARGUMENTS:
        expect(bad not in argv, f"{bad} in argv")
    cache = argv[8].split("=", 1)[1]
    expect(cache == os.path.join(h.root, pa.JOURNAL_DIR, doc["attempt_id"], pa.APPLY_CACHE) + "/", "cache path")
    marks = {p["package"]: (p["automatic"], p["relation"], p["version"]) for p in doc["post_state"]["packages"]}
    expect(marks == {"firmware-iwlwifi": (False, "direct", "20250410-2"),
                     "intel-microcode": (False, "direct", "3.20251111.1~deb13u1"),
                     "iucode-tool": (True, "dependency", "2.3.1-3")}, f"post-state {marks}")
    staged = pathlib.Path(h.root, pa.STAGING_DIR, doc["digests"]["transaction_digest"][7:])
    expect(staged.is_dir() and (staged / "manifest.json").is_file(), "root-owned staging bundle missing")
    for k, v in doc["digests"].items():
        expect(v and v.startswith("sha256:"), f"digest {k} not recorded")
    expect([c["package"] for c in doc["expected"]["closure"]] == ["firmware-iwlwifi", "intel-microcode", "iucode-tool"],
           "expected closure")
    pa.check_journal(doc)
    return f"11 phases to complete; argv {argv[9:]}; dependency automatic; journal 'applying' on disk at APT time"


@check
def nothing_to_apply_when_installed():
    h = FakeHost("noop")
    run_apply(h)
    h2 = h
    h2.commands.clear()
    doc = run_apply(h2)
    expect(doc["result"] == "nothing_to_apply" and doc["phase"] == "complete", f"second run {doc['result']}")
    expect(not any(c[:2] == [pa.APT_GET, "install"] for c in h2.commands), "APT ran without install operations")
    return "a satisfied plan completes as nothing_to_apply without APT"


# ---------------------------------------------------------------------------
# Privilege, API and lock (1-7)

@check
def root_api_and_lock_boundaries():                                  # 1-7
    h = FakeHost("notroot")
    h.uid = 1000
    rejects("not_root", run_apply, h)
    expect(not os.path.exists(os.path.join(h.root, pa.LOCK_DIR)), "non-root run touched state")
    expect(list(inspect.signature(pa.apply_current_plan).parameters) == ["host"], "apply signature")
    expect(not inspect.signature(pa.RealHost).parameters, "RealHost takes parameters")
    cli = HARDWARE / "apply-packages.py"
    # Usage probes run in-process with RealHost replaced by a sentinel, so a
    # regression can never reach the real executor (CI runs as root).
    climod = _load_cli(cli)
    constructed = []

    class Sentinel:
        def __init__(self):
            constructed.append(True)
            raise Failure("the CLI constructed a real host for invalid input")
    saved, climod.pa.RealHost = climod.pa.RealHost, Sentinel
    try:
        for argv in (["apply", "intel-microcode"], ["apply", "--package", "x"], ["apply", "-o", "APT::x=1"],
                     ["apply", "--uri", "https://evil"], ["apply", "--staging-root", "/tmp"], ["install", "x"],
                     ["apply", "--component", "contrib"], ["status", "x"], [], ["apply", ""]):
            with open(os.devnull, "w") as null, contextlib.redirect_stderr(null):
                rc = climod.main(argv)
            expect(rc == 64 and not constructed, f"CLI {argv} accepted")
    finally:
        climod.pa.RealHost = saved
    if os.geteuid() != 0:
        r = subprocess.run([sys.executable, "-I", str(cli), "apply"], env={"PATH": "/usr/bin:/bin"},
                           capture_output=True, timeout=60)
        expect(r.returncode == pa.EXIT_NOT_ROOT and b"not_root" in r.stderr, f"CLI as non-root -> {r.returncode}")
    # LUBARCHY lock: a second transaction is rejected while one holds the lock
    h = FakeHost("busy")
    lockdir = pathlib.Path(h.root, pa.LOCK_DIR)
    lockdir.mkdir(parents=True, mode=0o755)
    fd = os.open(lockdir / pa.LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        rejects("busy", run_apply, h)
        expect(not os.path.exists(os.path.join(h.root, pa.JOURNAL_DIR)), "busy run created a journal")
    finally:
        os.close(fd)
    expect((lockdir / pa.LOCK_NAME).exists(), "lock file deleted")
    doc = run_apply(h)
    expect(doc["result"] == "complete", "lock released run failed")
    expect((lockdir / pa.LOCK_NAME).exists(), "lock file deleted after a transaction")
    h = FakeHost("symlock")
    lockdir = pathlib.Path(h.root, pa.LOCK_DIR)
    lockdir.mkdir(parents=True, mode=0o755)
    victim = pathlib.Path(tmpdir("victim"), "target")
    victim.write_text("x")
    os.chmod(victim, 0o600)            # a perfectly shaped target: only O_NOFOLLOW can refuse it
    os.symlink(victim, lockdir / pa.LOCK_NAME)
    rejects("unsafe_path", run_apply, h)
    expect(victim.read_text() == "x", "lock followed a symlink")
    h = FakeHost("lockmode")
    lockdir = pathlib.Path(h.root, pa.LOCK_DIR)
    lockdir.mkdir(parents=True, mode=0o755)
    (lockdir / pa.LOCK_NAME).write_text("")
    os.chmod(lockdir / pa.LOCK_NAME, 0o666)
    rejects("unsafe_path", run_apply, h)
    h = FakeHost("lockdir")
    os.makedirs(os.path.join(h.root, pa.LOCK_DIR), mode=0o777)
    os.chmod(os.path.join(h.root, pa.LOCK_DIR), 0o777)
    rejects("unsafe_path", run_apply, h)
    return "root required; CLI takes no package/option/URI/root input; busy lock -> exit 8; symlink/mode lock refused"


# ---------------------------------------------------------------------------
# Source activation (8-12, 46-47, 50)

@check
def source_activation_rules():
    h = FakeHost("stale-pre", sources=BASELINE.replace(" main\n", " main contrib\n", 1))
    e, _j = failed_with(h, "unauthorised_source")
    expect(not owned(h).exists(), "unauthorised topology activated")
    h = FakeHost("nvidia", sources=BASELINE + "deb https://developer.download." + "nvidia.com/compute/cuda/repos/debian13/x86_64 /\n")
    failed_with(h, "unauthorised_source")
    expect(not owned(h).exists() and not h.commands, "NVIDIA topology reached a command")
    h = FakeHost("ambiguous", sources=BASELINE.splitlines()[0] + "\n")
    failed_with(h, "ambiguous_ownership")
    # write failure: nothing left behind (atomic model), owned state restored
    h = FakeHost("write-fail")
    h.fail["source_write"] = True
    failed_with(h, "source_activation_failed", phase_reached="locked")
    parts = os.listdir(os.path.join(h.root, "etc/apt/sources.list.d"))
    expect(parts == [], f"source write residue {parts}")
    # pre-existing (stale) owned file is restored byte-for-byte on failure
    h = FakeHost("restore")
    owned(h).write_bytes(b"# older LUBARCHY file\n")
    os.chmod(owned(h), 0o644)
    h.fail["source_write"] = True
    failed_with(h, "source_activation_failed")
    expect(owned(h).read_bytes() == b"# older LUBARCHY file\n", "owned pre-state not restored")
    # failure AFTER the atomic rename (post-write re-inspection): the owned
    # file is removed (create) or its exact old bytes come back (replace)
    real_compile = pa.sa.compile_activation
    for label, old in (("post-create", None), ("post-replace", b"# older LUBARCHY file\n")):
        h = FakeHost(label)
        if old is not None:
            owned(h).write_bytes(old)
            os.chmod(owned(h), 0o644)
        calls = []

        def flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:             # the re-inspection right after the rename
                expect(owned(h).exists() and owned(h).read_bytes() != old, "write did not land before re-inspection")
                raise sa.SourceError("ambiguous_ownership", "injected post-write mismatch")
            return real_compile(*args, **kwargs)
        pa.sa.compile_activation = flaky
        try:
            failed_with(h, "source_activation_failed", phase_reached="locked")
        finally:
            pa.sa.compile_activation = real_compile
        if old is None:
            expect(not owned(h).exists(), "created owned source not removed after a failed activation")
        else:
            expect(owned(h).read_bytes() == old, "owned source not restored after a failed activation")
    # a stale owned file is replaced (owned only) on success
    h = FakeHost("replace")
    owned(h).write_bytes(b"# older LUBARCHY file\n")
    os.chmod(owned(h), 0o644)
    doc = run_apply(h)
    expect(doc["source_activation"]["action"] == "replace_owned_source" and doc["result"] == "complete", "replace")
    # foreign source tree is never written: sources.list.d symlink / foreign perms refused
    h = FakeHost("partslink")
    shutil.rmtree(os.path.join(h.root, "etc/apt/sources.list.d"))
    os.symlink(tmpdir("elsewhere"), os.path.join(h.root, "etc/apt/sources.list.d"))
    e = rejects("unsafe_path", run_apply, h)
    return "contrib/NVIDIA/ambiguous topologies fail before any command; write failure leaves no residue and restores"


@check
def foreign_source_mutation_impossible():                           # 9, 47
    src = (HARDWARE / "package_apply.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    writes = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "durable_replace"]
    for call in writes:
        target = ast.unparse(call.args[2]) if len(call.args) > 2 else ""
        expect(target in ("sa.OWNED_NAME", "JOURNAL_FILE", "name"), f"durable_replace writes {target}")
    expect("sources.list\"" not in src and "SOURCE_LIST" not in src, "executor references the foreign sources.list")
    h = FakeHost("foreign")
    before = {p: p.read_bytes() for p in pathlib.Path(h.root, "etc/apt").rglob("*") if p.is_file()}
    run_apply(h)
    after = {p: p.read_bytes() for p in pathlib.Path(h.root, "etc/apt").rglob("*") if p.is_file()}
    expect({p: b for p, b in after.items() if p.name != sa.OWNED_NAME} == before, "foreign source tree changed")
    return "only the owned file and journal are ever written; foreign tree byte-identical after apply"


# ---------------------------------------------------------------------------
# Metadata refresh and fresh transaction (13-16)

@check
def refresh_and_fresh_transaction_required():
    h = FakeHost("update-fail")
    h.fail["update"] = True
    failed_with(h, "metadata_refresh_failed", phase_reached="source_activated")
    expect(not pathlib.Path(h.root, pa.STAGING_DIR).exists(), "staging after a failed refresh")
    h = FakeHost("order")
    run_apply(h)
    expect(h.log and all(updated for kind, updated in h.log if kind == "policy"),
           "a transaction was compiled before the authenticated metadata refresh")
    expect(h.commands[0] == [pa.APT_GET, "update"], f"first command {h.commands[0]}")
    # an unsafe change set (upgrade of an installed package) is rejected before staging
    h = FakeHost("unsafe", installed=[{"package": "firmware-iwlwifi", "architecture": "all", "version": "20240101-1",
                                       "status": "install ok installed", "automatic": False}])
    failed_with(h, "unsafe_change_set", phase_reached="metadata_refreshed")
    # transaction changes between refresh and final revalidation -> stale
    h = FakeHost("tx-changes")

    def newer(host):
        v = copy.deepcopy(host.u_after["packages"]["intel-microcode"][0])
        v.update(version="3.20261001.1~deb13u1", repositories=["trixie-security-non-free-firmware"], sha256="d" * 64)
        host.u_after["packages"]["intel-microcode"].append(v)
    h.hooks[("backend", 2)] = newer
    failed_with(h, "stale_transaction", phase_reached="bundle_staged")
    return "refresh failure stops before staging; transactions only after refresh; upgrade and new candidate rejected"


# ---------------------------------------------------------------------------
# Staging and re-hash (16-20)

@check
def root_staging_and_rehash():
    h = FakeHost("staging-link")
    os.makedirs(os.path.join(h.root, "var/lib/lubarchy"), mode=0o755)
    os.symlink(tmpdir("evil-staging"), os.path.join(h.root, pa.STAGING_DIR))
    failed_with(h, "unsafe_path", phase_reached="transaction_revalidated")
    h = FakeHost("staging-mode")
    os.makedirs(os.path.join(h.root, pa.STAGING_DIR), mode=0o777)
    os.chmod(os.path.join(h.root, pa.STAGING_DIR), 0o777)
    failed_with(h, "unsafe_path")
    expect(pa.STAGING_DIR == ps.STAGING_ROOT.lstrip("/") and "staging_root" not in
           inspect.signature(pa.apply_current_plan).parameters, "staging root is not fixed")

    def corrupt_same_size(host):
        b = next(pathlib.Path(host.root, pa.STAGING_DIR).glob("*/archives/intel-microcode_*.deb"))
        data = bytearray(b.read_bytes())
        data[-5] ^= 0xFF
        b.write_bytes(bytes(data))
    h = FakeHost("rehash")
    h.hooks[("backend", 2)] = corrupt_same_size
    e, _ = failed_with(h, "payload_mismatch", phase_reached="bundle_staged")
    expect("SHA-256" in e.message, e.message)

    def swap_identity(host):
        b = next(pathlib.Path(host.root, pa.STAGING_DIR).glob("*/archives/iucode-tool_*.deb"))
        b.write_bytes(gate10.make_deb("iucode-tool", "2.3.1-4", "amd64"))
    h = FakeHost("identity")
    h.hooks[("backend", 2)] = swap_identity
    failed_with(h, "payload_mismatch")
    h = FakeHost("ident-stage", identity={"iucode-tool": ("iucode-tool-evil", "2.3.1-3", "amd64")})
    failed_with(h, "payload_mismatch", phase_reached="transaction_revalidated")
    h = FakeHost("acquire")
    h.fail["acquire"] = True
    failed_with(h, "acquisition_failure")
    for bad in ("/var/lib/lubarchy/package-transactions/../../../etc/x/apply-cache/",
                "/tmp/x/apply-cache/", "/var/lib/lubarchy/package-transactions/zz/apply-cache/"):
        argv = list(pa.APPLY_BASE_ARGV) + ["-o", f"Dir::Cache::archives={bad}", "x:amd64=1"]
        expect(not pa.allowed_apply_argv(argv), f"cache {bad} allowed")
    return "symlinked/writable staging refused; same-size corruption and identity swap caught by the immediate re-hash"


# ---------------------------------------------------------------------------
# Final stale-state revalidation (21-26)

@check
def stale_state_is_never_applied():
    cases = {
        "inventory": ("inventory", 2, lambda h: setattr(h, "inv", gate9.inventory("fw-02-intel-i915")),
                      "stale_transaction"),
        "installed state": ("backend", 2, lambda h: h.u_after["installed"].append(
            {"package": "zsh", "architecture": "amd64", "version": "5.9-8", "status": "install ok installed"}),
            "stale_transaction"),
        "package marks": ("backend", 2, lambda h: h.u_after["installed"][0].update(automatic=True), "stale_transaction"),
        "holds": ("backend", 2, lambda h: h.u_after["installed"][0].update(status="hold ok installed"),
                  "stale_transaction"),
        "source state": ("backend", 2, lambda h: owned(h).write_bytes(owned(h).read_bytes() + b"# drift\n"),
                         "stale_activation"),
        "metadata": ("backend", 2, lambda h: pathlib.Path(h.list_file(h.u_after["repositories"]["trixie-main"]))
                     .write_text("Package: changed\n"), "stale_bundle"),
        "bundle manifest": ("backend", 2, lambda h: next(pathlib.Path(h.root, pa.STAGING_DIR).glob("*/manifest.json"))
                            .write_bytes(b"{}\n"), "contract_failure"),
    }
    for label, (event, n, mutate, code) in cases.items():
        h = FakeHost("stale-" + label.replace(" ", "-"))
        h.hooks[(event, n)] = mutate
        failed_with(h, code, phase_reached="bundle_staged")
    return f"{len(cases)} kinds of drift between staging and apply stop the transaction before APT"


# ---------------------------------------------------------------------------
# Journal (27-28, 48-49)

@check
def journal_durability_and_recovery():
    h = FakeHost("fsync")
    calls = []
    real = pa.os.fsync
    pa.os.fsync = lambda fd: (calls.append(fd), real(fd))[1]
    try:
        j = None
        pa._ensure_dir(h, pa.LUBARCHY_STATE, 0o755)
        j = pa.Journal(h)
    finally:
        pa.os.fsync = real
    expect(len(calls) >= 2, f"journal write fsyncs {len(calls)} (file and directory expected)")
    expect(oct(os.lstat(os.path.join(j.dir, pa.JOURNAL_FILE)).st_mode & 0o777) == "0o600", "journal mode")
    expect(not [n for n in os.listdir(j.dir) if n.endswith(".tmp")], "journal temp residue")
    # a non-terminal journal blocks a new transaction
    for phase, started in (("applying", True), ("bundle_staged", False), ("failed", True)):
        h = FakeHost("pending-" + phase)
        pa._ensure_dir(h, pa.LUBARCHY_STATE, 0o755)
        j = pa.Journal(h)
        seq = ["locked", "source_activated", "metadata_refreshed", "transaction_revalidated", "bundle_staged",
               "bundle_verified", "applying"]
        for p in seq[:{"applying": 7, "bundle_staged": 5, "failed": 7}[phase]]:
            j.transition(p)
        if phase == "failed":
            j.fail(pa.ApplyError("apply_failed", "x"))
        h.commands.clear()
        rejects("recovery_required", run_apply, h)
        expect(not h.commands and len(journals(h)) == 1, "recovery_required run did something")
    # a journal write failure at "applying" means APT never runs
    h = FakeHost("journal-write")
    real_replace = pa.durable_replace

    def failing(host, directory, name, data, mode):
        if name == pa.JOURNAL_FILE and b'"phase":"applying"' in data:
            raise OSError(28, "injected: no space left")
        return real_replace(host, directory, name, data, mode)
    pa.durable_replace = failing
    try:
        failed_with(h, "backend_failure", phase_reached="bundle_verified", apt_ran=False)
    finally:
        pa.durable_replace = real_replace
    expect(journals(h)[-1]["apply_started"] is False, "apply_started without a durable applying phase")
    # a failure before APT is terminal and does not block
    h = FakeHost("terminal")
    h.fail["update"] = True
    rejects("metadata_refresh_failed", run_apply, h)
    h.fail.pop("update")
    expect(run_apply(h)["result"] == "complete", "a pre-APT failure blocked the next attempt")
    # malformed journal fails closed
    h = FakeHost("malformed")
    d = pathlib.Path(h.root, pa.JOURNAL_DIR, "0" * 16)
    d.mkdir(parents=True)
    (d / pa.JOURNAL_FILE).write_text("{\"schema\": \"x\"}\n")
    rejects("recovery_required", run_apply, h)
    # journal content: no command output, no environment, no caller text
    h = FakeHost("content")
    doc = run_apply(h)
    text = json.dumps(doc)
    for bad in ("Selecting", "Hit:", "PATH", "DEBIAN_FRONTEND", "/usr/bin/apt-get"):
        expect(bad not in text, f"journal contains {bad}")
    # complete only after validation
    bad = copy.deepcopy(doc)
    bad["phases"] = [p for p in bad["phases"] if p["phase"] != "validated"]
    expect([p["phase"] for p in doc["phases"]][-2:] == ["validated", "complete"], "complete before validated")
    j = pa.Journal(h)
    j.transition("locked")
    try:
        j.transition("prepared")
        raise Failure("journal phase went backwards")
    except pa.ApplyError:
        pass
    sha = hashlib.sha256((HARDWARE / "schemas" / pa.JOURNAL_SCHEMA_FILE).read_bytes()).hexdigest()
    expect(sha == JOURNAL_SCHEMA_SHA256, f"apply-journal schema changed ({sha})")
    return "file+directory fsync, 0600; non-terminal or post-APT failure -> recovery_required; schema baseline matches"


# ---------------------------------------------------------------------------
# APT failure and post-apply validation (37-45)

@check
def apt_failure_is_journaled_without_recovery():
    h = FakeHost("apt-fail")
    h.fail["apt"] = True
    e, j = failed_with(h, "apply_failed", phase_reached="applying", apt_ran=True)
    expect(j["apply_started"] is True, "apply_started not recorded")
    after = h.commands[h.commands.index(next(c for c in h.commands if c[:2] == [pa.APT_GET, "install"])) + 1:]
    expect(after == [], f"commands after the failed APT run: {after}")
    h.fail.pop("apt")
    h.commands.clear()
    rejects("recovery_required", run_apply, h)
    expect(h.commands == [], "recovery_required ran commands")
    src = (HARDWARE / "package_apply.py").read_text(encoding="utf-8")
    for pat in (r"\"remove\"", r"\"purge\"", r"--fix-broken", r"\"-f\"", r"--configure", r"\"autoremove\"",
                r"\"-i\"", r"--install\b", r"\"--unpack\""):
        expect(not re.search(pat, src), f"executor contains recovery/dpkg action {pat}")
    return "APT exit 100 -> failed (apply_started) and nothing else runs; next invocation recovery_required"


@check
def post_apply_validation_detects_deviations():
    cases = {
        "extra package": lambda u: u["installed"].append({"package": "evil", "architecture": "amd64", "version": "1",
                                                          "status": "install ok installed"}),
        "removal": lambda u: u["installed"].remove(next(e for e in u["installed"] if e["package"] == "libc6")),
        "downgrade": lambda u: next(e for e in u["installed"] if e["package"] == "iucode-tool").update(version="2.3.1-1"),
        "hold change": lambda u: next(e for e in u["installed"] if e["package"] == "dpkg").update(
            status="hold ok installed"),
        "dependency manual": lambda u: next(e for e in u["installed"] if e["package"] == "iucode-tool").update(
            automatic=False),
        "direct automatic": lambda u: next(e for e in u["installed"] if e["package"] == "intel-microcode").update(
            automatic=True),
        "half installed": lambda u: next(e for e in u["installed"] if e["package"] == "intel-microcode").update(
            status="install ok half-configured"),
    }
    for label, mutate in cases.items():
        h = FakeHost("post-" + label.replace(" ", "-"))
        h.fail["post"] = {label: mutate}
        e, j = failed_with(h, "validation_failed", phase_reached="applied", apt_ran=True)
        expect(j["post_state"] is None and "complete" not in [p["phase"] for p in j["phases"]], f"{label} completed")
    for label, flag, value in (("dpkg audit", "audit", "The following packages are only half configured\n"),
                               ("apt check", "check", True), ("package files", "missing_file", "intel-microcode")):
        h = FakeHost("post-" + label.replace(" ", "-"))
        h.fail[flag] = value
        failed_with(h, "validation_failed", phase_reached="applied", apt_ran=True)

    h = FakeHost("post-source")
    h.fail["post"] = {"source": lambda u: owned(h).write_bytes(b"# changed\n")}
    failed_with(h, "validation_failed", apt_ran=True)
    return f"{len(cases) + 4} post-apply deviations (extra, removal, downgrade, hold, marks, audit, check, files, source) fail"


# ---------------------------------------------------------------------------
# Command boundary (29-36)

@check
def commands_are_allowlisted_sanitized_and_shell_free():
    calls = []

    class R:
        returncode, stdout, stderr = 0, b"", b""

    real = pa.subprocess.run
    pa.subprocess.run = lambda argv, **kw: (calls.append((argv, kw)), R())[1]
    os.environ.update(APT_CONFIG="/tmp/evil.conf", http_proxy="http://evil:1", LC_ALL="xx", PYTHONPATH="/evil")
    try:
        host = pa.RealHost()
        host.run([pa.APT_GET, "update"])
        host.run([pa.DPKG, "--audit"])
    finally:
        pa.subprocess.run = real
        for k in ("APT_CONFIG", "http_proxy", "LC_ALL", "PYTHONPATH"):
            os.environ.pop(k, None)
    for argv, kw in calls:
        expect(kw.get("shell") is False and kw.get("env") == pa.APPLY_ENV and kw.get("stdin") == subprocess.DEVNULL,
               f"{argv}: {kw.get('shell')} {kw.get('env')}")
    expect(pa.APPLY_ENV == {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LANGUAGE": "C", "LC_ALL": "C",
                            "DEBIAN_FRONTEND": "noninteractive", "HOME": "/root"}, "environment literal changed")
    for argv in ([pa.APT_GET, "install", "x"], [pa.APT_GET, "update", "-o", "x"], ["/usr/bin/dpkg", "-i", "x.deb"],
                 [pa.APT_GET, "-f", "install"], [pa.APT_GET, "remove", "x"], ["/bin/sh", "-c", "true"],
                 ["apt-get", "update"], [pa.APT_GET, "--fix-broken", "install"], ["/usr/bin/apt-mark", "auto", "x"],
                 list(pa.APPLY_BASE_ARGV) + ["-o", "APT::Get::AllowUnauthenticated=1", "x:amd64=1"],
                 list(pa.APPLY_BASE_ARGV) + ["-o", "Dir::Cache::archives=/var/lib/lubarchy/package-transactions/"
                                             "0123456789abcdef/apply-cache/", "--allow-downgrades", "x:amd64=1"],
                 [a for a in pa.APPLY_BASE_ARGV if a != "--no-download"] + ["-o", "Dir::Cache::archives=/var/lib/"
                                                                          "lubarchy/package-transactions/"
                                                                          "0123456789abcdef/apply-cache/", "x:amd64=1"]):
        expect(not pa.allowed_command(argv), f"{argv} allowed")
        try:
            pa.RealHost().run(argv)
            raise Failure(f"{argv} ran")
        except pa.ApplyError as e:
            expect(e.code == "contract_failure", e.code)
    ok = list(pa.APPLY_BASE_ARGV) + ["-o", "Dir::Cache::archives=/var/lib/lubarchy/package-transactions/"
                                     "0123456789abcdef/apply-cache/", "intel-microcode:amd64=1.0"]
    expect(pa.allowed_command(ok), "the canonical apply argv is not allowed")
    for a in ("--allow-unauthenticated", "--allow-downgrades", "--allow-remove-essential", "--allow-change-held-packages",
              "--force-yes", "--fix-broken", "--install-recommends", "--download-only", "--reinstall", "--purge"):
        expect(a in pa.FORBIDDEN_APT_ARGUMENTS and a not in pa.APPLY_BASE_ARGV, f"{a} not forbidden")
    for mod in ("package_apply.py", "apply-packages.py"):
        tree = ast.parse((HARDWARE / mod).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                expect(name not in ("system", "popen", "Popen", "check_output", "call", "check_call", "execv",
                                    "execve", "rmtree", "setuid", "seteuid"), f"{mod}: {name}()")
                if name == "run" and isinstance(node.func, ast.Attribute) and getattr(node.func.value, "id", "") == "subprocess":
                    kw = {k.arg: k.value for k in node.keywords}
                    expect(isinstance(kw.get("shell"), ast.Constant) and kw["shell"].value is False, f"{mod}: shell")
        text = (HARDWARE / mod).read_text(encoding="utf-8")
        for bad in ("sudo", "pkexec", "polkit", "sudoers", "setuid", "os.environ"):
            expect(bad not in text.replace("no sudo, setuid, sudoers, polkit rule", "").replace(
                "never invokes sudo", ""), f"{mod} mentions {bad}")
    return "literal environment, shell=False, caller APT_CONFIG/proxy/locale ignored; allowlist refuses repair/remove/dpkg -i/-o"


@check
def trusted_deployment_invariants():
    """The privileged launch uses only an exact, owner-checked, unprivileged-nonwritable deployment."""
    td = _load_tool(REPO / "tools" / "hwe" / "trusted-deployment.py")
    uid = os.geteuid()
    anchor = tmpdir("anchor")                       # stands in for "/" (the tmp ancestors are world-writable)
    src = tmpdir("reviewed-repo")
    shutil.copytree(HARDWARE, os.path.join(src, "hardware"), ignore=shutil.ignore_patterns("__pycache__"))
    os.makedirs(os.path.join(src, "tools", "hwe"))
    shutil.copy2(REPO / "tools" / "hwe" / "trusted-deployment.py", os.path.join(src, "tools", "hwe"))
    genv = {"PATH": "/usr/bin:/bin", "HOME": anchor, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}

    def sh(argv, cwd=None):
        r = subprocess.run(argv, cwd=cwd, env=genv, capture_output=True, timeout=300)
        expect(r.returncode == 0, f"{argv[:3]} failed: {r.stderr[-200:]}")
        return r.stdout.decode()
    sh(["git", "init", "-q", "-b", "main", src])
    sh(["git", "-C", src, "add", "-A"])
    sh(["git", "-C", src, "-c", "user.name=t", "-c", "user.email=t@invalid", "commit", "-qm", "reviewed"])
    commit = sh(["git", "-C", src, "rev-parse", "HEAD"]).strip()
    tree_sha = sh(["git", "-C", src, "rev-parse", "HEAD^{tree}"]).strip()
    root = os.path.join(anchor, "deploy", commit)
    os.makedirs(os.path.join(root, "tree"), mode=0o755)
    sh(["git", "clone", "-q", "--bare", src, os.path.join(root, "repo.git")])
    pathlib.Path(root, "manifest.txt").write_text(
        sh(["git", f"--git-dir={root}/repo.git", "ls-tree", "-r", "--full-tree", commit]))
    archive = subprocess.run(["git", f"--git-dir={root}/repo.git", "-c", "tar.umask=0022", "archive", commit], env=genv,
                             capture_output=True, timeout=300, check=True).stdout
    subprocess.run(["tar", "--no-same-owner", "--no-same-permissions", "-x", "-C", os.path.join(root, "tree")],
                   input=archive, check=True, timeout=300)

    def ok():
        return td.verify(root, commit, tree_sha, owner_uid=uid, stop=anchor)
    ev = ok()
    expect(ev["entrypoint"] == os.path.join(root, "tree", "hardware", "apply-packages.py") and ev["files"] > 20,
           f"evidence {ev}")

    def bad(label, mutate, undo, **kw):
        mutate()
        try:
            td.verify(kw.get("root", root), kw.get("commit", commit), kw.get("tree", tree_sha),
                      owner_uid=kw.get("uid", uid), stop=anchor)
            raise Failure(f"{label}: accepted")
        except td.DeploymentError:
            pass
        finally:
            undo()
        ok()
    t = os.path.join(root, "tree")
    pa_file = os.path.join(t, "hardware", "package_apply.py")
    original = pathlib.Path(pa_file).read_bytes()
    nothing = lambda: None  # noqa: E731
    bad("wrong owner (an unprivileged user's tree)", nothing, nothing, uid=uid + 1)
    bad("group-writable file", lambda: os.chmod(pa_file, 0o664), lambda: os.chmod(pa_file, 0o644))
    bad("other-writable directory", lambda: os.chmod(os.path.join(t, "hardware"), 0o757),
        lambda: os.chmod(os.path.join(t, "hardware"), 0o755))
    bad("writable ancestor", lambda: os.chmod(os.path.join(anchor, "deploy"), 0o777),
        lambda: os.chmod(os.path.join(anchor, "deploy"), 0o755))
    bad("content mismatch", lambda: pathlib.Path(pa_file).write_bytes(original + b"#"),
        lambda: pathlib.Path(pa_file).write_bytes(original))
    evil = os.path.join(t, "hardware", "sitecustomize.py")
    bad("untracked code", lambda: pathlib.Path(evil).write_text("x = 1\n"), lambda: os.unlink(evil))
    bad("executable bit drift", lambda: os.chmod(pa_file, 0o755), lambda: os.chmod(pa_file, 0o644))

    outside = os.path.join(tmpdir("outside-target"), "package_apply.py")

    def to_link():                                   # identical content, but outside the deployment
        os.rename(pa_file, outside)
        os.symlink(outside, pa_file)

    def from_link():
        os.unlink(pa_file)
        os.rename(outside, pa_file)
    bad("symlinked file", to_link, from_link)
    # directory ownership is checked on its own (ancestors and deployment directories)
    for d in (anchor, os.path.join(anchor, "deploy"), t):
        try:
            td.check_dir(d, uid + 1)
            raise Failure(f"directory {d} with a foreign owner accepted")
        except td.DeploymentError:
            pass
    try:
        td.check_ancestors(root, uid + 1, anchor)
        raise Failure("ancestor chain with a foreign owner accepted")
    except td.DeploymentError:
        pass
    alias = os.path.join(anchor, "alias")
    bad("symlinked deployment component", lambda: os.symlink(os.path.join(anchor, "deploy"), alias),
        lambda: os.unlink(alias), root=os.path.join(alias, commit))
    bad("wrong tree", nothing, nothing, tree="0" * 40)
    bad("wrong commit", nothing, nothing, commit="1" * 40)
    man = os.path.join(root, "manifest.txt")
    mtext = pathlib.Path(man).read_text()
    bad("manifest tamper", lambda: pathlib.Path(man).write_text(mtext.replace("100644", "100755", 1)),
        lambda: pathlib.Path(man).write_text(mtext))
    try:
        td.verify(str(REPO), commit, tree_sha, owner_uid=uid, stop=os.path.dirname(str(REPO)))
        raise Failure("the developer checkout was accepted as a deployment")
    except (td.DeploymentError, OSError):
        pass
    # the privileged argv points at the verified deployment, never the developer clone
    argv = td.privileged_argv(root)
    expect(argv == ["/usr/bin/env", "-i", "/usr/bin/python3", "-I", "-B", ev["entrypoint"], "apply"], f"argv {argv}")
    expect(not argv[5].startswith(str(REPO) + "/"), "privileged argv points into the developer clone")
    # imports cannot be redirected by PYTHONPATH, PYTHONHOME, the current directory or user site
    hostile = tmpdir("hostile")
    out = td.check_imports(root, hostile)
    ok()                                            # the probe wrote nothing (no __pycache__) into the deployment
    expect(all(v.startswith(os.path.realpath(t) + "/") for v in out["modules"].values()), f"imports {out}")
    control = subprocess.run([sys.executable, "-c", "import package_apply"], cwd=hostile, capture_output=True,
                             env={"PATH": "/usr/bin:/bin", "PYTHONPATH": hostile}, timeout=60)
    expect(control.returncode != 0 and b"decoy imported" in control.stderr, "decoys are not effective without -I")
    # the launcher verifies first and execs exactly the verified entrypoint (exec intercepted here)
    launched, cwd = [], os.getcwd()
    saved = (td.os.execv, td.os.geteuid, td.verify)
    td.os.execv = lambda path, args: launched.append(list(args))
    td.os.geteuid = lambda: 0
    td.verify = lambda r, c, tr: saved[2](r, c, tr, owner_uid=uid, stop=anchor)
    try:
        td.main(["run-apply", "--root", root, "--commit", commit, "--tree", tree_sha])
        expect(launched == [argv], f"launcher exec {launched}")
        pathlib.Path(pa_file).write_bytes(original + b"#")
        expect(td.main(["run-apply", "--root", root, "--commit", commit, "--tree", tree_sha]) == 1 and len(launched) == 1,
               "launcher executed a tampered deployment")
    finally:
        td.os.execv, td.os.geteuid, td.verify = saved
        pathlib.Path(pa_file).write_bytes(original)
        os.chdir(cwd)
    return (f"exact git deployment verified ({ev['files']} files); 13 owner/mode/symlink/content/identity violations "
            "rejected; imports isolated from hostile PYTHONPATH/cwd; launcher execs only the verified entrypoint")


@check
def no_nvidia_and_policy_unchanged():                                 # 50
    src = (HARDWARE / "package_apply.py").read_text(encoding="utf-8").lower()
    expect("nvidia" not in src and "contrib" not in src and "backports" not in src, "executor names a source")
    expect(sa.POLICY_COMPONENTS == ("main", "non-free-firmware"), "policy")
    pol = json.loads((HARDWARE / "nvidia" / "apt-policy-v1.json").read_text(encoding="utf-8"))
    expect(pol["active"] is False, "NVIDIA policy active")
    packages = {p["name"] for r in gate9.RULES for p in r.get("effects", {}).get("packages", [])}
    expect(len(packages) == 10 and not any("nvidia" in p for p in packages), "production package set")
    return "no NVIDIA/contrib/Backports path; policy main + non-free-firmware; 10 production packages"


def main():
    global TMP
    sys.stdout.reconfigure(line_buffering=True)
    from importlib.metadata import version
    print(f"python {sys.version.split()[0]}, jsonschema {version('jsonschema')}, dpkg-deb {shutil.which('dpkg-deb')}")
    if shutil.which("dpkg-deb") != ps.DPKG_DEB:
        print(f"FAIL: hardware package apply needs {ps.DPKG_DEB} (read-only metadata of synthetic payloads)",
              file=sys.stderr)
        return 1
    failures = 0
    old = os.umask(0o022)
    try:
        with tempfile.TemporaryDirectory(prefix="lubarchy-gate11-") as tmp:
            TMP = os.path.realpath(tmp)
            gate10.TMP = TMP
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
        print(f"FAIL: hardware package apply ({failures} of {len(CHECKS)} checks failed)", file=sys.stderr)
        return 1
    print(f"PASS: hardware package apply ({len(CHECKS)} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
