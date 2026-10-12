# Production hardware rules

This directory will hold LUBARCHY's production compatibility rules: one
`lubarchy.hardware.rule/v1` document per file, named `<rule id>.json`, valid
against [`../schemas/rule-v1.schema.json`](../schemas/rule-v1.schema.json).

**It holds no rules in M1-A.** A production rule needs a researched,
authoritative hardware-to-package mapping and CTO approval; neither exists
yet. The hardware contract gate (`tests/hardware-contracts.py`) fails if a
rule file appears here during M1-A.

Test-only rules live under
[`tests/fixtures/hardware/rules/`](../../tests/fixtures/hardware/rules/) and
must never be copied here: their ids start with `test.` and their package
names (`lubarchy-test-*`) are synthetic.

Rules are data. They contain no commands, shell fragments or code, and a
package source named in a rule is never enabled by the rule (see
[hardware enablement](../../docs/hardware-enablement.md)).
