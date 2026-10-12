#!/usr/bin/python3
"""LUBARCHY hardware resolver CLI (M1-C).

    hardware/resolve.py --inventory PATH
    hardware/resolve.py --inventory -        (read the inventory from stdin)

Resolves one inventory v1 document against the production rules in
hardware/rules/ (the only rule set this command can use) and writes one
canonical plan v1 document plus one newline to stdout. It installs and
configures nothing.

Exit status:
  0   plan written; the inventory was valid (status resolved, partial or conflict)
  2   plan written with status 'invalid'; the inventory was rejected
  3   input unreadable or not strict UTF-8 JSON; nothing written to stdout
  4   production rule set rejected; nothing written to stdout
  5   internal contract failure (schema unusable, plan not valid plan v1);
      nothing written to stdout
  64  usage error; nothing written to stdout
On any non-zero status other than 2, stderr has one line
'resolve: error: <code>: <message>' without input values; on 0 and 2 stderr
is empty.
"""

import sys

sys.dont_write_bytecode = True  # the resolver persists nothing, not even bytecode

import resolver  # noqa: E402

EXIT_OK = 0
EXIT_INVALID_INVENTORY = 2
EXIT_BAD_INPUT = 3
EXIT_BAD_RULES = 4
EXIT_CONTRACT = 5
EXIT_USAGE = 64

USAGE = "usage: resolve.py --inventory PATH|-\n"


def _read_input(path):
    if path == "-":
        data = sys.stdin.buffer.read(resolver.MAX_DOCUMENT_BYTES + 1)
    else:
        try:
            with open(path, "rb") as f:
                data = f.read(resolver.MAX_DOCUMENT_BYTES + 1)
        except OSError:
            raise resolver.ResolverError("input_unreadable", "inventory cannot be read") from None
    if len(data) > resolver.MAX_DOCUMENT_BYTES:
        raise resolver.ResolverError("input_unreadable", "inventory is too large")
    return data


def _fail(error, status):
    sys.stderr.write(f"resolve: error: {error.code}: {error.message}\n")
    return status


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2 or argv[0] != "--inventory" or not argv[1]:
        sys.stderr.write(USAGE)
        return EXIT_USAGE
    try:
        inventory = resolver.parse_strict(_read_input(argv[1]), label="inventory")
    except resolver.ResolverError as e:
        return _fail(e, EXIT_BAD_INPUT)
    try:
        rules = resolver.load_production_rules()
    except resolver.ResolverError as e:
        return _fail(e, EXIT_BAD_RULES)
    try:
        plan = resolver.check_plan(resolver.resolve(inventory, rules))
        output = resolver.canonical_json(plan) + b"\n"
    except resolver.ResolverError as e:
        return _fail(e, EXIT_CONTRACT)
    sys.stdout.buffer.write(output)
    sys.stdout.buffer.flush()
    return EXIT_INVALID_INVENTORY if plan["status"] == "invalid" else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
