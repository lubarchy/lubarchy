#!/usr/bin/python3
"""LUBARCHY NVIDIA repository containment policy tool (M1-H; evidence only).

The candidate policy (hardware/nvidia/apt-policy-v1.json) is NOT ACTIVE: no
build script, live-build configuration or production component uses it, and
ADR-0002 remains Proposed. This tool reads files only; it starts no process,
uses no network and never touches the system APT configuration.

Modes:

  check [REPO]
      Validate the committed policy, the rendered preferences example, the
      committed APT fixtures and the repository boundary; exit 1 on any
      problem. This is what static gate 8 runs (through its import).
  render [REPO]
      Print the apt_preferences(5) text rendered from the policy.
  extract CAPTURE-DIR
      Print normalized fixtures (lubarchy.hardware.nvidia-apt-fixtures/v1)
      from an authenticated builder capture (see tests/fixtures/README or
      docs/testing.md). Builder-only requalification.
  freshness STATE-JSON INRELEASE
      Apply the monotonic signed Release Date rule to a new InRelease file.

Policy semantics modelled (apt_preferences(5), verified against APT 3.0.3 on
the builder): for each package version, the first matching *specific* record
(Package names a package) decides the priority; otherwise the first matching
*general* record (Package: *); otherwise the default (500). A priority below
zero makes a version ineligible as a candidate in normal resolution. APT
preferences are not a sandbox or MAC mechanism against root: in APT 3.0.3 an
explicit exact-version administrator request (apt-get install pkg=version)
overrides candidate selection for the named package. The fixtures record
this; it is a privileged override outside the containment boundary.
"""

import email.utils
import fnmatch
import hashlib
import json
import os
import re
import sys

POLICY_SCHEMA = "lubarchy.hardware.nvidia-apt-policy/v1"
FIXTURES_SCHEMA = "lubarchy.hardware.nvidia-apt-fixtures/v1"
EXPECTED_FINGERPRINT = "02182E60104FCDC26EAE1B8597A5D4CB8793F200"
POLICY_PATH = "hardware/nvidia/apt-policy-v1.json"
PREFERENCES_PATH = "hardware/nvidia/apt-preferences.example"
FIXTURES_PATH = "hardware/nvidia/apt-fixtures-v1.json"
EVIDENCE_PATH = "hardware/nvidia/evidence-v1.json"
ADR_PATH = "docs/decisions/0002-nvidia-driver-strategy.md"

# Classes that must stay ineligible from the NVIDIA origin (section 10 of M1-H).
DENIED_CLASSES = ("compute-cuda", "datacenter-fabric", "development", "profiling-debugging",
                  "pinning-repository-management", "generic-infrastructure-overlap",
                  "optional-settings-utility", "other-unreviewed")
ALLOWED_CLASSES = ("desktop-driver-required", "open-kernel-module-required", "driver-support-utility",
                   "gl-egl-vulkan-required")
# Ordered classification rules for NVIDIA package names outside the reviewed closure.
CLASS_RULES = (
    ("pinning-repository-management", r"^(nvidia-driver-pinning-.*|cuda-keyring)$"),
    ("generic-infrastructure-overlap", r"^(dkms|dkms-.*-dkms|dkms-test-dkms|libnvidia-egl-wayland1)$"),
    ("development", r"(-dev|-dev-.*|-headers.*|-static.*)$|^(dh-dkms|eglexternalplatform-dev|nvidia-kernel-source|cuda-nvcc.*|cuda-compiler.*|cuda-minimal-build.*|cuda-cudart.*|cuda-crt.*|cuda-nvrtc.*|cuda-cccl.*|cuda-sandbox-dev.*)$"),
    ("profiling-debugging", r"(nsight|cupti|profiler|sanitizer|cuda-gdb|cudadebugger|nvprof|nvvp|nvtx|nvdebug)"),
    ("datacenter-fabric", r"(fabricmanager|nscq|nvsdm|imex|datacenter-gpu-manager|dcgm|^gds-|nvidia-gds|nvidia-fs|nvlink|^mft|^libnvidia-cfg-dev|nvidia-mig|^cuda-mps$|^collectx|^dps-utils$|^nv-hostengine$|^nvfwupd$|^nvlsm$|^nvvct$)"),
    ("optional-settings-utility", r"^(nvidia-settings|nvidia-xconfig|nvidia-persistenced|nvidia-smi|nvidia-driver-assistant|libxnvctrl.*|nvidia-detect|nvidia-legacy-.*|nvidia-alternative|nvidia-kernel-common|nvidia-support|nvidia-suspend-common|nvidia-egl-common|nvidia-vulkan-common|nvidia-opencl-common|nvidia-installer-cleanup)$"),
    ("compute-cuda", r"(cuda|^cccl|^cublas|^cuquantum|^cusparselt|^cutensor|^cuvs|^nvimgcodec|^nvjpeg2k|^nvshmem|^nvtiff|^libcu|^libnpp|^libnv(jpeg|jitlink|fatbin|ptxcompiler|rtc|vm|comp)|^libnvidia-(nvvm|ptxjitcompiler|opticalflow|encode|fbc|tileiras|sandboxutils|present|pkcs11)|^libnvcuvid|^libnvoptix|^libnccl|^libcudnn|^cudnn|^nvcomp|^libnvcomp|^tensorrt|^libnvinfer|^nvidia-opencl-icd|^libcutensor|^libcusparselt|^nvidia-open$|^nvidia-kernel-dkms$|^nvidia-driver-cuda$|^libnvidia-nvvm|^nvidia-container|^libnvidia-container|^nvidia-cuda)"),
)
INFRA_CHECK = ("dkms", "gcc", "g++", "cpp", "make", "binutils", "linux-headers-amd64", "linux-kbuild-6.12",
               "initramfs-tools", "systemd", "udev", "libc6", "libegl1", "libgl1", "libglx0", "libglvnd0",
               "libopengl0", "libgles2", "libvulkan1", "mesa-vulkan-drivers", "libegl-mesa0", "libgbm1",
               "libdrm2", "xwayland", "xserver-xorg-core", "libwayland-client0", "eglexternalplatform-dev",
               "libnvidia-egl-wayland1", "kmod", "dpkg-dev")


class PolicyError(Exception):
    pass


def expect(cond, message):
    if not cond:
        raise PolicyError(message)


def strict_load(path):
    def no_dupes(pairs):
        keys = [k for k, _ in pairs]
        if len(keys) != len(set(keys)):
            raise PolicyError(f"{path}: duplicate keys")
        return dict(pairs)
    with open(path, encoding="utf-8") as f:
        return json.load(f, object_pairs_hook=no_dupes)


def canonical(doc):
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


# ---------------------------------------------------------------- versions
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
        na = re.match(r"^\D*", a).group(0); nb = re.match(r"^\D*", b).group(0)  # noqa: E702
        a, b = a[len(na):], b[len(nb):]
        for i in range(max(len(na), len(nb))):
            ca = _order(na[i]) if i < len(na) else 0
            cb = _order(nb[i]) if i < len(nb) else 0
            if ca != cb:
                return -1 if ca < cb else 1
        da = re.match(r"^\d*", a).group(0); db = re.match(r"^\d*", b).group(0)  # noqa: E702
        a, b = a[len(da):], b[len(db):]
        ia, ib = int(da or 0), int(db or 0)
        if ia != ib:
            return -1 if ia < ib else 1
    return 0


def version_compare(a, b):
    def split(v):
        epoch, _, rest = v.rpartition(":") if ":" in v else ("0", "", v)
        upstream, _, rev = rest.rpartition("-") if "-" in rest else (rest, "", "0")
        return int(epoch or 0), upstream, rev
    ea, ua, ra = split(a); eb, ub, rb = split(b)  # noqa: E702
    if ea != eb:
        return -1 if ea < eb else 1
    return _cmp_part(ua, ub) or _cmp_part(ra, rb)


# ---------------------------------------------------------------- policy
def load_policy(repo):
    return strict_load(os.path.join(repo, POLICY_PATH))


def records(policy):
    """Ordered apt_preferences records derived from the policy (first specific match wins)."""
    rel = f"release o={policy['repository']['origin']},l={policy['repository']['label']}"
    deny = policy["deny_priority"]; allow = policy["allow_priority"]  # noqa: E702
    out = [{"comment": "Rule D: Debian infrastructure and every other Debian-overlapping NVIDIA name stay Debian",
            "packages": sorted(policy["rules"]["debian_overlap_deny"]), "pin": rel, "priority": deny}]
    out.append({"comment": f"Rules B+C: reviewed desktop/open driver closure, branch {policy['branch']['id']} only",
                "packages": sorted(policy["rules"]["driver_allowlist"]),
                "pin": f"version {policy['branch']['version_glob']}", "priority": allow})
    for name, ver in sorted(policy["rules"]["exact_version_allowlist"].items()):
        out.append({"comment": f"Rules B+C: reviewed closure member with its own versioning, exact version only",
                    "packages": [name], "pin": f"version {ver}", "priority": allow})
    out.append({"comment": "Rule A: every other package from the NVIDIA origin is ineligible",
                "packages": ["*"], "pin": rel, "priority": deny})
    return out


def _wrap(names):
    lines, line = [], ""
    for n in names:
        if line and len(line) + 1 + len(n) > 72:
            lines.append(line); line = n  # noqa: E702
        else:
            line = f"{line} {n}".strip()
    lines.append(line)
    return "\n ".join(lines)


def render(policy):
    head = [
        "# LUBARCHY candidate NVIDIA repository containment policy (apt_preferences(5)).",
        "# NOT ACTIVE. Evidence artifact of M1-H only: not installed by any build",
        "# script, not referenced by live-build, not an authorised package source.",
        "# ADR-0002 is Proposed. Generated by tools/hwe/validate-nvidia-apt-policy.py",
        f"# render from {POLICY_PATH}; do not edit by hand.",
        f"# Repository identity: Origin {policy['repository']['origin']}, Label {policy['repository']['label']},",
        f"# signing key {policy['repository']['signing_key_fingerprint']} (verify the full",
        "# fingerprint through a qualified bootstrap; never trusted=yes).",
        "",
    ]
    body = []
    for r in records(policy):
        body.append(f"Explanation: {r['comment']}\nPackage: {_wrap(r['packages'])}\nPin: {r['pin']}\n"
                    f"Pin-Priority: {r['priority']}\n")
    return "\n".join(head) + "\n" + "\n".join(body)


def _pin_matches(pin, version, release):
    kind, _, value = pin.partition(" ")
    if kind == "version":
        return fnmatch.fnmatchcase(version, value)
    if kind == "release":
        for part in value.split(","):
            key, _, val = part.partition("=")
            field = {"o": "origin", "l": "label", "a": "suite", "n": "codename"}.get(key)
            if field is None or release.get(field) != val:
                return False
        return True
    raise PolicyError(f"unsupported pin {pin!r}")


def priority(recs, package, version, release):
    for r in recs:
        if r["packages"] != ["*"] and package in r["packages"] and _pin_matches(r["pin"], version, release):
            return r["priority"]
    for r in recs:
        if r["packages"] == ["*"] and _pin_matches(r["pin"], version, release):
            return r["priority"]
    return 500


def candidates(recs, versions):
    """versions: {pkg: [{"version", "release": {...}}]} -> {pkg: (version, origin) or None}"""
    out = {}
    for pkg, vs in versions.items():
        best = None
        for v in vs:
            p = priority(recs, pkg, v["version"], v["release"])
            if p < 0:
                continue
            key = (p, v["version"])
            if best is None or p > best[0] or (p == best[0] and version_compare(v["version"], best[1]) > 0):
                best = (p, v["version"], v["release"]["origin"])
        out[pkg] = None if best is None else {"version": best[1], "origin": best[2]}
    return out


# ---------------------------------------------------------------- fixtures model
def version_universe(fx, debian_scope):
    rel_nv = {"origin": fx["release"]["Origin"], "label": fx["release"]["Label"]}
    uni = {}
    for rec in fx["nvidia_records"]:
        uni.setdefault(rec["Package"], []).append({"version": rec["Version"], "release": rel_nv})
    for rec in fx["debian_records"]:
        if debian_scope == "product" and rec["component"] not in ("main", "non-free-firmware"):
            continue
        uni.setdefault(rec["Package"], []).append({"version": rec["Version"],
                                                    "release": {"origin": "Debian", "label": "Debian"}})
    for pkg in uni:
        uni[pkg] = sorted({(v["version"], v["release"]["origin"]): v for v in uni[pkg]}.values(),
                          key=lambda v: (v["release"]["origin"], v["version"]))
    return uni


def classify(name, closure):
    if name in closure:
        return closure[name]
    for cls, pat in CLASS_RULES:
        if re.search(pat, name):
            return cls
    return "other-unreviewed"


# ---------------------------------------------------------------- checks
def check_policy(policy, evidence):
    expect(policy["schema"] == POLICY_SCHEMA, "policy schema id")
    expect(policy["active"] is False and policy["status"] == "NOT-ACTIVE", "policy must be NOT ACTIVE")
    expect(policy["adr"] == {"path": ADR_PATH, "status": "Proposed"}, "policy must reference ADR-0002 Proposed")
    repo = policy["repository"]
    expect(repo["origin"] == "NVIDIA" and repo["label"] == "NVIDIA CUDA", "repository Origin/Label identity")
    expect(repo["uri"] == "https://developer.download.nvidia.com/compute/cuda/repos/debian13/x86_64/", "repository URI")
    expect(repo["signing_key_fingerprint"] == EXPECTED_FINGERPRINT == evidence["nvidia_repo"]["signing_key_fingerprint"],
           "signing fingerprint must match evidence")
    expect(repo["key_material_committed"] is False and repo["trusted_yes"] is False, "key material committed or trusted_yes set")
    expect(isinstance(policy["deny_priority"], int) and policy["deny_priority"] < 0, "deny priority must be < 0")
    expect(isinstance(policy["allow_priority"], int) and 0 < policy["allow_priority"] < 1000,
           "allow priority must be in 1..999 (no forced downgrades, no override of Debian pins)")
    br = policy["branch"]
    expect(br["id"] == "615" and br["version_glob"] == "615.*", "branch lock must be 615.* (qualified branch)")
    rules = policy["rules"]
    allow = set(rules["driver_allowlist"]) | set(rules["exact_version_allowlist"])
    expect(0 < len(allow) <= 40 and "*" not in allow, "allowlist must be finite and explicit")
    expect(all(re.fullmatch(r"[a-z0-9][a-z0-9+.-]+", n) for n in allow), "allowlist entries must be plain package names")
    for name, ver in rules["exact_version_allowlist"].items():
        expect(not any(ch in ver for ch in "*?[]"), f"exact version for {name} must not be a glob")
    expect(set(rules["driver_allowlist"]).isdisjoint(rules["exact_version_allowlist"]), "allowlists overlap")
    for n in sorted(allow):
        cls = classify(n, {})
        expect(cls == "other-unreviewed", f"allowlisted {n} matches the denied class {cls}")
    deny = set(rules["debian_overlap_deny"])
    expect("dkms" in deny and not ("dkms" in allow), "dkms must be denied from NVIDIA")
    expect(deny.isdisjoint(allow), "a Debian-overlap deny entry is also allowlisted")
    expect(rules["origin_default_deny"] is True, "global NVIDIA origin default deny must exist")
    expect(set(policy["denied_classes"]) == set(DENIED_CLASSES), "denied classes")
    expect(set(policy["closure"].values()) <= set(ALLOWED_CLASSES), "closure classes")
    expect(set(policy["closure"]) == allow, "closure classification must cover exactly the allowlist")
    fr = policy["freshness"]
    expect(fr["release_date_monotonic"] is True and fr["valid_until_present"] is False, "freshness rule")
    expect(fr["max_age_days"] is None, "no unjustified maximum-age threshold")
    expect(policy["vendor_pinning_relied_upon"] is False, "vendor pinning must not own containment")
    ca = policy["containment_assessment"]
    expect(ca["result"] == "YES" and ca["statement"].startswith("YES — "), "containment assessment must be YES")
    expect(ca["not_a_root_boundary"] is True, "policy must not be presented as a boundary against root")
    expect(ca["explicit_version_override_observed"] is True and ca["explicit_version_override_in_threat_boundary"] is False,
           "explicit-version override must be recorded as observed and outside the containment boundary")
    expect(ca["authorises_repository"] is False, "containment assessment must not authorise the repository")
    pre = " ".join(ca["activation_prerequisites"])
    for need in ("key bootstrap", "freshness", "executor", "P-006", "ADR-0002"):
        expect(need in pre, f"activation prerequisite missing: {need}")
    expect(len(policy["executor_requirements"]) == 7 and
           any("validated plan" in r for r in policy["executor_requirements"]) and
           any("caller" in r for r in policy["executor_requirements"]), "executor requirements")
    recs = records(policy)
    expect(recs[-1]["packages"] == ["*"] and recs[-1]["priority"] < 0, "last record must be the origin default deny")
    expect(sum(1 for r in recs if r["packages"] == ["*"]) == 1, "exactly one general record")
    for r in recs:
        if r["priority"] >= 0:
            expect(r["pin"].startswith("version ") and r["packages"] != ["*"], "a positive record is not name+version")
            expect(not r["pin"].endswith(" *") and r["pin"] != "version *", "a positive record matches any version")
    return recs


def check_preferences(repo, policy):
    with open(os.path.join(repo, PREFERENCES_PATH), encoding="utf-8") as f:
        text = f.read()
    expect(text == render(policy), "apt-preferences.example is not the rendering of the policy")
    expect(text.startswith("# LUBARCHY candidate") and "# NOT ACTIVE." in text, "preferences header must say NOT ACTIVE")
    expect(not re.search(r"(?m)^(Package|Pin|Pin-Priority):.*trusted", text), "trusted in a record")


def check_fixtures(repo, policy, recs):
    fx = strict_load(os.path.join(repo, FIXTURES_PATH))
    expect(fx["schema"] == FIXTURES_SCHEMA, "fixtures schema id")
    with open(os.path.join(repo, FIXTURES_PATH), encoding="utf-8") as f:
        expect(f.read() == canonical(fx), "fixtures not canonical")
    rel = fx["release"]
    expect(rel["Origin"] == policy["repository"]["origin"] and rel["Label"] == policy["repository"]["label"],
           "fixture Release identity differs from policy")
    expect(rel["signing_key_fingerprint"] == policy["repository"]["signing_key_fingerprint"], "fixture fingerprint")
    for field in ("Suite", "Codename", "Valid-Until"):
        expect(field in rel["absent_fields"], f"absent {field} not recorded")
    expect(rel["Acquire-By-Hash"] == "no", "Acquire-By-Hash state")
    closure = policy["closure"]
    allow = set(closure)
    # 1. model reproduces the recorded APT candidates (product and all-component contexts)
    for scope, key in (("product", "S3"), ("all", "S3X")):
        model = candidates(recs, version_universe(fx, scope))
        observed = fx["apt_observations"][key]["candidates"]
        for pkg, obs in observed.items():
            m = model.get(pkg)
            expect((m is None and obs is None) or (m is not None and obs is not None and m == obs),
                   f"{key}: model candidate for {pkg} {m} != APT {obs}")
    # 2. the contained transaction pulls exactly the closure from NVIDIA, and dkms from Debian
    tx = fx["apt_observations"]["S3"]["desktop_open"]
    expect(tx["exit"] == 0 and set(tx["nvidia"]) == allow, "contained transaction NVIDIA set != closure")
    expect(tx["dkms"]["origin"] == "Debian", "contained transaction does not select Debian dkms")
    expect(fx["apt_observations"]["S1"]["desktop_open"]["dkms"]["origin"] == "NVIDIA",
           "uncontained takeover baseline (NVIDIA dkms) not recorded")
    expect(fx["apt_observations"]["S3"]["candidates"]["dkms"]["origin"] == "Debian", "dkms candidate not Debian")
    # 3. every NVIDIA-named dependency of the closure stays inside closure or Debian
    nv_names = {r["Package"] for r in fx["nvidia_records"]}
    provided = {}
    for r in fx["nvidia_records"]:
        for p in re.split(r",\s*", r.get("Provides", "")):
            if p:
                provided.setdefault(p.split()[0], set()).add(r["Package"])
    branch_glob = policy["branch"]["version_glob"]
    debian_product = {r["Package"] for r in fx["debian_records"] if r["component"] in ("main", "non-free-firmware")}
    for r in fx["nvidia_records"]:
        if r["Package"] not in allow:
            continue
        exact = policy["rules"]["exact_version_allowlist"].get(r["Package"])
        if not (fnmatch.fnmatchcase(r["Version"], exact or branch_glob)):
            continue
        for field in ("Pre-Depends", "Depends"):
            for clause in filter(None, re.split(r",\s*", r.get(field, ""))):
                alts = [a.strip().split()[0].split(":")[0] for a in clause.split("|")]
                ok = any((a not in nv_names and a not in provided) or a in allow or a in debian_product
                         or (provided.get(a, set()) & allow) for a in alts)
                expect(ok, f"{r['Package']} {r['Version']} {field} '{clause}' needs an NVIDIA package outside the closure")
    # 3b. every closure member is reachable from the roots (no unreviewed extras)
    reach, todo = set(), list(policy["roots"])
    while todo:
        n = todo.pop()
        if n in reach:
            continue
        reach.add(n)
        for r in fx["nvidia_records"]:
            exact = policy["rules"]["exact_version_allowlist"].get(r["Package"])
            if r["Package"] != n or not fnmatch.fnmatchcase(r["Version"], exact or branch_glob):
                continue
            for field in ("Pre-Depends", "Depends", "Recommends"):
                for clause in filter(None, re.split(r",\s*", r.get(field, ""))):
                    for a in clause.split("|"):
                        a = a.strip().split()[0].split(":")[0]
                        for target in ({a} | provided.get(a, set())) & allow:
                            todo.append(target)
    expect(reach == allow, f"closure members not reachable from the roots: {sorted(allow - reach)}")
    # 3c. Rule D covers every current Debian-overlapping NVIDIA name outside the closure
    ov_out = {row["package"] for row in fx["overlap"]} - allow
    expect(ov_out <= set(policy["rules"]["debian_overlap_deny"]), f"overlaps missing from Rule D: {sorted(ov_out - set(policy['rules']['debian_overlap_deny']))}")
    # 4. classification: every NVIDIA name outside the closure is in a denied class and ineligible
    classes = fx["classification"]
    expect(len(classes) == fx["nvidia_package_names"] and set(classes) >= nv_names and
           set(classes) == set(fx["apt_observations"]["S3"]["candidates"]),
           "classification does not cover every NVIDIA package name")
    for name in sorted(classes):
        expect(classes[name] == classify(name, closure), f"classification drift for {name}")
        if name not in allow:
            expect(classes[name] in DENIED_CLASSES, f"{name} outside the closure is not in a denied class")
            m = fx["apt_observations"]["S3"]["candidates"].get(name)
            expect(m is None or m["origin"] != "NVIDIA", f"{name} from NVIDIA is eligible under the policy")
    # 5. overlap audit: no unrelated overlapping NVIDIA package wins
    for row in fx["overlap"]:
        expect(row["policy_selects"] != "NVIDIA" or row["package"] in allow,
               f"unrelated overlap {row['package']} selected from NVIDIA")
    for name, row in fx["infrastructure_check"].items():
        expect(row["candidate_origin"] in ("Debian", None), f"infrastructure package {name} not from Debian")
    # 6. branch lock: no non-615 NVIDIA version of a branch-locked package is eligible
    for r in fx["nvidia_records"]:
        if r["Package"] in policy["rules"]["driver_allowlist"] and not fnmatch.fnmatchcase(r["Version"], branch_glob):
            p = priority(recs, r["Package"], r["Version"], {"origin": rel["Origin"], "label": rel["Label"]})
            expect(p < 0, f"branch escape: {r['Package']} {r['Version']} has priority {p}")
    future = priority(recs, "nvidia-driver", "620.10.01-1", {"origin": rel["Origin"], "label": rel["Label"]})
    expect(future < 0, "a future branch (620) would be eligible")
    # 7. recorded APT behaviour that the docs must not overclaim
    ob = fx["apt_observations"]
    expect(ob["S2"]["desktop_open_exit"] != 0, "default-deny profile must make the driver uninstallable")
    for spec, v in ob["S3"]["negative"].items():
        expect(v["nvidia_packages"] == 0, f"denied package {spec} installs from NVIDIA under the contained policy")
    for spec, v in ob["S3"]["branch_escape"].items():
        expect(v["exit"] != 0 and v["nvidia_packages"] == 0, f"explicit branch escape {spec} resolved")
    vp = fx["vendor_pinning"]
    expect(vp["default_deny"] is False and vp["dkms_candidate_origin"] == "NVIDIA" and
           vp["eligible_nvidia_names"] > fx["eligible_nvidia_names"]["S3"], "vendor pinning comparison not recorded")
    expect(fx["eligible_nvidia_names"]["S2"] == 0 and fx["eligible_nvidia_names"]["S3"] == len(allow),
           "eligible NVIDIA name counts")
    expect(ob["explicit_version_override"]["dkms=3.4.3-1"] == "NVIDIA", "explicit-version override not recorded")
    expect(ob["precedence"] == {"deny-first": -1, "allow-first": 500}, "precedence probe not recorded")
    return fx


def check_freshness_rule(policy, fx):
    hist = fx["release_history"]
    state = None
    for obs in hist:
        state = freshness_accept(state, obs)  # raises on regression
    for older in hist[:-1]:
        try:
            freshness_accept(state, older)
        except PolicyError:
            continue
        raise PolicyError("a Release Date regression was accepted")


def freshness_accept(state, new):
    """Monotonic signed Release Date rule. state/new: {"date": RFC 2822, "inrelease_sha256": hex}."""
    nd = email.utils.parsedate_to_datetime(new["date"])
    if state is None:
        return dict(new)
    od = email.utils.parsedate_to_datetime(state["date"])
    if nd < od:
        raise PolicyError(f"Release Date regression: {new['date']} < {state['date']}")
    if nd == od and new["inrelease_sha256"] != state["inrelease_sha256"]:
        raise PolicyError("same Release Date with different signed content")
    return dict(new)


def check_boundary(repo, policy):
    sys.path.insert(0, os.path.join(repo, "hardware"))
    try:
        import resolver  # noqa: PLC0415
    finally:
        sys.path.pop(0)
    expect(tuple(resolver.AUTHORISED_SOURCES) == ("main", "non-free-firmware"), "resolver source policy changed")
    with open(os.path.join(repo, ADR_PATH), encoding="utf-8") as f:
        expect(re.search(r"^Status: Proposed$", f.read(), re.M), "ADR-0002 is not Proposed")
    hits = []
    for root in ("build", ".github", "hardware/rules"):
        for dirpath, _, files in os.walk(os.path.join(repo, root)):
            for fn in files:
                path = os.path.join(dirpath, fn)
                try:
                    text = open(path, encoding="utf-8").read()
                except (UnicodeDecodeError, OSError):
                    continue
                for s in ("apt-policy-v1", "apt-preferences.example", "developer.download.nvidia.com",
                          EXPECTED_FINGERPRINT, "o=NVIDIA"):
                    if s in text:
                        hits.append(f"{os.path.relpath(path, repo)}: {s}")
    expect(not hits, f"inactive policy referenced by build/CI/rules: {hits}")


def check(repo):
    policy = load_policy(repo)
    evidence = strict_load(os.path.join(repo, EVIDENCE_PATH))
    recs = check_policy(policy, evidence)
    check_preferences(repo, policy)
    fx = check_fixtures(repo, policy, recs)
    check_freshness_rule(policy, fx)
    check_boundary(repo, policy)
    return policy, fx


# ---------------------------------------------------------------- extraction (builder captures)
def _deb822(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    out = []
    for st in text.strip().split("\n\n"):
        d, k = {}, None
        for line in st.split("\n"):
            if not line:
                continue
            if line[0] in " \t" and k:
                d[k] += "\n" + line
                continue
            k, _, v = line.partition(":")
            d[k] = v.strip()
        if "Package" in d:
            out.append(d)
    return out


def _policy_table(path):
    """Parse `apt-cache policy` output -> {pkg: {"candidate": ver|None, "versions": {ver: (prio, [sources])}}}."""
    out, pkg, cur = {}, None, None
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            m = re.match(r"^(\S+):$", line)
            if m:
                pkg = m.group(1); out[pkg] = {"candidate": None, "versions": {}}; cur = None  # noqa: E702
                continue
            if pkg is None:
                continue
            m = re.match(r"^  Candidate: (.*)$", line)
            if m:
                out[pkg]["candidate"] = None if m.group(1) == "(none)" else m.group(1)
                continue
            m = re.match(r"^ (?:\*\*\*|   ) (\S+) (-?\d+)$", line)
            if m:
                cur = m.group(1); out[pkg]["versions"][cur] = [int(m.group(2)), []]  # noqa: E702
                continue
            m = re.match(r"^\s+(-?\d+) (\S+)", line)
            if m and cur:
                out[pkg]["versions"][cur][1].append(m.group(2))
    return out


def _cand(table, pkg):
    t = table.get(pkg)
    if not t or t["candidate"] is None:
        return None
    srcs = t["versions"].get(t["candidate"], [0, []])[1]
    origin = "NVIDIA" if any("developer.download.nvidia.com" in s for s in srcs) else "Debian"
    return {"version": t["candidate"], "origin": origin}


def _sim(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    inst = re.findall(r"^Inst (\S+) (?:\[[^]]*\] )?\((\S+) (\S+)", text, re.M)
    return text, inst


def extract(cap):
    rel_text = open(os.path.join(cap, "nvidia-Release-verified"), encoding="utf-8").read()
    rel = {}
    for line in rel_text.splitlines():
        if line and not line.startswith(" ") and ":" in line:
            k, _, v = line.partition(":")
            rel[k] = v.strip()
    meta = strict_load(os.path.join(cap, "capture-meta.json"))
    nv = _deb822(os.path.join(cap, "nvidia-Packages"))
    nv_names = sorted({r["Package"] for r in nv})
    deb = []
    for fn in sorted(os.listdir(os.path.join(cap, "debian-lists"))):
        m = re.search(r"_dists_([^_]+)_([^_]+)_binary-amd64_Packages$", fn)
        if not m:
            continue
        for r in _deb822(os.path.join(cap, "debian-lists", fn)):
            r["_suite"], r["_component"] = m.group(1), m.group(2)
            deb.append(r)
    policy = strict_load(meta["policy_path"])
    closure = policy["closure"]
    overlap_names = sorted(set(nv_names) & {r["Package"] for r in deb})
    keep_nv = set(closure) | set(overlap_names) | set(meta["representative_denied"])
    fields = ("Package", "Version", "Source", "Architecture", "Pre-Depends", "Depends", "Provides", "Breaks")
    nvidia_records = sorted(({k: r[k] for k in fields if k in r} for r in nv if r["Package"] in keep_nv),
                            key=lambda r: (r["Package"], r["Version"], r.get("Architecture", "")))
    nvidia_records = [dict(t) for t in {tuple(sorted(r.items())) for r in nvidia_records}]
    nvidia_records.sort(key=lambda r: (r["Package"], r["Version"], r.get("Architecture", "")))
    debian_names = set(overlap_names) | set(INFRA_CHECK)
    debian_records = sorted(({"Package": r["Package"], "Version": r["Version"],
                              "Source": r.get("Source", r["Package"]).split()[0],
                              "suite": r["_suite"], "component": r["_component"]}
                             for r in deb if r["Package"] in debian_names),
                            key=lambda r: (r["Package"], r["Version"], r["suite"], r["component"]))
    debian_records = [dict(t) for t in sorted({tuple(sorted(r.items())) for r in debian_records})]
    debian_records.sort(key=lambda r: (r["Package"], r["Version"], r["suite"], r["component"]))
    pol = {k: _policy_table(os.path.join(cap, f"policy-{k}-all-nvidia.txt")) for k in ("S1", "S1X", "S2", "S3", "S3X", "SV")}
    infra = _policy_table(os.path.join(cap, "policy-S3X-infra.txt"))
    obs = {}
    for key in ("S1", "S1X", "S2", "S3", "S3X", "SV"):
        obs[key] = {"candidates": {n: _cand(pol[key], n) for n in nv_names}}

    def tx(name):
        text, inst = _sim(os.path.join(cap, name))
        rc = int(re.search(r"^## exit=(\d+)$", text, re.M).group(1))
        nvp = sorted(p for p, v, o in inst if o.startswith("NVIDIA"))
        dk = [(v, o) for p, v, o in inst if p == "dkms"]
        return rc, nvp, dk
    rc, nvp, dk = tx("sim-S3-desktop-open.txt")
    obs["S3"]["desktop_open"] = {"exit": rc, "nvidia": nvp,
                                 "dkms": {"version": dk[0][0], "origin": "NVIDIA" if dk[0][1].startswith("NVIDIA") else "Debian"}}
    rc, nvp, dk = tx("sim-S1-desktop-open.txt")
    obs["S1"]["desktop_open"] = {"exit": rc, "nvidia": nvp,
                                 "dkms": {"version": dk[0][0], "origin": "NVIDIA" if dk[0][1].startswith("NVIDIA") else "Debian"}}
    obs["S2"]["desktop_open_exit"] = tx("sim-S2-desktop-open.txt")[0]
    obs["S3"]["negative"] = {}
    for spec in meta["negative"]:
        rc, nvp, _ = tx(f"sim-S3-neg-{spec}.txt")
        obs["S3"]["negative"][spec] = {"exit": rc, "nvidia_packages": len(nvp)}
    obs["S3"]["branch_escape"] = {}
    for spec in meta["branch_escape"]:
        rc, nvp, _ = tx(f"sim-S3-esc-{spec}.txt")
        obs["S3"]["branch_escape"][spec] = {"exit": rc, "nvidia_packages": len(nvp)}
    obs["explicit_version_override"] = {}
    for spec in meta["explicit_override"]:
        rc, nvp, _ = tx(f"sim-S3-explicit-{spec}.txt")
        obs["explicit_version_override"][spec] = "NVIDIA" if (rc == 0 and spec.split("=")[0] in nvp) else "refused"
    prec = {}
    for k in ("deny-first", "allow-first"):
        t = _policy_table(os.path.join(cap, f"policy-PR-order-{k}-dkms.txt"))
        prec[k] = t["dkms"]["versions"]["3.4.3-1"][0]
    obs["precedence"] = prec
    overlap = []
    for n in overlap_names:
        dv = sorted((r["Version"] for r in debian_records if r["Package"] == n), key=_vkey)
        nvv = sorted({r["Version"] for r in nvidia_records if r["Package"] == n}, key=_vkey)
        srcs = sorted({r.get("Source", n).split()[0] for r in nvidia_records if r["Package"] == n})
        dsrc = sorted({r["Source"] for r in debian_records if r["Package"] == n})
        sel = obs["S3X"]["candidates"].get(n)
        unc = obs["S1X"]["candidates"].get(n)
        overlap.append({"package": n, "debian_version": dv[-1], "nvidia_version": nvv[-1], "debian_source": dsrc,
                        "nvidia_source": srcs, "in_closure": n in closure,
                        "uncontained_selects": None if unc is None else unc["origin"],
                        "policy_selects": None if sel is None else sel["origin"]})
    infra_check = {}
    for n in INFRA_CHECK:
        c = _cand(infra, n)
        infra_check[n] = {"in_nvidia_index": n in nv_names, "candidate_origin": None if c is None else c["origin"],
                          "candidate_version": None if c is None else c["version"]}
    classification = {n: classify(n, closure) for n in nv_names}
    counts = {}
    for c in classification.values():
        counts[c] = counts.get(c, 0) + 1
    return {
        "schema": FIXTURES_SCHEMA,
        "release": {"Origin": rel["Origin"], "Label": rel["Label"], "Date": rel["Date"],
                    "Acquire-By-Hash": rel.get("Acquire-By-Hash"),
                    "absent_fields": sorted(f for f in ("Suite", "Codename", "Valid-Until") if f not in rel),
                    "inrelease_sha256": meta["inrelease_sha256"], "packages_sha256": meta["packages_sha256"],
                    "signing_key_fingerprint": meta["fingerprint"], "uri": meta["uri"]},
        "release_history": meta["release_history"],
        "nvidia_package_names": len(nv_names),
        "classification": classification,
        "classification_counts": dict(sorted(counts.items())),
        "nvidia_records": nvidia_records,
        "debian_records": debian_records,
        "overlap": overlap,
        "infrastructure_check": infra_check,
        "apt_observations": obs,
        "vendor_pinning": dict(meta["vendor_pinning"], **{
            "eligible_nvidia_names": sum(1 for v in obs["SV"]["candidates"].values() if v and v["origin"] == "NVIDIA"),
            "dkms_candidate_origin": obs["SV"]["candidates"]["dkms"]["origin"],
            "cuda_toolkit_candidate_origin": (obs["SV"]["candidates"].get("cuda-toolkit") or {}).get("origin")}),
        "eligible_nvidia_names": {k: sum(1 for v in obs[k]["candidates"].values() if v and v["origin"] == "NVIDIA")
                                  for k in ("S1", "S2", "S3", "SV")},
    }


def _vkey(v):
    import functools
    return functools.cmp_to_key(version_compare)(v)


def main(argv):
    mode = argv[1] if len(argv) > 1 else "check"
    if mode == "check":
        repo = argv[2] if len(argv) > 2 else os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        policy, fx = check(repo)
        print(f"ok: NVIDIA containment policy NOT ACTIVE; {len(policy['closure'])} allowlisted binaries, branch "
              f"{policy['branch']['version_glob']}, {len(fx['nvidia_records'])} NVIDIA and "
              f"{len(fx['debian_records'])} Debian fixture records; model matches recorded APT")
        return 0
    if mode == "render":
        repo = argv[2] if len(argv) > 2 else os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        sys.stdout.write(render(load_policy(repo)))
        return 0
    if mode == "extract" and len(argv) == 3:
        sys.stdout.write(canonical(extract(argv[2])))
        return 0
    if mode == "freshness" and len(argv) == 4:
        state = strict_load(argv[2])
        with open(argv[3], "rb") as f:
            raw = f.read()
        date = re.search(rb"^Date: (.+)$", raw, re.M).group(1).decode().strip()
        new = {"date": date, "inrelease_sha256": hashlib.sha256(raw).hexdigest()}
        freshness_accept(state, new)
        print(f"ok: Release Date {date} accepted (not older than {state['date']})")
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except (PolicyError, OSError, ValueError, KeyError) as e:
        print(f"FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
