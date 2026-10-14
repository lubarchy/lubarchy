#!/usr/bin/python3
"""LUBARCHY staged package acquisition CLI (M1-J; system-nonmutating).

    hardware/stage-packages.py stage  --workspace DIR
    hardware/stage-packages.py verify --workspace DIR --transaction-digest D

stage   runs the whole chain inside an empty, caller-owned workspace:
        fresh source-activation compile of /etc/apt (D-013 policy) ->
        isolated authenticated `apt-get update` for its post-state
        (DIR/apt) -> fresh probe -> production resolve -> package
        transaction compiled against that isolated APT state -> exact
        acquisition of the transaction's complete change set ->
        payload verification -> atomic finalization of
        DIR/bundles/<transaction-hex>/ (manifest.json + archives/).
        Prints the manifest summary.
verify  verifies DIR/bundles/<hex>/ for the given transaction digest: layout,
        canonical manifest, bundle digest, every payload (regular file, name,
        size, SHA-256, dpkg-deb identity, mode).

There is no apply or activate operation: nothing is installed, configured,
activated or written outside the workspace; /etc/apt, the dpkg database, the
system APT cache and /var/lib/lubarchy are never written. The command
accepts no package, version, URI, suite, component, keyring, APT option or
destination path; the workspace must be caller-owned, outside every system
path, and empty for `stage`.

Exit status: 0 ok; 2 malformed input; 3 stale; 4 unauthorised source or
plan rejected; 5 unsafe path, ambiguous ownership or unsafe change set;
6 payload/metadata/provenance mismatch; 7 contract/internal failure;
64 usage. On error stderr has one line 'stage-packages: error: <code>: <message>'.
"""

import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import package_staging as ps  # noqa: E402
import package_transaction as pt  # noqa: E402
import probe  # noqa: E402
import resolver  # noqa: E402
import source_activation as sa  # noqa: E402

USAGE = ("usage: stage-packages.py stage --workspace DIR\n"
         "       stage-packages.py verify --workspace DIR --transaction-digest D\n")


def stage(workspace):
    ws = sa.check_workspace(workspace)
    if os.listdir(ws):
        raise sa.SourceError("unsafe_path", "workspace must be empty")
    contract = sa.compile_system("/")
    os.mkdir(os.path.join(ws, "apt"), 0o700)
    conf, metadata = sa.isolated_update(contract, os.path.join(ws, "apt"), "/")
    try:
        inventory = probe.Probe().inventory()
    except probe.ProbeError as e:
        raise pt.TransactionError("contract_failure", f"probe failed ({e.code})") from None
    tx = pt.compile_transaction(inventory, resolver.load_production_rules(), pt.SystemBackend(apt_config=conf))
    os.mkdir(os.path.join(ws, "bundles"), 0o700)
    bundle, manifest, _results = ps.stage_bundle(tx, sa.activation_digest(contract),
                                                 metadata["repository_metadata_digest"],
                                                 os.path.join(ws, "bundles"), ps.AptAcquirer(conf), ps.DebInspector())
    return {"state": "finalized", "bundle": "bundles/" + os.path.basename(bundle),
            "transaction_digest": manifest["transaction_digest"], "bundle_digest": manifest["bundle_digest"],
            "source_activation_digest": manifest["source_activation_digest"],
            "repository_metadata_digest": manifest["repository_metadata_digest"],
            "activation_action": contract["action"],
            "payloads": [{k: p[k] for k in ("package", "architecture", "version", "relation", "sha256", "size")}
                         for p in manifest["payloads"]],
            "direct_targets": manifest["direct_targets"]}


def verify(workspace, digest):
    ws = sa.check_workspace(workspace, create=False)
    root = ps.check_staging_root(os.path.join(ws, "bundles"))
    m, results = ps.verify_bundle(os.path.join(root, ps.bundle_name(digest)), ps.DebInspector(),
                                  expected={"transaction_digest": digest})
    return {"state": "verified", "transaction_digest": m["transaction_digest"], "bundle_digest": m["bundle_digest"],
            "payloads": len(results)}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        if len(argv) == 3 and argv[:2] == ["stage", "--workspace"] and argv[2]:
            out = stage(argv[2])
        elif len(argv) == 5 and argv[0] == "verify" and argv[1] == "--workspace" and argv[3] == "--transaction-digest":
            out = verify(argv[2], argv[4])
        else:
            sys.stderr.write(USAGE)
            return ps.EXIT_USAGE
    except (ps.StagingError, sa.SourceError, pt.TransactionError) as e:
        sys.stderr.write(f"stage-packages: error: {e.code}: {e.message}\n")
        return e.exit_status
    except resolver.ResolverError as e:
        sys.stderr.write(f"stage-packages: error: contract_failure: {e.code}\n")
        return ps.EXIT_CONTRACT
    sys.stdout.buffer.write(resolver.canonical_json(out) + b"\n")
    sys.stdout.buffer.flush()
    return ps.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
