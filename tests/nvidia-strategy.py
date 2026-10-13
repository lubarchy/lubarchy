#!/usr/bin/python3
"""LUBARCHY NVIDIA driver strategy gate (M1-G; committed evidence only).

Checks the NVIDIA strategy evidence and the policy boundary around it:

- hardware/nvidia/evidence-v1.json is well-formed, complete and internally
  consistent (strategies, scores, tiers, generation matrix, simulations);
- the supported-GPU snapshot is canonical, self-consistent, provenance-
  hashed and agrees with the evidence's generation matrix;
- ADR-0002 is Proposed (not Accepted) and the decision register records no
  accepted NVIDIA decision;
- no production rule, package mapping, source policy, live-build or CI
  source references an NVIDIA driver, NVIDIA repository or NVIDIA key, and
  class.gpu.nvidia stays classification-only;
- the Debian 550 strategies are never recommended or automatic while open
  security issues are recorded, and no proprietary path is automatic while
  Secure Boot (P-006) is undecided;
- Nouveau is recorded and described as a baseline/fallback candidate, never
  as a qualified default: per-generation status is fallback candidate,
  limited or not yet qualified (Blackwell), and nothing is production-
  qualified without real-hardware evidence;
- the NVIDIA signing-key wording is precise: the 404 fallback URL and the
  index-exposed 8793F200.pub are recorded, neither is a trust anchor, and a
  future bootstrap must verify the full fingerprint.

It reads committed files only; it downloads nothing and needs no
dependency beyond the existing static gates.
"""

import sys

sys.dont_write_bytecode = True

import importlib.util  # noqa: E402
import json  # noqa: E402
import pathlib  # noqa: E402
import re  # noqa: E402
import subprocess  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parent.parent
HARDWARE = REPO / "hardware"
NVIDIA = HARDWARE / "nvidia"
EVIDENCE = NVIDIA / "evidence-v1.json"
SNAPSHOT = NVIDIA / "supported-gpus-v1.json"
TOOL = REPO / "tools" / "hwe" / "normalize-nvidia-gpus.py"
ADR = REPO / "docs" / "decisions" / "0002-nvidia-driver-strategy.md"
REGISTER = REPO / "docs" / "decisions.md"

sys.path.insert(0, str(HARDWARE))
import resolver  # noqa: E402

sys.path.remove(str(HARDWARE))

spec = importlib.util.spec_from_file_location("lubarchy_normalize_nvidia", TOOL)
normalize = importlib.util.module_from_spec(spec)
spec.loader.exec_module(normalize)

STRATEGIES = ("A", "B", "C", "D")
SCORE_KEYS = ("open_security_issues", "security_update_channel", "provenance", "builds_on_current_kernel",
              "gpu_coverage", "wayland", "frankendebian_risk", "secure_boot_independence")
SUPPORT_CLASSES = ("legacy-older", "legacy-470", "legacy-580", "open-or-proprietary", "open-only")
NOUVEAU_STATUS = {"legacy-older": "limited", "legacy-470": "limited", "legacy-580": "limited",
                  "open-or-proprietary": "fallback-candidate", "open-only": "not-yet-qualified"}
FINGERPRINT = "02182E60104FCDC26EAE1B8597A5D4CB8793F200"
PROSE = [REPO / "docs" / n for n in ("project-state.md", "hardware-enablement.md", "security.md", "testing.md",
                                     "architecture.md", "decisions.md")] + [NVIDIA / "README.md"]
NOUVEAU_OVERCLAIMS = (
    r"Nouveau[^.\n|]*\bis the default",
    r"Nouveau[^.\n|]*\bas (the )?default",
    r"Nouveau for every NVIDIA GPU",
    r"default-and-fallback",
    r"only Nouveau (is|may be) automatic",
    r"Nouveau[^.\n|]*\bqualified default(?<!not a qualified default)(?<!never a qualified default)",
    r"run on Mesa like on any\s+other GPU",
)
KEY_OVERCLAIMS = (
    r"only (inside|in|from|via|through) (the )?`?cuda-keyring",
    r"key via HTTPS only",
    r"no second[- ]channel",
    r"publishes no fingerprint",
)
NVIDIA_DRIVER_PACKAGES = re.compile(
    r"^(nvidia-.*|libnvidia-.*|firmware-nvidia-.*|cuda-.*|libcuda.*|xserver-xorg-video-nvidia.*|nvidia)$")
NVIDIA_STRINGS = ("developer.download.nvidia.com", "02182E60104FCDC26EAE1B8597A5D4CB8793F200", "cuda-keyring",
                  "cuda-archive-keyring")
PRODUCTION_PACKAGE_SET = {"intel-microcode", "amd64-microcode", "firmware-iwlwifi", "firmware-intel-graphics",
                          "firmware-amd-graphics", "firmware-brcm80211", "firmware-realtek", "firmware-atheros",
                          "firmware-mediatek", "bluez-firmware"}


class Failure(Exception):
    pass


def expect(cond, message):
    if not cond:
        raise Failure(message)


CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


def strict_json(path):
    def no_dupes(pairs):
        keys = [k for k, _ in pairs]
        if len(keys) != len(set(keys)):
            raise Failure(f"{path.name}: duplicate keys")
        return dict(pairs)
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=no_dupes)


def adr_section(start, end):
    text = ADR.read_text(encoding="utf-8")
    expect(start in text and end in text, f"ADR-0002 lacks section {start!r}")
    body = text[text.index(start):]
    return " ".join(body[:body.index(end)].split())


def evidence():
    return strict_json(EVIDENCE)


def tracked_files():
    out = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z"], check=True, capture_output=True).stdout
    return [REPO / p for p in out.decode("utf-8").split("\0") if p]


@check
def evidence_is_complete_and_canonical():
    ev = evidence()
    expect(ev["schema"] == "lubarchy.hardware.nvidia-evidence/v1", "schema id")
    expect(ev["status"] == "evidence-only" and ev["milestone"] == "M1-G", "status/milestone")
    expect(ev["production_effect"] == {"rules": [], "packages": [], "sources": [], "classifications_added": []},
           "evidence declares a production effect")
    expect(ev["authorised_sources_unchanged"] == ["main", "non-free-firmware"], "authorised sources")
    for key in ("sources", "debian", "nvidia_repo", "gpu_snapshot", "generation_matrix", "desktop", "secure_boot",
                "strategies", "tiers", "hybrid", "simulations"):
        expect(key in ev, f"missing {key}")
    for src in ev["sources"]:
        expect(src["url"].startswith("https://"), f"non-HTTPS source {src}")
    text = EVIDENCE.read_text(encoding="utf-8")
    expect(text == json.dumps(ev, indent=2, sort_keys=True) + "\n", "evidence not canonical (indent 2, sorted keys)")
    return f"{len(ev['sources'])} sources, {len(ev['simulations']['results'])} simulation results"


@check
def strategies_scores_and_tiers_are_consistent():
    ev = evidence()
    st = ev["strategies"]
    expect(tuple(sorted(st)) == STRATEGIES, f"strategies {sorted(st)}")
    for sid, s in st.items():
        expect(tuple(sorted(s["scores"])) == tuple(sorted(SCORE_KEYS)), f"{sid}: score keys")
        expect(all(isinstance(v, int) and 0 <= v <= 3 for v in s["scores"].values()), f"{sid}: score range")
        expect(isinstance(s["recommended"], bool) and isinstance(s["automatic_default"], bool), f"{sid}: flags")
        expect(not s["automatic_default"] or s["recommended"], f"{sid}: automatic but not recommended")
    expect([sid for sid, s in st.items() if s["automatic_default"]] == [], "no strategy may be an automatic default")
    expect(st["D"]["role"] == "baseline-fallback-candidate", "Nouveau must be a baseline/fallback candidate")
    for tier in ev["tiers"]:
        expect(tier["strategy"] in st and st[tier["strategy"]]["recommended"], f"tier uses {tier['strategy']}")
        expect(set(tier["support_classes"]) <= set(SUPPORT_CLASSES), f"tier classes {tier['support_classes']}")
    expect(all(t["production_qualified"] is False for t in ev["tiers"]), "a tier claims production qualification")
    tier0 = [t for t in ev["tiers"] if t["tier"] == "0"]
    expect(len(tier0) == 1 and tier0[0]["mode"] == "baseline-fallback-candidate", "tier 0 must be a candidate")
    expect(len(tier0) == 1 and set(tier0[0]["support_classes"]) == set(SUPPORT_CLASSES), "tier 0 covers every class")
    for t in ev["tiers"]:
        if t["strategy"] == "C":
            expect(t["mode"] == "opt-in" and t["status"] == "Proposed", "strategy C must stay an opt-in proposal")
            expect(not {"legacy-older", "legacy-470", "legacy-580"} & set(t["support_classes"]),
                   "strategy C offered to GPUs its repository does not support")
    return "no automatic default; Nouveau is a baseline/fallback candidate; C is opt-in for Turing and later"


@check
def insecure_debian_550_is_never_recommended():
    ev = evidence()
    sec = ev["debian"]["security"]
    expect(sec["open_issues_trixie"] == sum(sec["open_issues_by_year"].values()), "open issue count mismatch")
    if sec["open_issues_trixie"] > 0:
        for sid in ("A", "B"):
            s = ev["strategies"][sid]
            expect(not s["recommended"] and not s["automatic_default"], f"{sid} recommended despite open issues")
            expect(s["scores"]["open_security_issues"] == 0, f"{sid} security score")
    return f"{sec['open_issues_trixie']} open trixie issues; A and B not recommended"


@check
def secure_boot_gates_proprietary_paths():
    ev = evidence()
    expect(ev["secure_boot"]["depends_on"] == "P-006" and ev["secure_boot"]["dkms_modules_require_mok"], "P-006")
    for sid in ("A", "B", "C"):
        expect(not ev["strategies"][sid]["automatic_default"], f"{sid} automatic while P-006 is undecided")
    expect("P-006 decided" in ev["strategies"]["C"]["conditions"], "C conditions omit P-006")
    expect(any("D-005" in c for c in ev["strategies"]["C"]["conditions"]), "C conditions omit the D-005 exception")
    return "no DKMS path is automatic; C is conditional on D-005 and P-006"


@check
def nvidia_repo_facts_are_consistent():
    repo = evidence()["nvidia_repo"]
    expect(re.fullmatch(r"[0-9A-F]{40}", repo["signing_key_fingerprint"]), "fingerprint format")
    for k in ("cuda_keyring_deb_sha256", "keyring_sha256", "inrelease_sha256", "packages_gz_sha256"):
        expect(re.fullmatch(r"[0-9a-f]{64}", repo[k]), f"{k} format")
    expect(repo["inrelease_signature"] == "good", "InRelease signature")
    expect({"Valid-Until"} <= set(repo["release_fields_missing"]), "missing Valid-Until must be recorded")
    branches = sorted({v.split(".")[0] for v in repo["driver_versions"]})
    expect(branches == repo["driver_branches"], f"branches {branches}")
    expect(sorted(p.rsplit("-", 1)[1] for p in repo["pinning_packages"]) == repo["driver_branches"], "pinning")
    expect(not repo["legacy_580_branch_present"] and "580" not in repo["driver_branches"], "580 claim")
    return f"key {repo['signing_key_fingerprint'][-8:]}, branches {','.join(repo['driver_branches'])}"


@check
def snapshot_is_canonical_and_matches_matrix():
    snap = normalize.check(str(SNAPSHOT))
    expect(snap["production_use"] is False, "snapshot production_use")
    prov = snap["provenance"]
    ev = evidence()
    expect(prov["repository_signing_key_fingerprint"] == ev["nvidia_repo"]["signing_key_fingerprint"], "key mismatch")
    expect(prov["repository_inrelease_sha256"] == ev["nvidia_repo"]["inrelease_sha256"], "InRelease mismatch")
    expect(prov["upstream_chip_entries"] == snap["counts"]["chips"], "entry count mismatch")
    expect(re.fullmatch(r"[0-9a-f]{64}", prov["upstream_sha256"]), "upstream hash")
    matrix_classes = [row["support_class"] for row in ev["generation_matrix"]]
    expect(sorted(matrix_classes) == sorted(SUPPORT_CLASSES), f"matrix classes {matrix_classes}")
    for row in ev["generation_matrix"]:
        expect(snap["counts"]["by_support_class"][row["support_class"]] > 0, f"empty class {row['support_class']}")
        if row["support_class"] in ("legacy-older", "legacy-470", "legacy-580"):
            expect(not row["nvidia_repo_c"] and row["proposed_path"] == "D-limited", f"{row['generation']} path")
    # spot checks against well-known PCI IDs
    by_dev = {c["pci_device"]: c for c in snap["chips"] if c["subsystem_vendor"] is None}
    for dev, cls in (("1b80", "legacy-580"), ("1180", "legacy-470"), ("1e87", "open-or-proprietary"),
                     ("2684", "open-or-proprietary"), ("2b85", "open-only")):
        expect(by_dev[dev]["support_class"] == cls, f"{dev} classified {by_dev[dev]['support_class']}")
    return f"{snap['counts']['chips']} chips, classes {snap['counts']['by_support_class']}"


@check
def nouveau_is_a_candidate_not_a_qualified_default():
    ev = evidence()
    vocab = ev["nouveau_status_vocabulary"]
    expect(set(vocab) == {"fallback-candidate", "limited", "not-yet-qualified"}, f"vocabulary {sorted(vocab)}")
    for row in ev["generation_matrix"]:
        expect(row["production_qualified"] is False, f"{row['generation']} claims production qualification")
        expect(row["nouveau_status"] == NOUVEAU_STATUS[row["support_class"]],
               f"{row['generation']}: Nouveau status {row['nouveau_status']}")
        expect("nouveau" not in row, f"{row['generation']}: unqualified 'nouveau' field")
    base = ev["nouveau_baseline"]
    expect(base["qualified_default"] is False and base["mesa"].startswith("25.0.7"), "Nouveau baseline")
    sections = {
        "Strategy D": adr_section("### Strategy D", "## GPU generation matrix"),
        "generation matrix": adr_section("## GPU generation matrix", "## Firmware relationship"),
        "tier model": adr_section("## Proposed tier model", "## D-005"),
    }
    required = {
        "Strategy D": ("baseline/fallback candidate", "not a qualified default", "Mesa 25.0.7", "Blackwell",
                       "production-qualified"),
        "generation matrix": ("fallback candidate", "limited", "not yet qualified", "not production-qualified"),
        "tier model": ("baseline/fallback candidate", "not a qualified default", "not yet qualified",
                       "production-qualified"),
    }
    for name, phrases in required.items():
        for phrase in phrases:
            expect(phrase in sections[name], f"ADR-0002 {name} section lacks {phrase!r}")
    rows = {line.split("|")[1].strip(): line for line in ADR.read_text(encoding="utf-8").splitlines()
            if line.startswith("| ") and "(" in line.split("|")[2]}
    row_status = {"Fermi and older": "limited", "Kepler": "limited", "Maxwell, Pascal, Volta": "limited",
                  "Turing, Ampere, Ada, Hopper": "fallback candidate", "Blackwell and newer": "not yet qualified"}
    for gen, status in row_status.items():
        expect(gen in rows, f"ADR-0002 matrix lacks row {gen!r}")
        cells = [c.strip().strip("*") for c in rows[gen].split("|")]
        expect(cells[3].startswith(status), f"ADR-0002 matrix row {gen!r}: Nouveau {cells[3]!r}")
        expect(cells[-2] == "no", f"ADR-0002 matrix row {gen!r} claims production qualification")
    hits = []
    for path in [ADR] + PROSE:
        text = path.read_text(encoding="utf-8")
        for pat in NOUVEAU_OVERCLAIMS:
            for m in re.finditer(pat, text):
                hits.append(f"{path.relative_to(REPO)}: {m.group(0)!r}")
    expect(not hits, f"Nouveau overclaims: {hits}")
    return "Nouveau: limited (legacy), fallback candidate (Turing-Hopper), not yet qualified (Blackwell); nothing qualified"


@check
def signing_key_wording_is_precise():
    ev = evidence()
    kd = ev["nvidia_repo"]["key_distribution"]
    expect(ev["nvidia_repo"]["signing_key_fingerprint"] == FINGERPRINT, "signing fingerprint")
    expect(kd["documented_fallback_http_status"] == 404 and kd["documented_fallback_url"].endswith(
        "/cuda-archive-keyring.gpg"), "404 fallback not recorded")
    expect(kd["index_pubkey_file"] == "8793F200.pub" and kd["index_pubkey_fingerprint"] == FINGERPRINT,
           "index-exposed 8793F200.pub not recorded")
    expect(re.fullmatch(r"[0-9a-f]{64}", kd["index_pubkey_sha256"]), "index pubkey hash")
    expect(kd["independent_trust_anchor"] is False, "a distribution channel is treated as a trust anchor")
    expect(FINGERPRINT in kd["trust_requirement"] and "explicitly qualified" in kd["trust_requirement"],
           "trust requirement must demand the full fingerprint via a qualified mechanism")
    strat_c = adr_section("### Strategy C", "### Isolated APT simulation results")
    for phrase in ("cuda-archive-keyring.gpg", "HTTP 404", "8793F200.pub", "explicitly qualified mechanism",
                   "Neither fact changes the trust decision"):
        expect(phrase in strat_c, f"ADR-0002 Strategy C section lacks {phrase!r}")
    expect(strat_c.count(FINGERPRINT) >= 2, "Strategy C must state the full fingerprint for bootstrap verification")
    d005 = adr_section("## D-005 external-repository exception", "## Decision")
    for phrase in (FINGERPRINT, "explicitly qualified bootstrap mechanism", "8793F200.pub"):
        expect(phrase in d005, f"ADR-0002 D-005 section lacks {phrase!r}")
    hits = []
    for path in [ADR, EVIDENCE] + PROSE:
        text = path.read_text(encoding="utf-8")
        for pat in KEY_OVERCLAIMS:
            for m in re.finditer(pat, text, re.I):
                hits.append(f"{path.relative_to(REPO)}: {m.group(0)!r}")
    expect(not hits, f"signing-key overclaims: {hits}")
    return "404 fallback and 8793F200.pub recorded; full-fingerprint bootstrap required; no 'only cuda-keyring' claim"


@check
def snapshot_drift_is_detected():
    import copy
    import tempfile
    snap = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    mutations = []
    m = copy.deepcopy(snap); m["chips"][0]["support_class"] = "open-only"; mutations.append(m)  # noqa: E702
    m = copy.deepcopy(snap); m["chips"][1], m["chips"][2] = m["chips"][2], m["chips"][1]; mutations.append(m)  # noqa: E702
    m = copy.deepcopy(snap); m["chips"][3]["name"] += "x"; mutations.append(m)  # noqa: E702
    m = copy.deepcopy(snap); m["production_use"] = True; mutations.append(m)  # noqa: E702
    with tempfile.TemporaryDirectory() as tmp:
        for i, doc in enumerate(mutations):
            path = pathlib.Path(tmp) / f"m{i}.json"
            path.write_text(normalize.serialize(doc), encoding="utf-8")
            try:
                normalize.check(str(path))
            except normalize.SnapshotError:
                continue
            raise Failure(f"mutation {i} not detected")
    return f"{len(mutations)} mutations rejected"


@check
def simulations_record_the_key_risks():
    sims = evidence()["simulations"]
    expect(sims["environment"]["signature_failures"] == 0 and sims["environment"]["payloads_retained"] == 0, "env")
    res = {r["id"]: r for r in sims["results"]}
    for rid in ("A-proprietary", "A-open", "B-proprietary", "C-desktop-open", "C610-unpin-upgrade",
                "C-downgrade-595", "C-purge-autoremove", "A-purge-autoremove", "CX-takeover", "A32-i386"):
        expect(rid in res, f"missing simulation {rid}")
    expect(res["C-desktop-open"]["contrib_needed"] is False, "C contrib claim")
    expect(res["A-proprietary"]["components"].get("non-free", 0) > 0, "A non-free claim")
    expect("dkms" in sims["name_overlap"]["non_nvidia_named_overlaps"], "dkms takeover not recorded")
    expect("blacklist nouveau" in sims["nvidia_615_modprobe"], "nouveau blacklist not recorded")
    expect(sims["headers_pulled_by_any_install"] is False, "header finding")
    return f"{len(res)} simulations; dkms takeover, flavor switch and nouveau blacklist recorded"


@check
def adr_is_proposed_and_register_unchanged():
    text = ADR.read_text(encoding="utf-8")
    expect(re.search(r"^Status: Proposed$", text, re.M), "ADR-0002 status is not Proposed")
    expect(not re.search(r"^Status: Accepted", text, re.M), "ADR-0002 accepted")
    expect(evidence()["adr"] == {"path": "docs/decisions/0002-nvidia-driver-strategy.md", "status": "Proposed"}, "adr")
    reg = REGISTER.read_text(encoding="utf-8")
    accepted = reg.split("## Pending decision subjects")[0]
    expect("D-014" not in accepted, "D-014 recorded as accepted")
    expect("0002-nvidia" not in accepted, "ADR-0002 listed under accepted decisions")
    return "ADR-0002 Proposed; no accepted NVIDIA decision"


@check
def no_production_nvidia_effect():
    rules = resolver.load_production_rules()
    packages = set()
    classes = set()
    for rule in rules:
        eff = rule.get("effects", {})
        packages |= {p["name"] for p in eff.get("packages", [])}
        classes |= set(eff.get("classifications", []))
    expect(packages == PRODUCTION_PACKAGE_SET, f"production package set {sorted(packages)}")
    expect(not any(NVIDIA_DRIVER_PACKAGES.match(p) for p in packages), "NVIDIA package mapped")
    expect(not any(c.startswith("gpu.hybrid") for c in classes), "gpu.hybrid classification")
    nv = json.loads((HARDWARE / "rules" / "class.gpu.nvidia.json").read_text(encoding="utf-8"))
    expect(nv["effects"] == {"classifications": ["gpu.nvidia"]}, "class.gpu.nvidia gained effects")
    expect(tuple(resolver.AUTHORISED_SOURCES) == ("main", "non-free-firmware"), "source policy changed")
    return f"{len(packages)} production packages, none NVIDIA; source policy main + non-free-firmware"


@check
def no_nvidia_source_or_key_tracked():
    hits = []
    allowed = {EVIDENCE, SNAPSHOT, ADR, NVIDIA / "README.md", pathlib.Path(__file__).resolve()}
    for path in tracked_files():
        if path in allowed or not path.is_file() or path.suffix in (".md",):
            continue
        if path.suffix in (".gpg", ".asc", ".pgp", ".key"):
            hits.append(f"{path.relative_to(REPO)} (key file)")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for s in NVIDIA_STRINGS:
            if s in text:
                hits.append(f"{path.relative_to(REPO)} ({s})")
    expect(not hits, f"NVIDIA source/key references outside evidence: {hits}")
    live = [p for p in tracked_files() if "build/live" in str(p) and p.is_file()]
    for p in live:
        t = p.read_text(encoding="utf-8", errors="replace")
        expect(not re.search(r"\b(contrib|non-free)\b(?!-firmware)", t) or p.suffix == ".md",
               f"{p.relative_to(REPO)} enables contrib/non-free")
        expect(not re.search(r"^\s*nvidia", t, re.M) or p.suffix == ".md", f"{p.relative_to(REPO)} lists NVIDIA")
    return f"{len(tracked_files())} tracked files scanned; live-build config {len(live)} files clean"


@check
def runtime_never_reads_evidence():
    for path in (HARDWARE / "resolver.py", HARDWARE / "resolve.py", HARDWARE / "probe.py"):
        text = path.read_text(encoding="utf-8")
        for word in ("nvidia/", "supported-gpus", "evidence-v1", "normalize-nvidia"):
            expect(word not in text, f"{path.name} references {word}")
    return "resolver, resolve and probe never read the NVIDIA evidence"


def main():
    sys.stdout.reconfigure(line_buffering=True)
    print(f"python {sys.version.split()[0]}")
    failures = 0
    for fn in CHECKS:
        try:
            detail = fn()
        except Exception as e:  # noqa: BLE001 - every failure is reported
            failures += 1
            print(f"FAIL: {fn.__name__}: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            continue
        print(f"ok: {fn.__name__}" + (f": {detail}" if detail else ""))
    if failures:
        print(f"FAIL: hardware NVIDIA strategy ({failures} of {len(CHECKS)} checks failed)", file=sys.stderr)
        return 1
    print(f"PASS: hardware NVIDIA strategy ({len(CHECKS)} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
