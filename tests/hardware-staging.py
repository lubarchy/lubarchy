#!/usr/bin/python3
"""LUBARCHY hardware package staging gate (M1-J; synthetic and offline only).

Exercises hardware/source_activation.py, hardware/package_staging.py and
their CLIs with synthetic APT source trees in temporary directories, the
real builder sources.list sample (tests/fixtures/hardware/staging/),
synthetic Debian packages built deterministically in memory (ar + tar.gz,
fixed timestamps) and the M1-I synthetic package universe rendered by the
gate-9 fake backend. The only external program it runs is
`dpkg-deb --field` on those synthetic payloads (read-only metadata).

It never contacts Debian or NVIDIA, never downloads a .deb, never runs
apt-get, never reads the runner's /etc/apt or hardware and never writes
outside its own temporary directories.
"""

import sys

sys.dont_write_bytecode = True

import ast  # noqa: E402
import copy  # noqa: E402
import gzip  # noqa: E402
import hashlib  # noqa: E402
import importlib.util  # noqa: E402
import inspect  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import pathlib  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import tarfile  # noqa: E402
import tempfile  # noqa: E402

TESTS = pathlib.Path(__file__).resolve().parent
REPO = TESTS.parent
HARDWARE = REPO / "hardware"
SFX = TESTS / "fixtures" / "hardware" / "staging"
sys.path.insert(0, str(HARDWARE))
import package_staging as ps  # noqa: E402
import package_transaction as pt  # noqa: E402
import resolver  # noqa: E402
import source_activation as sa  # noqa: E402

_spec = importlib.util.spec_from_file_location("gate9", TESTS / "hardware-transaction.py")
gate9 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate9)
sys.path.remove(str(HARDWARE))

# Protected contract baseline (shallow-CI safe; no Git history needed). A
# deliberate contract change must update this table in the same reviewed
# commit; an accidental edit fails here.
PROTECTED_SCHEMAS = {
    "inventory-v1.schema.json": "c70b6267dadb0ee7ded156b48133bd14e4a849306651f584f9d70afa5ce3b280",
    "rule-v1.schema.json": "e51aabb3573741ce4f3e137821b9c8841ff35f1c6767981d22fe4f97e79f23c5",
    "plan-v1.schema.json": "fa5f95fdf6fa00502f1923bf96d055e198fa4ff0b6da3764974ffbcde9ce622f",
    "package-transaction-v1.schema.json": "894e7f2481db99a5dd8df310f15a564dcbbe9592bc217ba6523018171333d5dc",
    "source-activation-v1.schema.json": "8e72a8e2257de9d5292436a4cfae7f00f4fa6db9fafd7dcb34900c218f14f077",
    "staged-bundle-v1.schema.json": "f226f04aeee9b316141704c7d0df975e876181d268e14bccc45c048c198ea3a1",
}
KEYRING_SHA = "506b815cbb32d9b6066b4a2aa524071e071761e7e7f68c3ac74f3061ba852017"   # debian-archive-keyring 2025.1
ACT_DIGEST = "sha256:" + "a" * 64
META_DIGEST = "sha256:" + "b" * 64
KEY = sa.KEYRING

# Literals the NVIDIA/trust gate (gate 8) forbids in tracked files are built
# at run time, as gate 8 itself does.
NV_HOST = "developer.download." + "nvidia.com"
NV_KEYRING = "/usr/share/keyrings/" + "cuda-" + "archive-keyring.gpg"
TRUSTED_YES = "trusted" + "=" + "yes"

ONE_LINE_BASELINE = ("deb http://deb.debian.org/debian trixie main\n"
                     "deb http://security.debian.org/debian-security trixie-security main\n"
                     "deb http://deb.debian.org/debian trixie-updates main\n")


class Failure(Exception):
    pass


def expect(cond, message):
    if not cond:
        raise Failure(message)


CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


def rejects(exc, code, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc as e:
        expect(e.code == code, f"expected {code}, got {e.code}: {e.message}")
        return e
    raise Failure(f"expected {code}, but it was accepted")


TMP = None


def tmpdir(name):
    d = os.path.join(TMP, name)
    os.makedirs(d, mode=0o700)
    return d


def src_root(name, sources_list=None, parts=None, raw_parts=None):
    root = tmpdir(name)
    os.makedirs(os.path.join(root, "etc/apt/sources.list.d"))
    if sources_list is not None:
        pathlib.Path(root, "etc/apt/sources.list").write_text(sources_list)
    for n, text in (parts or {}).items():
        pathlib.Path(root, "etc/apt/sources.list.d", n).write_text(text)
    for n, data in (raw_parts or {}).items():
        pathlib.Path(root, "etc/apt/sources.list.d", n).write_bytes(data)
    return root


def compile_root(root):
    return sa.compile_activation(sa.inspect(root), "amd64", KEYRING_SHA)


def tree_hash(root):
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in sorted(os.walk(root)):
        dirnames.sort()
        for f in sorted(filenames):
            p = os.path.join(dirpath, f)
            h.update(p.encode() + b"\0" + pathlib.Path(p).read_bytes())
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Synthetic Debian packages (deterministic) and staging helpers

def _ar_member(name, data):
    hdr = f"{name:<16}{0:<12}{0:<6}{0:<6}{100644:<8}{len(data):<10}`\n".encode()
    return hdr + data + (b"\n" if len(data) % 2 else b"")


def _tgz(files):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for name, data in files:
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.mode, info.uid, info.gid = len(data), 0, 0o644, 0, 0
            tar.addfile(info, io.BytesIO(data))
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as g:
        g.write(raw.getvalue())
    return out.getvalue()


def make_deb(package, version, arch, pad=0):
    control = (f"Package: {package}\nVersion: {version}\nArchitecture: {arch}\nMaintainer: Synthetic <s@invalid>\n"
               f"Description: synthetic test payload\n").encode()
    return (b"!<arch>\n" + _ar_member("debian-binary", b"2.0\n") + _ar_member("control.tar.gz", _tgz([("./control", control)]))
            + _ar_member("data.tar.gz", _tgz([("./usr/share/doc/synthetic", b"x" * pad)])))


def staging_universe(identity=None, extra=None):
    """The M1-I universe with SHA-256/size of the fw-01 closure replaced by
    synthetic payloads. identity: {package: (Package, Version, Architecture)}
    overrides the control fields inside one payload."""
    u = gate9.universe()
    debs = {}
    for name, ver_arch in (("firmware-iwlwifi", None), ("intel-microcode", "3.20251111.1~deb13u1"),
                           ("iucode-tool", None)):
        for v in u["packages"][name]:
            if ver_arch and v["version"] != ver_arch:
                continue
            fields = (identity or {}).get(name, (name, v["version"], v["architecture"]))
            data = make_deb(*fields)
            v["sha256"], v["size"] = hashlib.sha256(data).hexdigest(), len(data)
            debs[ps.payload_filename({"package": name, "version": v["version"], "architecture": v["architecture"]})] = data
            break
    for name, versions in (extra or {}).items():
        u["packages"][name].extend(versions)
    return u, debs


def staging_tx(u):
    return pt.compile_transaction(gate9.inventory("fw-01-intel-iwlwifi"), gate9.RULES, gate9.FakeBackend(u))


class FakeAcquirer:
    """Writes the synthetic payload for each requested operation (optionally
    tampered). Records the requested targets."""

    def __init__(self, debs, tamper=None):
        self.debs, self.tamper, self.targets = dict(debs), tamper, []

    def download(self, op, dest):
        self.targets.append(ps.acquisition_target(op))
        name = ps.payload_filename(op)
        data = self.debs[name]
        pathlib.Path(dest, name).write_bytes(data)
        if self.tamper:
            self.tamper(op, dest, name)


def stage(root_name, u=None, debs=None, tamper=None, tx=None):
    if u is None:
        u, debs = staging_universe()
    tx = tx or staging_tx(u)
    root = tmpdir(root_name)
    acq = FakeAcquirer(debs, tamper)
    bundle, manifest, results = ps.stage_bundle(tx, ACT_DIGEST, META_DIGEST, root, acq, ps.DebInspector())
    return tx, root, bundle, manifest, results, acq


def assert_no_temp_left(root):
    left = [n for n in os.listdir(root) if n.startswith(".staging-")]
    expect(not left, f"temporary staging directories left behind: {left}")


# ---------------------------------------------------------------------------
# Contracts and protected baseline

@check
def schemas_meta_valid_closed_and_protected():
    import jsonschema
    for name in ("source-activation-v1.schema.json", "staged-bundle-v1.schema.json"):
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
        props = set()

        def collect(node):
            if isinstance(node, dict):
                props.update((node.get("properties") or {}).keys())
                for v in node.values():
                    collect(v)
            elif isinstance(node, list):
                for v in node:
                    collect(v)
        collect(schema)
        for bad in props:
            expect(not re.search(r"time|date|created|host|user|command|output|path_abs", bad), f"{name}: field {bad}")
    for name, want in PROTECTED_SCHEMAS.items():
        got = hashlib.sha256((HARDWARE / "schemas" / name).read_bytes()).hexdigest()
        expect(got == want, f"protected contract {name} changed ({got})")
    return f"2 new schemas meta-valid and closed; {len(PROTECTED_SCHEMAS)} protected contract hashes match"


# ---------------------------------------------------------------------------
# Source activation (coverage 1-13)

@check
def accepted_policy_renders_deterministically():              # 1, 2, 12
    root = src_root("det", ONE_LINE_BASELINE)
    c1, c2 = compile_root(root), compile_root(root)
    expect(resolver.canonical_json(c1) == resolver.canonical_json(c2), "contract not deterministic")
    r1, r2 = sa.render(c1), sa.render(c2)
    expect(r1 == r2 and r1.startswith(sa.HEADER), "render not deterministic")
    expect(c1["model"] == "complement_foreign_baseline" and c1["action"] == "create_owned_source", "model/action")
    expect([s["components"] for s in c1["owned_source"]["stanzas"]] == [["non-free-firmware"]] * 2,
           f"owned components {c1['owned_source']['stanzas']}")
    expect({t["component"] for t in c1["post_state"]["targets"]} == {"main", "non-free-firmware"}, "post-state")
    expect(len(c1["post_state"]["targets"]) == 6, "post-state targets")
    expect(b"Signed-By: " + KEY.encode() in r1 and r1.count(b"Types: deb\n") == 2, "rendered stanzas")
    text = r1.decode()
    expect(not re.search(r"contrib|non-free(?!-firmware)|backports|nvidia|trusted", text, re.I), "forbidden text")
    expect([line.split(":")[0] for line in text.splitlines() if line and not line.startswith("#")] ==
           ["Types", "URIs", "Suites", "Components", "Signed-By"] * 2, "field order")
    # owned-complete: no foreign sources -> https, main + non-free-firmware, all suites
    c3 = compile_root(src_root("empty"))
    expect(c3["model"] == "owned_complete" and c3["action"] == "create_owned_source", "owned_complete")
    expect(all(s["uri"].startswith("https://") and s["components"] == ["main", "non-free-firmware"]
               for s in c3["owned_source"]["stanzas"]), "owned_complete stanzas")
    # pre-state digest: comments/whitespace are not semantic, components are
    d1 = c1["pre_state_digest"]
    d2 = compile_root(src_root("det2", "# admin comment\n\n" + ONE_LINE_BASELINE.replace(" main\n", "   main  \n")))[
        "pre_state_digest"]
    d3 = sa.pre_state_digest(sa.inspect(src_root("det3", ONE_LINE_BASELINE.replace("trixie main", "trixie main "
                                                                                  "non-free-firmware", 1))))
    expect(d1 == d2 and d1 != d3, "pre-state digest is not semantic")
    return f"render sha256 {c1['owned_source']['sha256'][:16]}..; owned_complete https; pre-state digest semantic"


@check
def real_builder_topology_is_complemented():
    data = (SFX / "builder-sources.list").read_bytes()
    root = src_root("builder", data.decode())
    c = compile_root(root)
    expect(c["model"] == "complement_foreign_baseline" and c["action"] == "create_owned_source", "builder model")
    uris = sorted({s["uri"] for s in c["owned_source"]["stanzas"]})
    expect(uris == ["http://deb.debian.org/debian", "http://security.debian.org/debian-security"],
           "complement must reuse the foreign URIs exactly (APT merges archives regardless of scheme)")
    expect(c["ownership"]["foreign_paths"] == ["/etc/apt/sources.list"], "foreign paths")
    return "real builder sources.list (one-line, main only, no Signed-By) -> create_owned_source (non-free-firmware)"


@check
def signed_by_and_trust_rules():                             # 3, 4
    rejects(sa.SourceError, "trusted_rejected", sa.parse_one_line,
            f"deb [{TRUSTED_YES}] http://deb.debian.org/debian trixie main\n")
    rejects(sa.SourceError, "trusted_rejected", sa.parse_deb822,
            "Types: deb\nURIs: https://deb.debian.org/debian\nSuites: trixie\nComponents: main\nTrusted: yes\n")
    rejects(sa.SourceError, "unauthorised_source", sa.parse_one_line,
            "deb [signed-by=/etc/apt/keyrings/other.gpg] http://deb.debian.org/debian trixie main\n")
    rejects(sa.SourceError, "signed_by_missing", sa.render_entries,
            [{"uri": "https://deb.debian.org/debian", "suites": ["trixie"], "components": ["main"], "signed_by": None}])
    # foreign sources already providing every policy component but no Signed-By: nothing binds the keyring
    full = ONE_LINE_BASELINE.replace(" main\n", " main non-free-firmware\n")
    rejects(sa.SourceError, "signed_by_missing", compile_root, src_root("nosig", full))
    c = compile_root(src_root("sig", full.replace("deb http", f"deb [signed-by={KEY}] http")))
    expect(c["action"] == "already_satisfied" and c["owned_source"] is None, "fully signed foreign -> satisfied")
    for s in compile_root(src_root("own", ONE_LINE_BASELINE))["owned_source"]["stanzas"]:
        expect(s["signed_by"] == KEY, "owned stanza keyring")
    return "trusted= rejected (one-line, deb822); non-Debian keyring rejected; Signed-By mandatory"


@check
def forbidden_sources_are_rejected():                        # 5-8
    cases = {
        "contrib": "deb http://deb.debian.org/debian trixie main contrib\n",
        "non-free": "deb http://deb.debian.org/debian trixie main non-free\n",
        "backports": "deb http://deb.debian.org/debian trixie-backports main\n",
        "testing": "deb http://deb.debian.org/debian forky main\n",
        "nvidia": f"deb https://{NV_HOST}/compute/cuda/repos/debian13/x86_64 /\n",
        "nvidia-deb822": None,
        "third-party mirror": "deb http://mirror.example.com/debian trixie main\n",
        "lubarchy repository": "deb https://packages.lubarchy.com/debian trixie main\n",
        "ftp": "deb ftp://deb.debian.org/debian trixie main\n",
    }
    for label, line in cases.items():
        if line is None:
            rejects(sa.SourceError, "unauthorised_source", sa.parse_deb822,
                    f"Types: deb\nURIs: https://{NV_HOST}/compute/cuda/repos/debian13/x86_64/\n"
                    f"Suites: /\nComponents: main\nSigned-By: {NV_KEYRING}\n")
            continue
        e = None
        try:
            sa.parse_one_line(line)
        except sa.SourceError as err:
            e = err
        expect(e is not None and e.code in ("unauthorised_source", "malformed_source"), f"{label} accepted")
    # rejected in a full root too (foreign file next to a valid baseline)
    rejects(sa.SourceError, "unauthorised_source", sa.inspect,
            src_root("nv", ONE_LINE_BASELINE, {"cuda.list": cases["third-party mirror"]}))
    return f"{len(cases)} forbidden source definitions rejected (contrib, non-free, backports, NVIDIA, third-party)"


@check
def malformed_and_unsafe_source_state_rejected():            # 9
    for label, text in (("garbage", "this is not a source\n"), ("deb-src", "deb-src http://deb.debian.org/debian trixie main\n"),
                        ("cdrom", "deb cdrom:[Debian]/ trixie main\n"), ("arch option", "deb [arch=amd64] http://deb.debian.org/debian trixie main\n"),
                        ("no components", "deb http://deb.debian.org/debian trixie\n")):
        e = None
        try:
            sa.parse_one_line(text)
        except sa.SourceError as err:
            e = err
        expect(e is not None and e.code in ("malformed_source", "unauthorised_source"), f"{label} accepted")
    for label, text in (("missing field", "Types: deb\nURIs: https://deb.debian.org/debian\nSuites: trixie\n"),
                        ("multi-line", "Types: deb\nURIs: https://deb.debian.org/debian\n https://x\nSuites: trixie\nComponents: main\n"),
                        ("duplicate field", "Types: deb\nTypes: deb\nURIs: https://deb.debian.org/debian\nSuites: trixie\nComponents: main\n"),
                        ("architectures", "Types: deb\nURIs: https://deb.debian.org/debian\nSuites: trixie\nComponents: main\nArchitectures: amd64\n"),
                        ("enabled", "Types: deb\nURIs: https://deb.debian.org/debian\nSuites: trixie\nComponents: main\nEnabled: no\n"),
                        ("deb-src type", "Types: deb deb-src\nURIs: https://deb.debian.org/debian\nSuites: trixie\nComponents: main\n")):
        rejects(sa.SourceError, "malformed_source", sa.parse_deb822, text)
    rejects(sa.SourceError, "malformed_source", sa.inspect, src_root("utf8", None, raw_parts={"x.sources": b"\xff\xfe"}))
    root = src_root("symlink", ONE_LINE_BASELINE)
    os.symlink("/etc/hostname", os.path.join(root, "etc/apt/sources.list.d/link.list"))
    rejects(sa.SourceError, "unsafe_path", sa.inspect, root)
    root = src_root("fifo", ONE_LINE_BASELINE)
    os.mkfifo(os.path.join(root, "etc/apt/sources.list.d/pipe.list"))
    rejects(sa.SourceError, "unsafe_path", sa.inspect, root)
    root = src_root("subdir", ONE_LINE_BASELINE)
    os.mkdir(os.path.join(root, "etc/apt/sources.list.d/sub.list"))
    rejects(sa.SourceError, "unsafe_path", sa.inspect, root)
    root = src_root("listlink", None)
    os.symlink("/etc/hostname", os.path.join(root, "etc/apt/sources.list"))
    rejects(sa.SourceError, "unsafe_path", sa.inspect, root)
    ign = sa.inspect(src_root("ignored", ONE_LINE_BASELINE, {"old.list.save": "garbage\n", "x.list.bak": "garbage\n"}))
    expect(ign["ignored_files"] == ["old.list.save", "x.list.bak"], "APT-ignored files are recorded, not parsed")
    return "garbage, deb-src, cdrom, options, malformed deb822, non-UTF-8, symlink, FIFO, directory rejected"


@check
def ambiguous_ownership_fails_closed():                      # 10
    cases = {
        "partial topology": (ONE_LINE_BASELINE.splitlines()[0] + "\n", {}),
        "duplicate definition": (ONE_LINE_BASELINE, {"dup.list": "deb http://deb.debian.org/debian trixie main\n"}),
        "mixed schemes": (ONE_LINE_BASELINE, {sa.OWNED_NAME: "Types: deb\nURIs: https://deb.debian.org/debian\n"
                                              "Suites: trixie trixie-updates\nComponents: non-free-firmware\n"
                                              f"Signed-By: {KEY}\n"}),
        "unrecognised file": (ONE_LINE_BASELINE, {"vendor.conf": "x\n"}),
        "conflicting Signed-By": (ONE_LINE_BASELINE.replace("deb http://deb.debian.org/debian trixie main",
                                                            "deb [signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] "
                                                            "http://deb.debian.org/debian trixie main"),
                                  {sa.OWNED_NAME: "Types: deb\nURIs: http://deb.debian.org/debian\nSuites: trixie\n"
                                                  f"Components: non-free-firmware\nSigned-By: {KEY}\n"}),
    }
    for label, (sl, parts) in cases.items():
        e = None
        try:
            compile_root(src_root("amb-" + label.replace(" ", "-"), sl, parts))
        except sa.SourceError as err:
            e = err
        expect(e is not None and e.code == "ambiguous_ownership", f"{label}: {e and e.code}")
    # a redundant owned file (nothing required) is ambiguous; an owned file
    # with stale content is replaced (owned only)
    full = ONE_LINE_BASELINE.replace(" main\n", " main non-free-firmware\n").replace("deb http", f"deb [signed-by={KEY}] http")
    rejects(sa.SourceError, "ambiguous_ownership", compile_root,
            src_root("redundant", full, {sa.OWNED_NAME: "# empty\n"}))
    stale = compile_root(src_root("stale-owned", ONE_LINE_BASELINE, {sa.OWNED_NAME: "# old LUBARCHY file\n"}))
    expect(stale["action"] == "replace_owned_source", f"stale owned file action {stale['action']}")
    good = src_root("good-owned", ONE_LINE_BASELINE)
    data = sa.render(compile_root(good))
    pathlib.Path(good, "etc/apt/sources.list.d", sa.OWNED_NAME).write_bytes(data)
    expect(compile_root(good)["action"] == "already_satisfied", "rendered owned file must satisfy the contract")
    return f"{len(cases) + 1} ambiguous topologies rejected; stale owned file -> replace_owned_source; rendered -> satisfied"


@check
def foreign_files_are_never_modified():                      # 11, 37
    root = src_root("foreign", ONE_LINE_BASELINE, {"old.list.save": "x\n"})
    before = tree_hash(root)
    c = compile_root(root)
    out = tmpdir("render-out")
    path = sa.render_to(c, out)
    expect(path == os.path.join(out, sa.OWNED_NAME) and pathlib.Path(path).read_bytes() == sa.render(c), "render_to")
    ws = os.path.join(TMP, "iso-ws")
    conf = sa.isolated_config(c, ws, root)
    expect(tree_hash(root) == before, "inspected source tree was modified")
    text = pathlib.Path(conf).read_text()
    for k in ('Dir::Etc::TrustedParts "' + ws, 'Dir::Etc::Parts "' + ws, 'Dir::Cache "' + ws, 'Dir::Log "' + ws,
              'Dir::State::Lists "' + ws, 'Acquire::AllowInsecureRepositories "false"'):
        expect(k in text, f"isolated config lacks {k}")
    expect(os.listdir(os.path.join(ws, "etc/trusted.gpg.d")) == [], "isolated trusted parts must be empty")
    for bad in ("/etc/apt", "/etc/apt/sources.list.d", "/var/lib/apt/lists", "/var/cache/apt/archives", "/usr/share/x",
                sa.OWNED_PATH, "/var/lib/lubarchy/x", "relative/dir", "/tmp/../etc/apt"):
        rejects(sa.SourceError, "unsafe_path", sa.check_workspace, bad, False)   # never create while probing
    rejects(sa.SourceError, "unsafe_path", sa.render_to, c, out)               # not empty
    return "render/isolated config write only into workspaces; /etc and system APT paths refused; tree unchanged"


@check
def source_drift_makes_activation_stale():                   # 13
    root = src_root("drift", ONE_LINE_BASELINE)
    c = compile_root(root)
    sa.verify_activation(c, root, "amd64", KEYRING_SHA)
    pathlib.Path(root, "etc/apt/sources.list").write_text(ONE_LINE_BASELINE.replace("http://", "https://"))
    rejects(sa.SourceError, "stale_activation", sa.verify_activation, c, root, "amd64", KEYRING_SHA)
    rejects(sa.SourceError, "stale_activation", sa.isolated_config, c, os.path.join(TMP, "drift-ws"), root)
    # drift into an unsigned complete foreign topology fails closed as well
    pathlib.Path(root, "etc/apt/sources.list").write_text(ONE_LINE_BASELINE.replace("trixie-updates main",
                                                                                     "trixie-updates main non-free-firmware"))
    rejects(sa.SourceError, "signed_by_missing", sa.verify_activation, c, root, "amd64", KEYRING_SHA)
    root2 = src_root("drift2", ONE_LINE_BASELINE)
    c2 = compile_root(root2)
    rejects(sa.SourceError, "stale_activation", sa.verify_activation, c2, root2, "amd64", "c" * 64)   # keyring changed
    pathlib.Path(root2, "etc/apt/sources.list.d", sa.OWNED_NAME).write_bytes(sa.render(c2))
    rejects(sa.SourceError, "stale_activation", sa.verify_activation, c2, root2, "amd64", KEYRING_SHA)
    for label, mutate in (("pre-state", lambda d: d["pre_state"]["files"][0]["entries"].pop()),
                          ("policy", lambda d: d["source_policy"]["components"].append("contrib")),
                          ("owned", lambda d: d["owned_source"]["stanzas"][0]["components"].append("main")),
                          ("action", lambda d: d.update(action="already_satisfied"))):
        bad = copy.deepcopy(c2)
        mutate(bad)
        e = None
        try:
            sa.check_activation(bad)
        except sa.SourceError as err:
            e = err
        expect(e is not None, f"{label} tamper accepted")
    return "source, keyring or owned-file drift -> stale; tampered contracts rejected"


@check
def authenticated_metadata_evidence_rules():
    c = compile_root(src_root("meta", ONE_LINE_BASELINE))
    lists = tmpdir("meta-lists")
    recs, inrel = [], {}
    for t in c["post_state"]["targets"]:
        if t["suite"] == "trixie-updates" and t["component"] == "non-free-firmware":
            continue                                                       # empty index: no list file
        host_path = t["uri"].split("://")[1].replace("/", "_")
        fn = os.path.join(lists, f"{host_path}_dists_{t['suite']}_{t['component']}_binary-amd64_Packages")
        pathlib.Path(fn).write_text(f"Package: synthetic-{t['component']}\n")
        label = "Debian-Security" if "security" in t["uri"] else "Debian"
        inrel[(host_path, t["suite"])] = label
        recs.append({"Identifier": "Packages", "Description": f"{t['uri']} {t['suite']}/{t['component']} amd64 Packages",
                     "Base-URI": f"{t['uri']}/dists/{t['suite']}/", "Component": t["component"], "Architecture": "amd64",
                     "Origin": "Debian", "Label": label, "Codename": t["suite"], "Suite": "stable", "Trusted": "yes",
                     "Signed-By": t["signed_by"], "Filename": fn})
    for (host_path, suite), label in inrel.items():
        body = (f"Origin: Debian\nLabel: {label}\nCodename: {suite}\nComponents: main non-free-firmware\nSHA256:\n"
                f" {sa.EMPTY_SHA256}        0 non-free-firmware/binary-amd64/Packages\n")
        pathlib.Path(lists, f"{host_path}_dists_{suite}_InRelease").write_text(body)

    def text(records):
        return "\n\n".join("\n".join(f"{k}: {v}" for k, v in sorted(r.items())) for r in records) + "\n"
    ev = sa.metadata_evidence(c, text(recs))
    expect(len(ev["targets"]) == 6 and sum(t["empty_index"] for t in ev["targets"]) == 1, "evidence targets")
    expect(ev["repository_metadata_digest"] == sa.metadata_evidence(c, text(recs))["repository_metadata_digest"],
           "metadata digest not deterministic")
    pathlib.Path(recs[0]["Filename"]).write_text("Package: changed\n")
    expect(sa.metadata_evidence(c, text(recs))["repository_metadata_digest"] != ev["repository_metadata_digest"],
           "metadata digest does not bind index content")
    for label, mutate in (("untrusted", lambda r: r.update(Trusted="no")),
                          ("other keyring", lambda r: r.update({"Signed-By": "/etc/apt/keyrings/x.gpg"})),
                          ("contrib", lambda r: r.update(Component="contrib")),
                          ("NVIDIA", lambda r: r.update(Origin="NVIDIA", Label="NVIDIA CUDA"))):
        bad = copy.deepcopy(recs)
        mutate(bad[0])
        rejects(sa.SourceError, "metadata_failure", sa.metadata_evidence, c, text(bad))
    # an absent target is accepted only when the authenticated InRelease lists it as empty
    rejects(sa.SourceError, "metadata_failure", sa.metadata_evidence, c, text(recs[1:]))
    upd = [p for p in os.listdir(lists) if "trixie-updates_InRelease" in p][0]
    pathlib.Path(lists, upd).write_text(pathlib.Path(lists, upd).read_text().replace(" 0 non-free", " 7 non-free"))
    rejects(sa.SourceError, "metadata_failure", sa.metadata_evidence, c, text(recs))
    return "targets must equal the post-state, be Debian/Trusted/keyring-signed; empty index only if signed as empty"


# ---------------------------------------------------------------------------
# Staging (coverage 14-36)

@check
def bundle_binds_transaction_and_exact_package_list():       # 14, 15, 27, 28, 32, 33
    tx, root, bundle, m, results, acq = stage("stage-a")
    tx_digest = pt.transaction_digest(tx)
    expect(os.path.basename(bundle) == tx_digest[7:] and m["transaction_digest"] == tx_digest, "transaction binding")
    want = sorted((o["package"], o["architecture"], o["version"]) for o in tx["operations"] if o["action"] == "install")
    got = sorted((p["package"], p["architecture"], p["version"]) for p in m["payloads"])
    expect(got == want and len(got) == 3, f"payload list {got}")
    expect(sorted(os.listdir(os.path.join(bundle, "archives"))) == sorted(p["filename"] for p in m["payloads"]),
           "archive listing")
    rel = {p["package"]: (p["relation"], p["automatic_after"]) for p in m["payloads"]}
    expect(rel == {"firmware-iwlwifi": ("direct", False), "intel-microcode": ("direct", False),
                   "iucode-tool": ("dependency", True)}, f"relations {rel}")
    expect(m["direct_targets"] == ["firmware-iwlwifi:all=20250410-2", "intel-microcode:amd64=3.20251111.1~deb13u1"],
           f"direct targets {m['direct_targets']}")
    argv = ps.offline_apply_argv(m, bundle)
    expect(not any(a.startswith("iucode-tool") for a in argv), "dependency became an explicit target")
    expect(sorted(acq.targets) == sorted(f"{p}:{a}={v}" for p, a, v in want), "acquisition targets")
    assert_no_temp_left(root)
    tx2, _root2, bundle2, m2, _r, _a = stage("stage-b", tx=tx, u=staging_universe()[0], debs=staging_universe()[1])
    expect(ps.manifest_bytes(m) == ps.manifest_bytes(m2), "finalized manifest not deterministic")
    expect(m["bundle_digest"] == m2["bundle_digest"] == ps.compute_bundle_digest(m), "bundle digest")
    for p in m["payloads"]:
        a = pathlib.Path(bundle, "archives", p["filename"]).read_bytes()
        b = pathlib.Path(bundle2, "archives", p["filename"]).read_bytes()
        expect(a == b and hashlib.sha256(a).hexdigest() == p["sha256"], "payload bytes")
    for p in (bundle, os.path.join(bundle, "archives")):
        expect(os.lstat(p).st_mode & 0o7777 == 0o755, "directory mode")
    for p in m["payloads"]:
        expect(os.lstat(os.path.join(bundle, "archives", p["filename"])).st_mode & 0o7777 == 0o644, "file mode")
    ps.verify_bundle(bundle, ps.DebInspector(), expected={"transaction_digest": tx_digest,
                                                          "source_activation_digest": ACT_DIGEST,
                                                          "repository_metadata_digest": META_DIGEST}, tx=tx)
    return f"bundle {m['bundle_digest'][:23]}..: 3 payloads (2 direct manual, 1 dependency automatic); deterministic"


def _tamper_case(label, tamper, code="payload_mismatch"):
    u, debs = staging_universe()
    tx = staging_tx(u)
    root = tmpdir("tamper-" + label)
    e = rejects(ps.StagingError, code, ps.stage_bundle, tx, ACT_DIGEST, META_DIGEST, root,
                FakeAcquirer(debs, tamper), ps.DebInspector())
    assert_no_temp_left(root)
    expect(os.listdir(root) == [], f"{label}: rejected staging left files behind")
    return e


@check
def payload_acquisition_rejections():                         # 16-19, 23-25
    def missing(op, dest, name):
        if op["package"] == "iucode-tool":
            os.unlink(os.path.join(dest, name))

    def extra(op, dest, name):
        pathlib.Path(dest, "evil_1.0_amd64.deb").write_bytes(make_deb("evil", "1.0", "amd64"))

    def bad_hash(op, dest, name):
        if op["package"] == "iucode-tool":
            b = bytearray(pathlib.Path(dest, name).read_bytes())
            b[-3] ^= 0xFF
            pathlib.Path(dest, name).write_bytes(bytes(b))

    def bad_size(op, dest, name):
        if op["package"] == "iucode-tool":
            with open(os.path.join(dest, name), "ab") as f:
                f.write(b"\0")

    def symlink(op, dest, name):
        if op["package"] == "iucode-tool":
            real = os.path.join(TMP, "outside-" + name)
            os.replace(os.path.join(dest, name), real)
            os.symlink(real, os.path.join(dest, name))

    def fifo(op, dest, name):
        if op["package"] == "iucode-tool":
            os.unlink(os.path.join(dest, name))
            os.mkfifo(os.path.join(dest, name))

    def directory(op, dest, name):
        if op["package"] == "iucode-tool":
            os.unlink(os.path.join(dest, name))
            os.mkdir(os.path.join(dest, name))

    def renamed(op, dest, name):
        if op["package"] == "iucode-tool":
            os.replace(os.path.join(dest, name), os.path.join(dest, "iucode-tool_2.3.1-3_all.deb"))

    msgs = {}
    for label, fn in (("missing", missing), ("extra", extra), ("hash", bad_hash), ("size", bad_size),
                      ("symlink", symlink), ("fifo", fifo), ("directory", directory), ("filename", renamed)):
        msgs[label] = _tamper_case(label, fn).message
    expect("missing" in msgs["missing"] and "unexpected" in msgs["extra"], "missing/extra messages")
    expect("SHA-256" in msgs["hash"] and "size" in msgs["size"], "hash/size messages")
    return "missing, extra, wrong SHA-256, wrong size, symlink, FIFO, directory, renamed payload rejected; no residue"


@check
def deb_identity_must_match_the_transaction():               # 20-22
    out = []
    for label, identity in (("package", {"iucode-tool": ("iucode-tool-evil", "2.3.1-3", "amd64")}),
                            ("version", {"iucode-tool": ("iucode-tool", "2.3.1-4", "amd64")}),
                            ("architecture", {"iucode-tool": ("iucode-tool", "2.3.1-3", "i386")}),
                            ("architecture all", {"intel-microcode": ("intel-microcode", "3.20251111.1~deb13u1", "all")})):
        u, debs = staging_universe(identity=identity)
        tx = staging_tx(u)            # the index hash matches the payload; only dpkg-deb identity differs
        root = tmpdir("ident-" + label.replace(" ", "-"))
        e = rejects(ps.StagingError, "payload_mismatch", ps.stage_bundle, tx, ACT_DIGEST, META_DIGEST, root,
                    FakeAcquirer(debs), ps.DebInspector())
        expect(label.split()[0] in e.message, f"{label}: {e.message}")
        out.append(label)
    return f"dpkg-deb identity mismatch rejected: {', '.join(out)} (hash/size matched)"


@check
def finalized_bundle_tamper_is_detected():                    # 23-26, 28
    tx, root, bundle, m, _r, _a = stage("final")
    arch = os.path.join(bundle, "archives")
    names = sorted(os.listdir(arch))
    snapshot = {n: pathlib.Path(arch, n).read_bytes() for n in names}
    man = pathlib.Path(bundle, "manifest.json").read_bytes()

    def restore():
        for n in os.listdir(arch):
            p = os.path.join(arch, n)
            if os.path.isdir(p) and not os.path.islink(p):
                os.rmdir(p)
            else:
                os.unlink(p)
        for n, data in snapshot.items():
            pathlib.Path(arch, n).write_bytes(data)
            os.chmod(os.path.join(arch, n), 0o644)
        pathlib.Path(bundle, "manifest.json").write_bytes(man)
        pathlib.Path(bundle, "notes.txt").unlink(missing_ok=True)

    def check_rejects(label, mutate, codes=("payload_mismatch",)):
        mutate()
        e = None
        try:
            ps.verify_bundle(bundle, ps.DebInspector())
        except ps.StagingError as err:
            e = err
        restore()
        expect(e is not None and e.code in codes, f"{label}: {e and e.code}")
        ps.verify_bundle(bundle, ps.DebInspector())

    first = os.path.join(arch, names[0])
    check_rejects("replacement", lambda: pathlib.Path(first).write_bytes(make_deb("x", "1", "all")))
    check_rejects("truncation", lambda: os.truncate(first, 10))
    check_rejects("addition", lambda: pathlib.Path(arch, "extra_1_all.deb").write_bytes(b"x"))
    check_rejects("removal", lambda: os.unlink(first))

    def swap():
        a, b = os.path.join(arch, names[0]), os.path.join(arch, names[1])
        os.rename(a, a + ".t")
        os.rename(b, a)
        os.rename(a + ".t", b)
    check_rejects("filename swap", swap)

    def to_symlink():
        os.replace(first, first + ".real")
        os.symlink(first + ".real", first)
    check_rejects("symlink", lambda: (to_symlink(), None))
    check_rejects("mode", lambda: os.chmod(first, 0o666))

    def manifest_edit(fn):
        def go():
            d = json.loads(man)
            fn(d)
            pathlib.Path(bundle, "manifest.json").write_bytes(ps.manifest_bytes(d))
        return go
    check_rejects("manifest hash", manifest_edit(lambda d: d["payloads"][0].update(sha256="0" * 64)),
                  ("payload_mismatch", "contract_failure"))
    check_rejects("manifest binding edit (bundle digest stale)", manifest_edit(
        lambda d: d.update(repository_metadata_digest="sha256:" + "c" * 64)))
    check_rejects("manifest digest recomputed", manifest_edit(
        lambda d: (d["payloads"][0].update(size=1), d.update(bundle_digest=ps.compute_bundle_digest(d)))))
    check_rejects("manifest not canonical", lambda: pathlib.Path(bundle, "manifest.json").write_bytes(
        json.dumps(json.loads(man), indent=1).encode()))
    check_rejects("manifest extra entry", lambda: pathlib.Path(bundle, "notes.txt").write_text("x"))
    return "replacement, truncation, addition, removal, swap, symlink, mode, manifest edits detected after finalization"


@check
def stale_bindings_require_restaging():                       # 29-31
    tx, root, bundle, m, _r, _a = stage("fresh")
    tx_digest = pt.transaction_digest(tx)
    fresh = {"transaction_digest": tx_digest, "source_activation_digest": ACT_DIGEST,
             "repository_metadata_digest": META_DIGEST}
    expect(ps.final_revalidation(m, fresh) == "accept", "identical fresh state must be accepted")
    for key in fresh:
        changed = dict(fresh, **{key: "sha256:" + "c" * 64})
        rejects(ps.StagingError, "stale_bundle", ps.final_revalidation, m, changed)
        rejects(ps.StagingError, "stale_bundle", ps.verify_bundle, bundle, ps.DebInspector(),
                expected={key: changed[key]})
    # a newer Debian security candidate appears after staging
    u, debs = staging_universe()
    newer = copy.deepcopy(u["packages"]["intel-microcode"][0])
    newer.update(version="3.20261001.1~deb13u1", repositories=["trixie-security-non-free-firmware"], sha256="d" * 64)
    u2 = copy.deepcopy(u)
    u2["packages"]["intel-microcode"].append(newer)
    fresh_tx = staging_tx(u2)
    expect(ops_version(fresh_tx, "intel-microcode") == "3.20261001.1~deb13u1", "security candidate not selected")
    rejects(ps.StagingError, "stale_bundle", ps.final_revalidation, m,
            dict(fresh, transaction_digest=pt.transaction_digest(fresh_tx)))
    rejects(ps.StagingError, "stale_bundle", ps.verify_bundle, bundle, ps.DebInspector(), tx=fresh_tx)
    rejects(pt.TransactionError, "stale_transaction", pt.verify_transaction, tx, gate9.inventory("fw-01-intel-iwlwifi"),
            gate9.RULES, gate9.FakeBackend(u2))
    return "changed transaction / activation / metadata digest -> stale; newer security candidate -> restage"


def ops_version(tx, name):
    return next(o["version"] for o in tx["operations"] if o["package"] == name)


# ---------------------------------------------------------------------------
# Authority boundaries (coverage 33-40)

@check
def caller_has_no_package_source_or_path_authority():        # 34-36
    for fn, params in ((ps.stage_bundle, ["tx", "source_activation_digest", "repository_metadata_digest", "root",
                                          "acquirer", "inspector"]),
                       (ps.build_manifest, ["tx", "source_activation_digest", "repository_metadata_digest"]),
                       (sa.compile_activation, ["state", "architecture", "keyring_sha256"]),
                       (sa.render_entries, ["stanzas"])):
        expect(list(inspect.signature(fn).parameters) == params, f"{fn.__name__} signature")
    cli_s, cli_p = HARDWARE / "compile-source-activation.py", HARDWARE / "stage-packages.py"
    env = {"PATH": "/usr/bin:/bin"}
    for cli, argv in ((cli_s, ["compile", "--uri", "https://evil"]), (cli_s, ["compile", "--component", "contrib"]),
                      (cli_s, ["inspect", "--root", "/tmp"]), (cli_s, ["activate"]), (cli_s, ["apply"]),
                      (cli_s, ["render", "--contract", "-"]),
                      (cli_p, ["stage", "--workspace", "/tmp/x", "--package", "evil"]),
                      (cli_p, ["stage", "--workspace", "/tmp/x", "--version", "1"]),
                      (cli_p, ["stage", "--workspace", "/tmp/x", "-o", "APT::x=1"]), (cli_p, ["apply"]),
                      (cli_p, ["stage"]), (cli_p, ["install", "intel-microcode"])):
        r = subprocess.run([sys.executable, "-I", str(cli), *argv], env=env, capture_output=True, timeout=60)
        expect(r.returncode == 64 and b"usage:" in r.stderr, f"{cli.name} {argv} -> {r.returncode}")
    for argv in (["stage", "--workspace", "/etc/apt"], ["stage", "--workspace", "/var/lib/lubarchy/package-staging"],
                 ["stage", "--workspace", "relative"], ["verify", "--workspace", "/var/cache/apt",
                                                         "--transaction-digest", "sha256:" + "0" * 64]):
        r = subprocess.run([sys.executable, "-I", str(cli_p), *argv], env=env, capture_output=True, timeout=60)
        expect(r.returncode == ps.EXIT_UNSAFE and b"unsafe_path" in r.stderr, f"{argv} -> {r.returncode} {r.stderr}")
    for bad in ("/etc", "/var/lib/lubarchy/package-staging", "/var/cache/apt/archives", "/tmp/../etc", "x",
                ps.STAGING_ROOT):
        rejects(ps.StagingError, "unsafe_path", ps.check_staging_root, bad)
    # The prefix check itself (independent of ownership): a caller-owned
    # directory under a protected prefix is refused, and the system prefixes
    # are all protected.
    required = {"/etc", "/usr", "/boot", "/var/lib/apt", "/var/lib/dpkg", "/var/cache/apt", "/var/lib/lubarchy",
                "/var/log", "/proc", "/sys", "/dev", "/run"}
    for mod in (ps, sa):
        expect(required <= set(mod.PROTECTED_PREFIXES), f"{mod.__name__}: protected prefixes {mod.PROTECTED_PREFIXES}")
        guarded = tmpdir(f"protected-{mod.__name__}")
        inside = os.path.join(guarded, "workspace")
        os.mkdir(inside, 0o700)
        saved = mod.PROTECTED_PREFIXES
        mod.PROTECTED_PREFIXES = saved + (guarded,)
        try:
            if mod is ps:
                rejects(ps.StagingError, "unsafe_path", ps.check_staging_root, inside)
            else:
                rejects(sa.SourceError, "unsafe_path", sa.check_workspace, inside)
        finally:
            mod.PROTECTED_PREFIXES = saved
    linked = os.path.join(TMP, "linked-root")
    os.symlink(tmpdir("real-root"), linked)
    rejects(ps.StagingError, "unsafe_path", ps.check_staging_root, linked)
    for bad in ("sha256:../../etc", "../x", "sha256:" + "0" * 63 + "/", "md5:" + "0" * 64):
        rejects(ps.StagingError, "input_malformed", ps.bundle_name, bad)
    # a tampered transaction cannot inject packages or versions into a bundle
    u, debs = staging_universe()
    tx = staging_tx(u)
    evil = copy.deepcopy(tx)
    evil["operations"].append(dict(evil["operations"][0], package="evil", relation="dependency", automatic_after=True))
    evil["operations"].sort(key=lambda o: (o["package"], o["architecture"]))
    expect(pt.transaction_digest(evil) != pt.transaction_digest(tx), "tampered transaction keeps its digest")
    rejects(pt.TransactionError, "stale_transaction", pt.verify_transaction, evil,
            gate9.inventory("fw-01-intel-iwlwifi"), gate9.RULES, gate9.FakeBackend(u))
    bad = copy.deepcopy(tx)
    bad["operations"][0]["version"] = "1.0 --allow-downgrades"
    rejects(pt.TransactionError, "contract_failure", ps.expected_payloads, bad)
    for argv in ([ps.APT_GET, "--quiet", "download", "evil"], [ps.APT_GET, "--quiet", "download", "x:amd64=1", "y"],
                 [ps.APT_GET, "--quiet", "download", "x=1"], [ps.APT_GET, "download", "x:amd64=1"]):
        expect(not ps._allowed(argv), f"{argv} allowed")
    return "fixed signatures; CLIs reject package/version/URI/component/option/root input; staging root escape refused"


@check
def no_install_no_dpkg_install_no_shell():                   # 37-40
    for argv in ([ps.APT_GET, "install", "x"], [ps.APT_GET, "--quiet", "install", "x:amd64=1"],
                 [ps.APT_GET, "--download-only", "install", "x"], ["/usr/bin/dpkg", "-i", "x.deb"],
                 ["/usr/bin/dpkg", "--install", "x.deb"], [ps.DPKG_DEB, "--extract", "/dev/fd/3", "/"],
                 [ps.DPKG_DEB, "--field", "/tmp/x.deb", "Package", "Version", "Architecture"],
                 [ps.DPKG_DEB, "-x", "/dev/fd/3", "/tmp"], ["/bin/sh", "-c", "true"], "apt-get install x"):
        expect(not ps._allowed(argv) if isinstance(argv, list) else True, f"{argv} allowed")
        rejects(ps.StagingError, "contract_failure", ps._run, argv if isinstance(argv, list) else [argv], ps.COMMAND_ENV)
    for argv in ([sa.APT_GET, "install", "x"], [sa.APT_GET, "update", "-o", "x"], ["/usr/bin/apt", "modernize-sources"],
                 [sa.APT_GET, "--simulate", "install", "x"]):
        rejects(sa.SourceError, "contract_failure", sa._run, argv, sa.COMMAND_ENV)
    for mod in ("package_staging.py", "source_activation.py", "stage-packages.py", "compile-source-activation.py"):
        src = (HARDWARE / mod).read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
                expect(name not in ("system", "popen", "Popen", "check_output", "call", "check_call", "execv",
                                    "execve", "spawnv", "rmtree"), f"{mod}: {name}()")
                if name == "run":
                    kw = {k.arg: k.value for k in node.keywords}
                    expect(isinstance(kw.get("shell"), ast.Constant) and kw["shell"].value is False,
                           f"{mod}: subprocess.run without shell=False")
        for pat in (r"dpkg\"?,?\s*\"?(-i|--install|--unpack)\b", r"\"--extract\"", TRUSTED_YES + "\"", r"\[trusted",
                    r"os\.system", r"shell=True", r"/etc/apt/sources\.list\.d\"\s*,\s*\"w"):
            expect(not re.search(pat, src), f"{mod} matches {pat}")
    # the offline argv is constructed, never executed: no subprocess call can carry it
    src = (HARDWARE / "package_staging.py").read_text(encoding="utf-8")
    expect(src.count("subprocess.run(") == 1, "package_staging has more than the allowlisted runner")
    u, debs = staging_universe()
    tx, root, bundle, m, _r, _a = stage("argv", u=u, debs=debs)
    argv = ps.offline_apply_argv(m, bundle)
    expect(argv[:7] == list(ps.OFFLINE_APPLY_BASE_ARGV) and argv[7:9] == ["-o", f"Dir::Cache::archives={bundle}/archives/"]
           and argv[9:] == m["direct_targets"], f"offline argv {argv}")
    for a in ("--allow-unauthenticated", "--allow-downgrades", "--force-yes", "--allow-insecure-repositories",
              "--reinstall", "--fix-broken", "--download-only"):
        expect(a not in argv and a in ps.OFFLINE_FORBIDDEN, f"{a} not forbidden")
    expect("--no-download" in argv and not any("trusted" in a.lower() for a in argv), "offline argv network/trust")
    rejects(ps.StagingError, "unsafe_path", ps.offline_apply_argv, m, os.path.join(TMP, "elsewhere"))
    return "allowlists refuse install/dpkg -i/extract/shell; no shell; offline argv constructed only (direct, --no-download)"


@check
def cleanup_is_bounded():                                     # 29 (garbage collection boundary)
    tx, root, bundle, m, _r, _a = stage("cleanup")
    victim = tmpdir("victim")
    pathlib.Path(victim, "keep").write_text("keep")
    os.symlink(victim, os.path.join(bundle, "archives", "zz-link"))
    rejects(ps.StagingError, "payload_mismatch", ps.verify_bundle, bundle, ps.DebInspector())
    expect(ps.remove_bundle(root, m["transaction_digest"]) is True, "bundle not removed")
    expect(pathlib.Path(victim, "keep").read_text() == "keep" and not os.path.exists(bundle), "cleanup followed a link")
    expect(ps.remove_bundle(root, m["transaction_digest"]) is False, "second removal")
    tx2, root2, bundle2, m2, _r2, _a2 = stage("cleanup2")
    os.mkdir(os.path.join(bundle2, "archives", "nested"))
    rejects(ps.StagingError, "unsafe_path", ps.remove_bundle, root2, m2["transaction_digest"])
    expect(os.path.exists(bundle2), "aborted cleanup must not remove the bundle")
    for bad_root in (ps.STAGING_ROOT, "/etc/apt", "/"):
        rejects(ps.StagingError, "unsafe_path", ps.remove_bundle, bad_root, m2["transaction_digest"])
    rejects(ps.StagingError, "input_malformed", ps.remove_bundle, root2, "sha256:../../victim")
    return "removal is exact-digest, entry-by-entry, never follows links, aborts on unknown layout, refuses system roots"


@check
def existing_contracts_and_policy_unchanged():
    expect(sa.POLICY_COMPONENTS == ("main", "non-free-firmware") == tuple(resolver.AUTHORISED_SOURCES), "components")
    expect(pt.AUTHORISED_RELEASES == (("Debian", "Debian", "trixie"), ("Debian", "Debian", "trixie-updates"),
                                      ("Debian", "Debian-Security", "trixie-security")), "releases")
    pol = json.loads((HARDWARE / "nvidia" / "apt-policy-v1.json").read_text(encoding="utf-8"))
    expect(pol["active"] is False, "NVIDIA policy active")
    packages = {p["name"] for r in gate9.RULES for p in r.get("effects", {}).get("packages", [])}
    expect(packages == gate9.PRODUCTION_PACKAGES and len(packages) == 10, "production package set")
    for mod in ("source_activation.py", "package_staging.py"):
        src = (HARDWARE / mod).read_text(encoding="utf-8").lower()
        expect("nvidia" not in src and "backports" not in src, f"{mod} names NVIDIA or Backports")
    return "policy main + non-free-firmware, Debian releases only, 10 production packages, NVIDIA policy inactive"


def main():
    global TMP
    sys.stdout.reconfigure(line_buffering=True)
    from importlib.metadata import version
    dpkg_deb = shutil.which("dpkg-deb")
    print(f"python {sys.version.split()[0]}, jsonschema {version('jsonschema')}, dpkg-deb {dpkg_deb}")
    if dpkg_deb != ps.DPKG_DEB:
        print(f"FAIL: hardware package staging needs {ps.DPKG_DEB} (read-only metadata of synthetic payloads)",
              file=sys.stderr)
        return 1
    failures = 0
    old = os.umask(0o022)
    try:
        with tempfile.TemporaryDirectory(prefix="lubarchy-gate10-") as tmp:
            TMP = os.path.realpath(tmp)
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
        print(f"FAIL: hardware package staging ({failures} of {len(CHECKS)} checks failed)", file=sys.stderr)
        return 1
    print(f"PASS: hardware package staging ({len(CHECKS)} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
