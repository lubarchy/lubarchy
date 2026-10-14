#!/usr/bin/python3
"""LUBARCHY Debian source activation CLI (M1-J; system-nonmutating).

    hardware/compile-source-activation.py inspect
    hardware/compile-source-activation.py compile
    hardware/compile-source-activation.py validate --contract PATH|-
    hardware/compile-source-activation.py verify   --contract PATH|-
    hardware/compile-source-activation.py render   --contract PATH|- --output-dir DIR
    hardware/compile-source-activation.py simulate --contract PATH|- --workdir DIR

inspect   prints the normalized APT source pre-state of this system and its digest.
compile   inspects /etc/apt and compiles one canonical
          lubarchy.packages.source-activation/v1 contract for the accepted
          Debian policy (D-013: main + non-free-firmware).
validate  checks a contract against the v1 contract only.
verify    recompiles from fresh state and requires a byte-identical contract;
          prints {"state": "revalidated"|"already_satisfied", ...}.
render    writes the LUBARCHY-owned source file of a verified contract into an
          empty workspace directory (never /etc).
simulate  runs an isolated, authenticated `apt-get update` for the contract's
          post-state inside an empty workspace directory and prints the
          authenticated metadata evidence (network access to Debian only).

There is no activate operation: this command never writes /etc/apt or any
system path. It accepts no URI, suite, component, keyring, package, APT
option or system destination path; workspaces must be caller-owned
directories outside every system path.

Exit status: 0 ok; 2 malformed input or source file; 3 stale contract;
4 unauthorised source, trusted=, missing Signed-By; 5 ambiguous ownership or
unsafe path; 6 authenticated metadata failure; 7 contract/internal failure;
64 usage. On error stderr has one line
'compile-source-activation: error: <code>: <message>'.
"""

import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import resolver  # noqa: E402
import source_activation as sa  # noqa: E402

MAX_CONTRACT_BYTES = 4 * 1024 * 1024
USAGE = ("usage: compile-source-activation.py inspect | compile\n"
         "       compile-source-activation.py validate|verify --contract PATH|-\n"
         "       compile-source-activation.py render --contract PATH|- --output-dir DIR\n"
         "       compile-source-activation.py simulate --contract PATH|- --workdir DIR\n")


def _read_contract(path):
    try:
        data = sys.stdin.buffer.read(MAX_CONTRACT_BYTES + 1) if path == "-" else sa.read_regular(path, MAX_CONTRACT_BYTES)
    except OSError:
        raise sa.SourceError("input_malformed", "contract cannot be read") from None
    except sa.SourceError:
        raise sa.SourceError("input_malformed", "contract cannot be read as a regular file") from None
    if len(data) > MAX_CONTRACT_BYTES:
        raise sa.SourceError("input_malformed", "contract is too large")
    try:
        doc = resolver.parse_strict(data, label="contract")
    except resolver.ResolverError:
        raise sa.SourceError("input_malformed", "contract is not strict JSON") from None
    try:
        return sa.check_activation(doc)
    except sa.SourceError as e:
        raise sa.SourceError("input_malformed", e.message) from None


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        if argv == ["inspect"]:
            state = sa.inspect("/")
            out = {"pre_state": state, "pre_state_digest": sa.pre_state_digest(state)}
        elif argv == ["compile"]:
            out = sa.compile_system("/")
        elif len(argv) == 3 and argv[0] in ("validate", "verify") and argv[1] == "--contract" and argv[2]:
            c = _read_contract(argv[2])
            if argv[0] == "validate":
                out = {"state": "valid", "activation_digest": sa.activation_digest(c), "action": c["action"]}
            else:
                fresh = sa.verify_activation(c, "/")
                out = {"state": "already_satisfied" if fresh["action"] == "already_satisfied" else "revalidated",
                       "activation_digest": sa.activation_digest(fresh), "action": fresh["action"]}
        elif len(argv) == 5 and argv[0] in ("render", "simulate") and argv[1] == "--contract" and argv[2] \
                and argv[3] == ("--output-dir" if argv[0] == "render" else "--workdir") and argv[4]:
            c = sa.verify_activation(_read_contract(argv[2]), "/")
            if argv[0] == "render":
                path = sa.render_to(c, argv[4])
                out = {"state": "rendered" if path else "nothing_to_render", "owned_path": sa.OWNED_PATH,
                       "sha256": None if path is None else c["owned_source"]["sha256"]}
            else:
                _conf, ev = sa.isolated_update(c, argv[4], "/")
                out = {"state": "authenticated", "activation_digest": sa.activation_digest(c), **ev}
        else:
            sys.stderr.write(USAGE)
            return sa.EXIT_USAGE
    except sa.SourceError as e:
        sys.stderr.write(f"compile-source-activation: error: {e.code}: {e.message}\n")
        return e.exit_status
    except resolver.ResolverError as e:
        sys.stderr.write(f"compile-source-activation: error: contract_failure: {e.code}\n")
        return sa.EXIT_CONTRACT
    sys.stdout.buffer.write(resolver.canonical_json(out) + b"\n")
    sys.stdout.buffer.flush()
    return sa.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
