#!/usr/bin/python3
"""LUBARCHY source activation and package staging builder qualification (M1-J; builder only).

    tools/hwe/qualify-staging.py WORKDIR

Runs as an unprivileged user (refuses root) and writes only inside WORKDIR
(absent or empty). Against the real system it:

 1. compiles the source-activation contract from /etc/apt (read-only) and
    renders the LUBARCHY-owned source file into WORKDIR/rendered;
 2. runs an isolated, authenticated `apt-get update` for the contract's
    post-state (only the Signed-By keyring authenticates; system APT
    configuration, sources and lists are never used or written);
 3. fresh probe -> production resolve -> package transaction against that
    isolated state, then exact acquisition, verification and atomic
    finalization of the staged bundle;
 4. repeats 2-3 independently in a second workspace and compares the
    canonical transaction, metadata digest and manifest;
 5. proves the future offline apply model on copies of the payloads:
    --download-only --no-download with the network black-holed (succeeds,
    fetches nothing), a missing dependency payload (fails), resolver mark
    decisions (simulation debug), and a non-root sandbox `apt-get install`
    with dpkg replaced by /bin/true and private copies of the dpkg status
    and extended_states (shows the marks APT would write; nothing is
    unpacked or installed); it also records that APT accepts a same-size
    corrupted cached payload, which is why LUBARCHY's bundle verification
    is the authoritative integrity control;
 6. tamper checks on a bundle copy, final revalidation, the production CLIs.

Evidence goes to WORKDIR/evidence. Every .deb is deleted before exit.
Not for CI (needs network and the builder).
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
HARDWARE = os.path.join(REPO, "hardware")
sys.path.insert(0, HARDWARE)
sys.dont_write_bytecode = True

import package_staging as ps  # noqa: E402
import package_transaction as pt  # noqa: E402
import probe  # noqa: E402
import resolver  # noqa: E402
import source_activation as sa  # noqa: E402

BLACKHOLE = ["-o", "Acquire::http::Proxy=http://127.0.0.1:9", "-o", "Acquire::https::Proxy=http://127.0.0.1:9",
             "-o", "Acquire::Retries=0"]


def run(argv, env, cwd=None):
    r = subprocess.run(argv, env=env, cwd=cwd, shell=False, stdin=subprocess.DEVNULL, capture_output=True,
                       timeout=900, check=False)
    return r.returncode, r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace")


def dump(ev, name, value):
    with open(os.path.join(ev, name), "w", encoding="utf-8") as f:
        if isinstance(value, (bytes, str)):
            f.write(value if isinstance(value, str) else value.decode())
        else:
            json.dump(value, f, indent=2, sort_keys=True)
            f.write("\n")


def sanitize(text, work):
    return text.replace(work, "<workdir>").replace(os.path.expanduser("~"), "<home>")


def chain(work, label, contract):
    """Isolated update + fresh probe/resolve/transaction + staged bundle in WORKDIR/<label>."""
    ws = os.path.join(work, label)
    os.mkdir(ws, 0o700)
    os.mkdir(os.path.join(ws, "apt"), 0o700)
    conf, metadata = sa.isolated_update(contract, os.path.join(ws, "apt"), "/")
    rules = resolver.load_production_rules()
    inventory = probe.Probe().inventory()
    plan = resolver.check_plan(resolver.resolve(inventory, rules))
    tx = pt.compile_transaction(inventory, rules, pt.SystemBackend(apt_config=conf))
    root = os.path.join(ws, "bundles")
    os.mkdir(root, 0o700)
    bundle, manifest, results = ps.stage_bundle(tx, sa.activation_digest(contract),
                                                metadata["repository_metadata_digest"], root,
                                                ps.AptAcquirer(conf), ps.DebInspector())
    return {"ws": ws, "conf": conf, "metadata": metadata, "inventory": inventory, "plan": plan, "tx": tx,
            "root": root, "bundle": bundle, "manifest": manifest, "results": results}


def offline_model(work, c, ev):
    """Future offline apply model, proven on payload copies (never on the system)."""
    env = dict(pt.COMMAND_ENV, APT_CONFIG=c["conf"])
    targets = c["manifest"]["direct_targets"]
    payloads = c["manifest"]["payloads"]
    deps = sorted(p["package"] for p in payloads if p["relation"] == "dependency")
    direct = sorted(p["package"] for p in payloads if p["relation"] == "direct")
    out = {"direct_targets": targets, "dependencies": deps}

    def cache(name, drop=None, corrupt=None):
        d = os.path.join(work, "offline", name)
        os.makedirs(os.path.join(d, "archives", "partial"))
        for p in payloads:
            if p["package"] == drop:
                continue
            dst = os.path.join(d, "archives", p["filename"])
            shutil.copyfile(os.path.join(c["bundle"], "archives", p["filename"]), dst)
            if p["package"] == corrupt:
                b = bytearray(open(dst, "rb").read())
                b[-16] ^= 0xFF
                with open(dst, "wb") as f:
                    f.write(b)
        return d

    base = ["/usr/bin/apt-get", "--yes", "--quiet", "--no-install-recommends", "--no-remove"]
    d = cache("complete")
    rc, text = run(base + ["--no-download", "--download-only", "-o", f"Dir::Cache::archives={d}/archives/",
                           *BLACKHOLE, "install", *targets], env)
    insts = sorted(re.findall(r"^Inst (\S+)", run(base + ["--simulate", "--no-download", "-o",
                                                       f"Dir::Cache::archives={d}/archives/", "install", *targets],
                                                env)[1], re.M))
    out["complete_cache"] = {"exit": rc, "download_complete": "Download complete and in download only mode" in text,
                             "simulated_installs": insts,
                             "closure_matches": insts == sorted(p["package"] for p in payloads)}
    rc, text = run(base + ["--print-uris", "-o", f"Dir::Cache::archives={d}/archives/", *BLACKHOLE, "install",
                           *targets], env)
    uris = [line for line in text.splitlines() if line.startswith("'")]
    out["print_uris"] = {"exit": rc, "uris_to_fetch": len(uris), "need_to_get_0": "Need to get 0 B" in text}
    if deps:
        d2 = cache("missing-dependency", drop=deps[0])
        rc, text = run(base + ["--no-download", "--download-only", "-o", f"Dir::Cache::archives={d2}/archives/",
                               *BLACKHOLE, "install", *targets], env)
        out["missing_dependency"] = {"dropped": deps[0], "exit": rc,
                                     "error": [line for line in text.splitlines() if line.startswith("E:")]}
    rc, text = run(base + ["--simulate", "-o", "Debug::pkgDepCache::Marker=1", "-o", "Debug::pkgDepCache::AutoInstall=1",
                           "install", *targets], env)
    marks = {}
    for m in re.finditer(r"MarkInstall (\S+?):\S+ < none -> (\S+) .*?FU=(\d)", text):
        marks[m.group(1)] = {"version": m.group(2), "from_user": m.group(3) == "1"}
    out["resolver_marks"] = marks
    out["resolver_marks_ok"] = (all(marks.get(n, {}).get("from_user") is True for n in direct)
                                and all(marks.get(n, {}).get("from_user") is False for n in deps))

    def sandbox(name, corrupt=None):
        d3 = cache("sandbox-" + name, corrupt=corrupt)
        state = os.path.join(d3, "state")
        os.makedirs(state)
        os.makedirs(os.path.join(d3, "log"))
        shutil.copyfile("/var/lib/dpkg/status", os.path.join(d3, "status"))
        shutil.copyfile("/var/lib/apt/extended_states", os.path.join(state, "extended_states"))
        before = open(os.path.join(state, "extended_states"), encoding="utf-8").read()
        sandbox_opts = ["-o", "Dir::Bin::dpkg=/bin/true", "-o", f"Dir::State={state}",
                        "-o", f"Dir::State::extended_states={state}/extended_states",
                        "-o", f"Dir::State::status={d3}/status", "-o", f"Dir::Log={d3}/log"]
        for o in sandbox_opts[1::2]:
            if "/var/lib" in o or "/var/log" in o:
                raise SystemExit(f"sandbox option points at system state: {o}")
        rc, text = run(base + ["--no-download", *sandbox_opts,
                               "-o", f"Dir::Cache::archives={d3}/archives/", "-o", "Debug::NoLocking=true",
                               *BLACKHOLE, "install", *targets], env)
        after = open(os.path.join(state, "extended_states"), encoding="utf-8").read()
        added = after[len(before):] if after.startswith(before) else None
        auto = sorted(re.findall(r"Package: (\S+)\nArchitecture: \S+\nAuto-Installed: 1", added or ""))
        status_unchanged = open(os.path.join(d3, "status"), "rb").read() == open("/var/lib/dpkg/status", "rb").read()
        return {"exit": rc, "extended_states_appended": added, "auto_installed_written": auto,
                "status_copy_unchanged": status_unchanged}
    out["sandbox_marks"] = sandbox("good")
    out["sandbox_marks_ok"] = (out["sandbox_marks"]["exit"] == 0 and out["sandbox_marks"]["auto_installed_written"] == deps
                               and out["sandbox_marks"]["status_copy_unchanged"])
    corrupt = direct[0] if direct else payloads[0]["package"]
    out["apt_accepts_same_size_corruption"] = sandbox("corrupt", corrupt=corrupt)["exit"] == 0
    d4 = cache("lubarchy-verify-corrupt", corrupt=corrupt)
    os.rmdir(os.path.join(d4, "archives", "partial"))          # payload-only directory, as in a bundle
    try:
        ps.verify_payloads(os.path.join(d4, "archives"), payloads, ps.DebInspector())
        out["lubarchy_rejects_corruption"] = False
    except ps.StagingError as e:
        out["lubarchy_rejects_corruption"] = e.code == "payload_mismatch" and "SHA-256" in e.message
    out["offline_apply_argv"] = ps.offline_apply_argv(c["manifest"], c["bundle"])
    out["offline_apply_executed"] = False
    shutil.rmtree(os.path.join(work, "offline"))
    dump(ev, "offline-apply-model.json", json.loads(sanitize(json.dumps(out), work)))
    return out


def tamper_checks(work, c):
    copy = os.path.join(work, "tamper", os.path.basename(c["bundle"]))
    os.makedirs(os.path.dirname(copy), mode=0o700)
    shutil.copytree(c["bundle"], copy)
    os.chmod(os.path.dirname(copy), 0o700)
    results = {}
    first = os.path.join(copy, "archives", c["manifest"]["payloads"][0]["filename"])
    original = open(first, "rb").read()
    for label, mutate in (("payload_byte_flip", lambda: open(first, "r+b").write(bytes([original[0] ^ 1]))),
                          ("payload_truncation", lambda: os.truncate(first, 100)),
                          ("payload_addition", lambda: open(os.path.join(copy, "archives", "x_1_all.deb"), "wb").close()),
                          ("manifest_edit", lambda: open(os.path.join(copy, "manifest.json"), "ab").write(b" "))):
        man = open(os.path.join(copy, "manifest.json"), "rb").read()
        mutate()
        try:
            ps.verify_bundle(copy, ps.DebInspector())
            results[label] = "ACCEPTED"
        except ps.StagingError as e:
            results[label] = f"rejected ({e.code})"
        with open(first, "wb") as f:
            f.write(original)
        os.chmod(first, 0o644)
        extra = os.path.join(copy, "archives", "x_1_all.deb")
        if os.path.exists(extra):
            os.unlink(extra)
        with open(os.path.join(copy, "manifest.json"), "wb") as f:
            f.write(man)
        ps.verify_bundle(copy, ps.DebInspector())
    shutil.rmtree(os.path.join(work, "tamper"))
    return results


def main(argv):
    if len(argv) != 2:
        sys.stderr.write(__doc__)
        return 64
    if os.geteuid() == 0:
        sys.stderr.write("refusing to run as root (qualification must not be able to write system state)\n")
        return 64
    work = os.path.realpath(argv[1])
    if os.path.exists(work) and os.listdir(work):
        sys.stderr.write("WORKDIR must be absent or empty\n")
        return 64
    os.makedirs(work, mode=0o700, exist_ok=True)
    ev = os.path.join(work, "evidence")
    os.mkdir(ev)
    checks = {}
    sys_before = {p: hashlib.sha256(open(p, "rb").read()).hexdigest()
                  for p in ("/etc/apt/sources.list", "/var/lib/dpkg/status", "/var/lib/apt/extended_states")}

    # 1. Source activation (read-only for the system)
    state = sa.inspect("/")
    contract = sa.compile_system("/")
    contract2 = sa.compile_system("/")
    sa.verify_activation(contract, "/")
    rendered = sa.render_to(contract, os.path.join(work, "rendered"))
    dump(ev, "source-pre-state.json", {"pre_state": state, "pre_state_digest": sa.pre_state_digest(state)})
    dump(ev, "source-activation-contract.json", contract)
    if rendered:
        shutil.copyfile(rendered, os.path.join(ev, "lubarchy-debian.sources.rendered"))
    checks["activation_deterministic"] = resolver.canonical_json(contract) == resolver.canonical_json(contract2)
    added = sorted({(t["suite"], t["component"]) for s in (contract["owned_source"] or {}).get("stanzas", [])
                    for t in [{"suite": x, "component": c} for x in s["suites"] for c in s["components"]]})
    checks["activation_adds_only_non_free_firmware"] = all(c == "non-free-firmware" for _s, c in added) and bool(added)
    checks["owned_path_absent_on_system"] = not os.path.lexists(sa.OWNED_PATH)
    print(f"source activation: model={contract['model']} action={contract['action']} adds={added}")

    # 2-3. Two independent chains
    c1 = chain(work, "chain-1", contract)
    c2 = chain(work, "chain-2", contract)
    for i, c in ((1, c1), (2, c2)):
        dump(ev, f"metadata-{i}.json", json.loads(sanitize(json.dumps(c["metadata"]), work)))
        dump(ev, f"transaction-{i}.json", json.loads(resolver.canonical_json(c["tx"])))
        dump(ev, f"staged-manifest-{i}.json", c["manifest"])
        with open(os.path.join(ev, f"staged-manifest-{i}.canonical.json"), "wb") as f:
            f.write(ps.manifest_bytes(c["manifest"]))
    dump(ev, "inventory.json", json.loads(resolver.canonical_json(c1["inventory"])))
    dump(ev, "plan.json", json.loads(resolver.canonical_json(c1["plan"])))
    checks["metadata_digest_equal"] = (c1["metadata"]["repository_metadata_digest"]
                                       == c2["metadata"]["repository_metadata_digest"])
    checks["transaction_equal"] = resolver.canonical_json(c1["tx"]) == resolver.canonical_json(c2["tx"])
    checks["manifest_equal"] = ps.manifest_bytes(c1["manifest"]) == ps.manifest_bytes(c2["manifest"])
    checks["payload_bytes_equal"] = all(
        open(os.path.join(c1["bundle"], "archives", p["filename"]), "rb").read()
        == open(os.path.join(c2["bundle"], "archives", p["filename"]), "rb").read() for p in c1["manifest"]["payloads"])
    m, results = ps.verify_bundle(c1["bundle"], ps.DebInspector(), tx=c1["tx"], expected={
        "transaction_digest": pt.transaction_digest(c1["tx"]),
        "source_activation_digest": sa.activation_digest(contract),
        "repository_metadata_digest": c1["metadata"]["repository_metadata_digest"]})
    checks["bundle_reverified"] = m == c1["manifest"]
    table = []
    for p, r in zip(sorted(m["payloads"], key=lambda x: x["filename"]), results):
        table.append({"package": p["package"], "architecture": p["architecture"], "version": p["version"],
                      "relation": p["relation"], "automatic_after": p["automatic_after"],
                      "expected_sha256": r["expected_sha256"], "observed_sha256": r["observed_sha256"],
                      "expected_size": r["expected_size"], "observed_size": r["observed_size"],
                      "deb_fields": r["deb_fields"],
                      "repositories": p["repositories"],
                      "repository_metadata_digest": m["repository_metadata_digest"]})
    dump(ev, "payload-verification.json", table)
    checks["payloads_verified"] = bool(table) and all(
        t["expected_sha256"] == t["observed_sha256"] and t["expected_size"] == t["observed_size"] for t in table)
    print(json.dumps({"transaction_digest": m["transaction_digest"], "bundle_digest": m["bundle_digest"],
                      "repository_metadata_digest": m["repository_metadata_digest"],
                      "payloads": [(t["package"], t["architecture"], t["version"], t["relation"]) for t in table]},
                     indent=2))

    # 5. Offline apply model
    om = offline_model(work, c1, ev)
    checks["offline_complete_cache"] = (om["complete_cache"]["exit"] == 0 and om["complete_cache"]["download_complete"]
                                        and om["complete_cache"]["closure_matches"])
    checks["offline_nothing_to_fetch"] = om["print_uris"]["uris_to_fetch"] == 0 and om["print_uris"]["need_to_get_0"]
    checks["offline_missing_dependency_fails"] = om.get("missing_dependency", {}).get("exit") == 100
    checks["resolver_marks"] = om["resolver_marks_ok"]
    checks["sandbox_marks"] = om["sandbox_marks_ok"]
    checks["lubarchy_rejects_corruption"] = om["lubarchy_rejects_corruption"]
    print("offline model:", json.dumps({k: om[k] for k in ("complete_cache", "print_uris", "resolver_marks",
                                                             "apt_accepts_same_size_corruption")}, indent=1))

    # 6. Tamper, final revalidation, CLIs
    tamper = tamper_checks(work, c1)
    dump(ev, "tamper-checks.json", tamper)
    checks["tamper_detected"] = all(v.startswith("rejected") for v in tamper.values())
    fresh_contract = sa.compile_system("/")
    fresh_tx = pt.compile_transaction(probe.Probe().inventory(), resolver.load_production_rules(),
                                      pt.SystemBackend(apt_config=c2["conf"]))
    fresh = {"transaction_digest": pt.transaction_digest(fresh_tx),
             "source_activation_digest": sa.activation_digest(fresh_contract),
             "repository_metadata_digest": c2["metadata"]["repository_metadata_digest"]}
    try:
        decision = ps.final_revalidation(c1["manifest"], fresh)
    except ps.StagingError as e:
        decision = f"restage ({e.message})"
    dump(ev, "final-revalidation.json", {"fresh": fresh, "decision": decision})
    checks["final_revalidation_accepts_unchanged"] = decision == "accept"
    cli = {}
    for name, args in (("inspect", ["inspect"]), ("compile", ["compile"])):
        rc, text = run([os.path.join(HARDWARE, "compile-source-activation.py"), *args], {"PATH": "/usr/bin:/bin"})
        cli[f"compile-source-activation {name}"] = {"exit": rc}
    rc, text = run([os.path.join(HARDWARE, "stage-packages.py"), "stage", "--workspace", os.path.join(work, "cli")],
                   {"PATH": "/usr/bin:/bin"})
    cli_out = json.loads(text) if rc == 0 else {}
    cli["stage-packages stage"] = {"exit": rc, "transaction_digest": cli_out.get("transaction_digest"),
                                   "bundle_digest_matches_library": cli_out.get("bundle_digest") == c1["manifest"]["bundle_digest"]}
    rc, text = run([os.path.join(HARDWARE, "stage-packages.py"), "verify", "--workspace", os.path.join(work, "cli"),
                    "--transaction-digest", cli_out.get("transaction_digest", "")], {"PATH": "/usr/bin:/bin"})
    cli["stage-packages verify"] = {"exit": rc}
    rc, text = run([os.path.join(HARDWARE, "compile-transaction.py"), "compile"], {"PATH": "/usr/bin:/bin"})
    cli["compile-transaction compile (system APT, main only)"] = {"exit": rc, "stderr": text.strip()[-160:]}
    dump(ev, "production-cli.json", cli)
    checks["cli_stage_and_verify"] = (cli["stage-packages stage"]["exit"] == 0 and cli["stage-packages verify"]["exit"] == 0
                                      and cli["stage-packages stage"]["bundle_digest_matches_library"])
    checks["system_transaction_still_fails_closed"] = cli["compile-transaction compile (system APT, main only)"]["exit"] == 6

    # Cleanup: payloads are not retained.
    for c in (c1, c2):
        ps.remove_bundle(c["root"], c["manifest"]["transaction_digest"])
    if cli_out.get("transaction_digest"):
        ps.remove_bundle(os.path.join(work, "cli", "bundles"), cli_out["transaction_digest"])
    for c in (c1, c2):
        shutil.rmtree(os.path.join(c["ws"], "apt", "cache"))
    debs = [os.path.join(d, f) for d, _s, fs in os.walk(work) for f in fs if f.endswith(".deb")]
    checks["no_deb_retained"] = not debs
    sys_after = {p: hashlib.sha256(open(p, "rb").read()).hexdigest() for p in sys_before}
    checks["system_files_unchanged"] = sys_before == sys_after
    dump(ev, "checks.json", checks)
    for k, v in checks.items():
        print(f"{'ok  ' if v else 'FAIL'} {k}")
    ok = all(checks.values())
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
