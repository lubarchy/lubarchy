#!/usr/bin/python3
"""LUBARCHY hardware-enablement coordinator CLI (M1-O; root only).

    hardware/manage-hardware.py status
    hardware/manage-hardware.py reconcile

reconcile converges this machine toward its current source-controlled
          qualified hardware plan, or stops safely with a precise state:
          orchestration lock -> persistent-state check -> package recovery
          first (M1-L, bounded) -> fresh probe and production resolve ->
          read-only package precheck -> the existing apply executor only if a
          package change is required (M1-K) -> fresh validation (M1-M) ->
          reboot_required (it never reboots) or a terminal outcome ->
          conservative GC through the existing collector (M1-N). Prints the
          final run journal (lubarchy.hardware.orchestration-journal/v1).
status    prints a read-only summary of the latest run, package recovery
          state, pending reboot and whether the stored validation is still
          current. It takes no lock and writes nothing.

It accepts no package, device, path, rule, plan, transaction, attempt,
recovery or receipt identity, validation file, GC candidate, APT option,
environment setting, reboot or force flag. Everything is derived from
source-controlled policy, fresh local observation and the primitives' own
journals. It requires effective UID 0 and never escalates privilege itself.

Exit status: 0 complete; 1 validation_failed; 2 validation_limited; 3 stale;
4 policy_blocked; 5 reboot_required; 6 hardware converged but GC failed or
was partial; 7 failed; 8 another reconcile holds the orchestration lock;
9 manual_recovery_required; 10 persistent orchestration state is invalid
(preserved, nothing mutated); 64 usage; 77 not root. status exits 0, or 10
when the stored state is invalid. On error stderr has one line
'manage-hardware: error: <code>: <reason>'.
"""

import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import hardware_orchestration as ho  # noqa: E402
import resolver  # noqa: E402

USAGE = "usage: manage-hardware.py status | reconcile\n"


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv not in (["status"], ["reconcile"]):
        sys.stderr.write(USAGE)
        return ho.EXIT_USAGE
    try:
        host = ho.RealHost()
        if argv == ["reconcile"]:
            out = ho.reconcile(host)
            rc = ho.exit_status(out)
        else:
            out = ho.status(host)
            rc = ho.EXIT_STATE_INVALID if out["state"] == "invalid" else ho.EXIT_OK
    except ho.OrchestrationError as e:
        sys.stderr.write(f"manage-hardware: error: {e.code}: {e.reason}\n")
        return e.exit_status
    except OSError as e:
        sys.stderr.write(f"manage-hardware: error: failed: filesystem operation failed ({e.errno})\n")
        return ho.EXIT_FAILED
    sys.stdout.buffer.write(resolver.canonical_json(out) + b"\n")
    sys.stdout.buffer.flush()
    return rc


if __name__ == "__main__":
    sys.exit(main())
