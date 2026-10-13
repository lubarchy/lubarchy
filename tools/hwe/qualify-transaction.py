#!/usr/bin/python3
"""LUBARCHY package transaction builder qualification (M1-I; builder only).

    tools/hwe/qualify-transaction.py WORKDIR

Builds an isolated, authenticated APT environment in WORKDIR (Debian trixie,
trixie-updates, trixie-security; components main and non-free-firmware;
Signed-By the Debian archive keyring; the system dpkg database read-only),
then runs fresh probe -> production resolve -> transaction compile twice and
verify once, and writes evidence files into WORKDIR/evidence:

  inventory.json, plan.json, transaction-1.json, transaction-2.json,
  digests.json, simulate-install.txt, simulate-direct-targets.txt,
  direct-target-closure.json, architecture-syntax.json,
  simulate-with-recommends.txt, indextargets.txt, policy.txt, hashes.txt,
  production-cli.txt

It never changes the system APT configuration or package state: apt-get
update and apt-get download use only WORKDIR (lists, cache, downloads), the
downloaded .deb files are hashed against the transaction and deleted, and
apt-get is otherwise used with --simulate only. Not for CI (needs network).
"""

import hashlib
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
HARDWARE = os.path.join(REPO, "hardware")
sys.path.insert(0, HARDWARE)
sys.dont_write_bytecode = True

import package_transaction as pt  # noqa: E402
import probe  # noqa: E402
import resolver  # noqa: E402

KEYRING = "/usr/share/keyrings/debian-archive-keyring.pgp"


def isolated_config(work):
    for d in ("lists/partial", "cache/archives/partial", "etc/sources.list.d", "etc/preferences.d",
              "etc/apt.conf.d", "debs", "evidence"):
        os.makedirs(os.path.join(work, d), exist_ok=True)
    with open(os.path.join(work, "etc/sources.list.d/debian.sources"), "w", encoding="utf-8") as f:
        f.write("Types: deb\nURIs: https://deb.debian.org/debian\nSuites: trixie trixie-updates\n"
                f"Components: main non-free-firmware\nSigned-By: {KEYRING}\n\n"
                "Types: deb\nURIs: https://security.debian.org/debian-security\nSuites: trixie-security\n"
                f"Components: main non-free-firmware\nSigned-By: {KEYRING}\n")
    conf = os.path.join(work, "apt.conf")
    with open(conf, "w", encoding="utf-8") as f:
        f.write(f'Dir::State::Lists "{work}/lists"; Dir::State::status "/var/lib/dpkg/status";\n'
                f'Dir::Cache "{work}/cache"; Dir::Etc::SourceList "/dev/null";\n'
                f'Dir::Etc::SourceParts "{work}/etc/sources.list.d"; Dir::Etc::Preferences "/dev/null";\n'
                f'Dir::Etc::PreferencesParts "{work}/etc/preferences.d"; Dir::Etc::Parts "{work}/etc/apt.conf.d";\n'
                'Acquire::Languages "none"; Debug::NoLocking "true";\n'
                'Acquire::AllowInsecureRepositories "false"; Acquire::AllowDowngradeToInsecureRepositories "false";\n')
    return conf


def run(argv, env, cwd=None):
    r = subprocess.run(argv, env=env, cwd=cwd, shell=False, stdin=subprocess.DEVNULL, capture_output=True,
                       timeout=900, check=False)
    return r.returncode, r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace")


def main(argv):
    if len(argv) != 2:
        sys.stderr.write(__doc__)
        return 64
    work = os.path.realpath(argv[1])
    if os.path.exists(work) and os.listdir(work):
        sys.stderr.write("WORKDIR must be absent or empty\n")
        return 64
    conf = isolated_config(work)
    ev = os.path.join(work, "evidence")
    env = dict(pt.COMMAND_ENV, APT_CONFIG=conf)

    rc, out = run(["/usr/bin/apt-get", "update"], env)
    bad = [line for line in out.splitlines() if line.startswith(("W:", "E:", "Err:"))]
    print(f"isolated apt-get update exit={rc}; warnings/errors={len(bad)}")
    if rc != 0 or bad:
        print("\n".join(bad))
        return 1

    backend = pt.SystemBackend(apt_config=conf)
    rules = resolver.load_production_rules()
    results = []
    for i in (1, 2):
        inventory = probe.Probe().inventory()                      # fresh probe each time
        plan = resolver.check_plan(resolver.resolve(inventory, rules))
        tx = pt.compile_transaction(inventory, rules, backend)
        raw = resolver.canonical_json(tx)
        with open(os.path.join(ev, f"transaction-{i}.json"), "wb") as f:
            f.write(raw + b"\n")
        results.append((inventory, plan, tx, raw))
    (inv1, plan1, tx1, raw1), (inv2, plan2, tx2, raw2) = results
    with open(os.path.join(ev, "inventory.json"), "wb") as f:
        f.write(resolver.canonical_json(inv1) + b"\n")
    with open(os.path.join(ev, "plan.json"), "wb") as f:
        f.write(resolver.canonical_json(plan1) + b"\n")
    same_inv = resolver.canonical_json(inv1) == resolver.canonical_json(inv2)
    same_plan = resolver.canonical_json(plan1) == resolver.canonical_json(plan2)
    same_tx = raw1 == raw2
    fresh = pt.verify_transaction(tx1, probe.Probe().inventory(), rules, backend)
    digests = {"inventory_digest": tx1["inventory_digest"], "plan_digest": tx1["plan_digest"],
               "rule_set_digest": tx1["rule_set_digest"], "transaction_digest": pt.transaction_digest(tx1),
               "repository_state_digest": tx1["repository_state_digest"],
               "installed_state_digest": tx1["installed_state_digest"],
               "package_manager_config_digest": tx1["package_manager_config_digest"],
               "deterministic": {"inventory": same_inv, "plan": same_plan, "transaction": same_tx},
               "verify": pt.transaction_digest(fresh) == pt.transaction_digest(tx1)}
    with open(os.path.join(ev, "digests.json"), "w", encoding="utf-8") as f:
        json.dump(digests, f, indent=2, sort_keys=True)
        f.write("\n")
    print(json.dumps(digests, indent=2, sort_keys=True))

    names = [r["package"] for r in tx1["direct_requirements"]]
    installs = [o for o in tx1["operations"] if o["action"] == "install"]
    with open(os.path.join(ev, "simulate-install.txt"), "w", encoding="utf-8") as f:
        f.write(backend.simulate_text(["--no-install-recommends", "--no-remove", "install", *names]))
    direct = pt.direct_targets(tx1["operations"], tx1["architecture"])
    with open(os.path.join(ev, "simulate-direct-targets.txt"), "w", encoding="utf-8") as f:
        f.write(backend.simulate_text(["--no-install-recommends", "--no-remove", "install", *direct]))
    sim = pt.parse_simulation(backend.simulate_text(["--no-install-recommends", "--no-remove", "install", *direct]))
    got = sorted((a["package"], a["architecture"], a["version"]) for a in sim)
    want = sorted((o["package"], o["architecture"], o["version"]) for o in installs)
    marks = {o["package"]: {"relation": o["relation"], "automatic_before": o["automatic_before"],
                            "automatic_after": o["automatic_after"]} for o in tx1["operations"]}
    auto_before = sorted(pt.parse_auto(backend.auto_text(), tx1["architecture"]))
    closure = {"direct_targets": direct, "simulated": got, "transaction_installs": want,
               "matches": got == want, "unsafe": [a for a in sim if a["op"] != "Inst" or a["old_version"]],
               "marks": marks, "automatic_packages_before": len(auto_before)}
    with open(os.path.join(ev, "direct-target-closure.json"), "w", encoding="utf-8") as f:
        json.dump(closure, f, indent=2, sort_keys=True)
        f.write("\n")
    print(json.dumps(closure, indent=2, sort_keys=True))

    # Empirical APT target syntax (simulation only).
    pol = pt.parse_policy(backend.policy_text(["intel-microcode", "firmware-realtek"]))
    im, fr = pol["intel-microcode"]["candidate"], pol["firmware-realtek"]["candidate"]
    probes = {}
    for spec in (f"intel-microcode:amd64={im}", f"intel-microcode={im}", f"intel-microcode:all={im}",
                 f"intel-microcode:i386={im}", f"intel-microcode:arm64={im}",
                 f"firmware-realtek:all={fr}", f"firmware-realtek:amd64={fr}", f"firmware-realtek={fr}"):
        text = backend.simulate_text(["--no-install-recommends", "--no-remove", "install", spec])
        errors = [l for l in text.splitlines() if l.startswith("E:")]
        insts = [l for l in text.splitlines() if l.startswith("Inst ")]
        probes[spec] = {"resolved": not errors, "inst": insts, "errors": errors}
    with open(os.path.join(ev, "architecture-syntax.json"), "w", encoding="utf-8") as f:
        json.dump(probes, f, indent=2, sort_keys=True)
        f.write("\n")
    for spec, r in probes.items():
        print(f"syntax {spec}: {'resolved' if r['resolved'] else 'REJECTED'} {r['inst'] or r['errors'][:1]}")
    with open(os.path.join(ev, "simulate-with-recommends.txt"), "w", encoding="utf-8") as f:
        f.write(backend.simulate_text(["--no-remove", "install", *names]))
    with open(os.path.join(ev, "indextargets.txt"), "w", encoding="utf-8") as f:
        f.write(backend.indextargets_text())
    with open(os.path.join(ev, "policy.txt"), "w", encoding="utf-8") as f:
        f.write(backend.policy_text(sorted({o["package"] for o in tx1["operations"]})))
    with open(os.path.join(ev, "apply-argv.json"), "w", encoding="utf-8") as f:
        json.dump({"argv": pt.apply_argv(tx1), "environment": pt.apply_environment(), "executed": False}, f,
                  indent=2, sort_keys=True)
        f.write("\n")

    # Corroborate the index SHA-256 against the actual payloads, then delete them.
    lines = []
    for o in installs:
        rc, out = run(["/usr/bin/apt-get", "download", f"{o['package']}={o['version']}"], env,
                      cwd=os.path.join(work, "debs"))
        debs = [d for d in os.listdir(os.path.join(work, "debs")) if d.startswith(o["package"] + "_")]
        if rc != 0 or len(debs) != 1:
            lines.append(f"{o['package']} {o['version']} download-failed")
            continue
        path = os.path.join(work, "debs", debs[0])
        with open(path, "rb") as f:
            got = hashlib.sha256(f.read()).hexdigest()
        size = os.path.getsize(path)
        lines.append(f"{o['package']} {o['version']} sha256 {got} size {size} "
                     f"matches-transaction {got == o['sha256'] and size == o['size']}")
        os.unlink(path)
    with open(os.path.join(ev, "hashes.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"retained .deb files: {sum(1 for d in os.listdir(os.path.join(work, 'debs')))}")

    # The production CLI against the system's own (main-only) APT configuration.
    rc, out = run([os.path.join(HARDWARE, "compile-transaction.py"), "compile"], {"PATH": "/usr/bin:/bin"})
    with open(os.path.join(ev, "production-cli.txt"), "w", encoding="utf-8") as f:
        f.write(f"exit={rc}\n{out}")
    print(f"production CLI against system APT: exit={rc} {out.strip().splitlines()[-1] if out.strip() else ''}")
    ok = (same_inv and same_plan and same_tx and digests["verify"] and closure["matches"] and not closure["unsafe"]
          and all("matches-transaction True" in l for l in lines))
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
