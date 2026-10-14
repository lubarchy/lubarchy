#!/usr/bin/python3
"""LUBARCHY privileged hardware-package apply executor CLI (M1-K; root only).

    hardware/apply-packages.py apply
    hardware/apply-packages.py status
    hardware/apply-packages.py recover

apply   applies the current qualified hardware plan of this machine:
        LUBARCHY transaction lock -> journal -> activation of the
        LUBARCHY-owned Debian source file (D-013 policy) -> authenticated
        `apt-get update` -> fresh probe, production resolve and package
        transaction -> root-owned staging -> final revalidation and bundle
        re-hash -> offline APT install of the direct targets -> package,
        source and file validation. Prints the final journal.
status  prints the apply and recovery journals (phase, whether APT had
        started, whether a recovery resolved the attempt).
recover recovers the interrupted LUBARCHY hardware-package transaction
        (M1-L): classifies the REAL package state against the apply journal
        and the re-verified staged bundle; finalizes attempts that changed
        no package, validates an already complete post-state without
        re-running APT, completes a bounded interrupted dpkg run only with
        exact-package `dpkg --configure`/`--triggers-only`, and otherwise
        records manual_recovery_required without mutating anything. It
        never runs APT, `apt-get -f install` or `dpkg --configure -a`.

It accepts no package, version, architecture, URI, suite, component, APT or
dpkg option, environment setting, staging root or source path: everything
is derived from source-controlled policy and fresh local state. It requires
effective UID 0 and never escalates privilege itself.

APT/dpkg installation is not atomic. After APT has started a failure is
journaled as `failed` and nothing is rolled back or repaired automatically;
later invocations then report recovery_required.

Exit status: 0 complete, nothing to apply, recovered, abandoned (no package
mutation) or nothing to recover; 2 malformed state; 3 stale;
4 unauthorised source or plan rejected; 5 unsafe change set or path;
6 payload/metadata/provenance mismatch; 7 contract/internal failure;
8 another transaction holds the lock; 9 recovery required / manual recovery
required; 10 apply, recovery or post-apply validation failed; 64 usage;
77 not root. On error stderr has one
line 'apply-packages: error: <code>: <message>'.
"""

import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import package_apply as pa  # noqa: E402
import resolver  # noqa: E402

USAGE = "usage: apply-packages.py apply | status | recover\n"


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv not in (["apply"], ["status"], ["recover"]):
        sys.stderr.write(USAGE)
        return pa.EXIT_USAGE
    try:
        host = pa.RealHost()
        if argv == ["apply"]:
            out = pa.apply_current_plan(host)
        elif argv == ["recover"]:
            import package_recovery as pr
            out = pr.recover_interrupted_transaction(host)
            if out.get("result") == "manual_recovery_required":
                sys.stdout.buffer.write(resolver.canonical_json(out) + b"\n")
                sys.stderr.write("apply-packages: error: manual_recovery_required: " + (out.get("reason") or "") + "\n")
                return pa.EXIT_RECOVERY
        else:
            out = pa.status(host)
    except pa.ApplyError as e:
        sys.stderr.write(f"apply-packages: error: {e.code}: {e.message}\n")
        return e.exit_status
    sys.stdout.buffer.write(resolver.canonical_json(out) + b"\n")
    sys.stdout.buffer.flush()
    return pa.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
