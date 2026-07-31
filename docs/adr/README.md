# Architecture Decision Records

Architecture Decision Records (ADRs) capture durable design choices for Agent
Config Bridge. Accepted ADRs are historical records: change their status only to
mark them deprecated or superseded, and create a new ADR for a materially new
decision.

## Index

| ADR                                                          | Decision                                  | Status   | Date       |
| ------------------------------------------------------------ | ----------------------------------------- | -------- | ---------- |
| [0001](0001-render-target-specific-artifacts.md)             | Render target-specific artifacts          | Accepted | 2026-07-14 |
| [0002](0002-never-share-runtime-state.md)                    | Never share runtime state                 | Accepted | 2026-07-14 |
| [0003](0003-use-dual-marketplace-packages.md)                | Use dual marketplace packages             | Accepted | 2026-07-14 |
| [0004](0004-manage-declarative-settings-by-owned-leaf.md)    | Manage declarative settings by owned leaf | Accepted | 2026-07-14 |
| [0005](0005-use-host-scheduler-adapters.md)                  | Use host scheduler adapters               | Accepted | 2026-07-14 |
| [0006](0006-generate-codex-instruction-profiles.md)          | Generate Codex instruction profiles       | Accepted | 2026-07-29 |
| [0007](0007-preserve-codex-profile-hook-trust-state.md)      | Preserve Codex profile Hook trust state   | Accepted | 2026-07-30 |

## Statuses

- **Proposed**: open for review.
- **Accepted**: the project is implementing or enforcing the decision.
- **Rejected**: considered and deliberately not adopted.
- **Deprecated**: retained for context but no longer recommended.
- **Superseded**: replaced by a named later ADR.

## Adding an ADR

Use the next four-digit number and a short kebab-case title. Include at least:

1. status and date;
2. context and decision drivers;
3. considered options;
4. the decision and rationale;
5. positive, negative, and security consequences;
6. links to related ADRs and authoritative references.

Keep implementation detail in the main documentation unless it is necessary to
understand the decision. Do not rewrite trade-offs after acceptance; supersede
the ADR when the decision changes.
