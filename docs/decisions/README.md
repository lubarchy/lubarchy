# Architecture Decision Records

Significant LUBARCHY architecture decisions are recorded as ADRs in this
directory. The [decision register](../decisions.md) lists all accepted and
pending decisions.

## When an ADR is required

- Choosing or changing a core component (bootloader, filesystem layout,
  encryption, installer technology, desktop sourcing, Secure Boot model,
  package infrastructure).
- Adding a package source or external repository.
- Changing security, release, or support policy.
- Superseding an accepted decision.
- Resolving any pending decision subject (P-001 to P-009).

## When an ADR is not required

- Implementation work within an accepted decision.
- Bug fixes, refactoring, and documentation corrections that do not change
  architecture.
- Routine package version updates within existing policy.

## Status vocabulary

| Status | Meaning |
| --- | --- |
| Proposed | Under review; not authoritative |
| Accepted | Approved by the CTO; authoritative |
| Superseded | Replaced by a later ADR, which must be linked |
| Rejected | Considered and not adopted |

## Numbering

Files use `NNNN-short-title.md`, for example `0001-bootloader.md`. Numbers are
sequential and never reused. [0000-template.md](0000-template.md) is the
template.

## Rules

- A superseded ADR links to the ADR that supersedes it, and the new ADR links
  back.
- When an architecture decision changes, update
  [decisions.md](../decisions.md) and
  [project-state.md](../project-state.md) in the same change.
- Claude cannot accept architecture decisions independently. Claude may draft
  a Proposed ADR with evidence; only the CTO accepts it.
- Decisions D-001 to D-012 predate this process and live in the decision
  register.
