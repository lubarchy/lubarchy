#!/usr/bin/python3
"""LUBARCHY package transaction CLI (M1-I; read-only).

    hardware/compile-transaction.py compile [--expect-plan-digest D]
                                            [--expect-inventory-digest D]
                                            [--expect-rule-set-digest D]
    hardware/compile-transaction.py verify   --transaction PATH|-
    hardware/compile-transaction.py validate --transaction PATH|-

compile   probes this machine (hardware/probe.py), resolves the inventory
          against the production rules in hardware/rules/, and compiles the
          install-only package transaction against the system's own APT/dpkg
          state; writes one canonical lubarchy.hardware.package-transaction/v1
          document plus a newline. Package names come only from that freshly
          recomputed plan; the optional expectations are digests, never
          packages.
verify    recompiles from fresh state and requires a byte-identical
          transaction; writes {"state": "revalidated", "transaction_digest": ...}.
validate  checks a transaction document against the v1 contract only.

There is no apply operation: this command installs, removes, upgrades and
configures nothing, activates no source and takes no package-manager lock.
It accepts no package names, versions, release/source selectors, APT or dpkg
options, rule directories, schemas, binaries or configuration files, and it
does not inherit the caller's environment for the commands it runs.

Exit status:
  0   ready (compile/verify) or valid (validate)
  2   input malformed (transaction document unreadable or invalid JSON/contract)
  3   stale transaction (inventory, rules, plan, installed state, holds,
      configuration or repository metadata changed)
  4   plan rejected or unauthorised source (invalid/conflict plan,
      policy_review_required, unsupported effects, insecure configuration)
  5   unsafe APT change set (upgrade, downgrade, removal, reinstall,
      architecture change, held package, unresolvable)
  6   repository provenance failure (unauthorised identity/component, missing
      authenticated SHA-256)
  7   contract or internal failure (probe, rules, schema, package manager)
  64  usage error
On any non-zero status stderr has one line
'compile-transaction: error: <code>: <message>'.
"""

import os
import sys

sys.dont_write_bytecode = True
# The sibling modules live next to this file; resolve them from there even
# under `python3 -I` (no current directory, no user site, no PYTHON* env).
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import package_transaction as pt  # noqa: E402
import probe  # noqa: E402
import resolver  # noqa: E402

MAX_TRANSACTION_BYTES = 4 * 1024 * 1024
DIGEST_OPTIONS = {"--expect-plan-digest": "plan_digest", "--expect-inventory-digest": "inventory_digest",
                  "--expect-rule-set-digest": "rule_set_digest"}
USAGE = ("usage: compile-transaction.py compile [--expect-plan-digest D] [--expect-inventory-digest D] "
         "[--expect-rule-set-digest D]\n"
         "       compile-transaction.py verify --transaction PATH|-\n"
         "       compile-transaction.py validate --transaction PATH|-\n")


def _fail(error):
    sys.stderr.write(f"compile-transaction: error: {error.code}: {error.message}\n")
    return error.exit_status


def _read_transaction(path):
    try:
        if path == "-":
            data = sys.stdin.buffer.read(MAX_TRANSACTION_BYTES + 1)
        else:
            with open(path, "rb") as f:
                data = f.read(MAX_TRANSACTION_BYTES + 1)
    except OSError:
        raise pt.TransactionError("input_malformed", "transaction cannot be read") from None
    if len(data) > MAX_TRANSACTION_BYTES:
        raise pt.TransactionError("input_malformed", "transaction is too large")
    try:
        doc = resolver.parse_strict(data, label="transaction")
    except resolver.ResolverError:
        raise pt.TransactionError("input_malformed", "transaction is not strict JSON") from None
    try:
        return pt.check_transaction(doc)
    except pt.TransactionError as e:
        raise pt.TransactionError("input_malformed", e.message) from None


def _fresh_inputs():
    """Fixed trusted implementations only: the real probe, the production
    rules and the system package manager."""
    try:
        inventory = probe.Probe().inventory()
    except probe.ProbeError as e:
        raise pt.TransactionError("contract_failure", f"probe failed ({e.code})") from None
    try:
        rules = resolver.load_production_rules()
    except resolver.ResolverError as e:
        raise pt.TransactionError("contract_failure", f"production rules rejected ({e.code})") from None
    return inventory, rules, pt.SystemBackend()


def _parse_expectations(args):
    expected = {}
    if len(args) % 2:
        return None
    for opt, value in zip(args[0::2], args[1::2]):
        key = DIGEST_OPTIONS.get(opt)
        if key is None or key in expected or not value.startswith("sha256:") or len(value) != 71:
            return None
        expected[key] = value
    return expected


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        if argv and argv[0] == "compile":
            expected = _parse_expectations(argv[1:])
            if expected is None:
                sys.stderr.write(USAGE)
                return pt.EXIT_USAGE
            inventory, rules, backend = _fresh_inputs()
            tx = pt.compile_transaction(inventory, rules, backend, expected)
            output = resolver.canonical_json(tx) + b"\n"
        elif len(argv) == 3 and argv[0] in ("verify", "validate") and argv[1] == "--transaction" and argv[2]:
            tx = _read_transaction(argv[2])
            if argv[0] == "validate":
                output = resolver.canonical_json({"state": "valid", "transaction_digest": pt.transaction_digest(tx)}) + b"\n"
            else:
                inventory, rules, backend = _fresh_inputs()
                fresh = pt.verify_transaction(tx, inventory, rules, backend)
                output = resolver.canonical_json({"state": "revalidated",
                                                  "transaction_digest": pt.transaction_digest(fresh)}) + b"\n"
        else:
            sys.stderr.write(USAGE)
            return pt.EXIT_USAGE
    except pt.TransactionError as e:
        return _fail(e)
    except resolver.ResolverError as e:
        return _fail(pt.TransactionError("contract_failure", e.code))
    sys.stdout.buffer.write(output)
    sys.stdout.buffer.flush()
    return pt.EXIT_READY


if __name__ == "__main__":
    sys.exit(main())
