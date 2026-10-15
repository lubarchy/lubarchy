#!/usr/bin/python3
"""LUBARCHY post-install hardware validation CLI (M1-M; read-only).

    hardware/validate-hardware.py validate
    hardware/validate-hardware.py revalidate     < previous-result.json
    hardware/validate-hardware.py check-current  < result.json
    hardware/validate-hardware.py evidence

validate       validates the current qualified hardware state: a fresh probe,
               the production rules and the existing resolver give the plan;
               the running system is compared against it. Prints one
               lubarchy.hardware.validation-result/v1 document.
revalidate     the same, plus continuity against a previous result read from
               stdin (a previously validated device that is gone fails, for
               example after a reboot).
check-current  reads a result from stdin and reports whether it is still
               current (every bound digest unchanged) or stale.
evidence       prints the normalized, redacted runtime evidence (qualification
               aid; read-only).

It accepts no device, package, driver, path, command, check, environment or
other caller input: what is checked and what PASS means come only from the
plan and the source-controlled validation profile. It installs, loads,
binds, writes and repairs nothing and needs no root. Run as root it can also
read the kernel log on restricted systems and the LUBARCHY apply journal
(exact package versions); without root those observations are LIMITED.

Exit status: 0 PASS / current; 1 FAIL; 2 LIMITED or NOT_TESTED; 3 stale;
4 malformed input or evidence; 7 internal/observation failure; 64 usage.
"""

import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import hardware_validation as hv  # noqa: E402
import resolver  # noqa: E402

USAGE = "usage: validate-hardware.py validate | revalidate | check-current | evidence\n"
MAX_INPUT = 4 * 1024 * 1024


def _read_stdin():
    data = sys.stdin.buffer.read(MAX_INPUT + 1)
    if len(data) > MAX_INPUT:
        raise hv.ValidationError("malformed_result", "input is too large")
    try:
        return hv.check_result(resolver.parse_strict(data, "malformed_result", "input"))
    except resolver.ResolverError as e:
        raise hv.ValidationError("malformed_result", e.message) from None


def main(argv=None, runtime_factory=None, probe_factory=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv not in (["validate"], ["revalidate"], ["check-current"], ["evidence"]):
        sys.stderr.write(USAGE)
        return 64
    try:
        previous = _read_stdin() if argv[0] in ("revalidate", "check-current") else None
        if probe_factory is None:
            import probe
            probe_factory = probe.Probe
        if runtime_factory is None:
            runtime_factory = hv.RealRuntime
        inventory = probe_factory().inventory()
        rules = resolver.load_production_rules()
        plan = resolver.check_plan(resolver.resolve(inventory, rules))
        names = [p["name"] for p in plan["packages"] if p["disposition"] == "require"]
        ev = runtime_factory().collect(inventory, names)
        if argv[0] == "evidence":
            out, rc = {"inventory": inventory, "runtime": ev}, 0
        elif argv[0] == "check-current":
            out = hv.freshness(previous, inventory, rules, ev)
            rc = 0 if out["current"] else 3
        else:
            out = hv.evaluate(inventory, rules, ev, previous=previous)
            rc = hv.OVERALL_EXIT[out["status"]]
    except hv.ValidationError as e:
        sys.stderr.write(f"validate-hardware: error: {e.code}: {e.message}\n")
        return 4 if e.code in ("malformed_result", "malformed_evidence", "stale_previous") else 7
    except resolver.ResolverError as e:
        sys.stderr.write(f"validate-hardware: error: {e.code}: {e.message}\n")
        return 7
    except Exception as e:  # noqa: BLE001 - probe errors and similar: no partial output
        code = getattr(e, "code", "internal_failure")
        sys.stderr.write(f"validate-hardware: error: {code}\n")
        return 7
    sys.stdout.buffer.write(resolver.canonical_json(out) + b"\n")
    sys.stdout.buffer.flush()
    return rc


if __name__ == "__main__":
    sys.exit(main())
