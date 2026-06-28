---
name: Feature request
about: Propose a connector, role, or capability
title: "[feat] "
labels: enhancement
---

## Problem / who it helps

<!-- Which role (tester / developer / PO) and what they can't do today. -->

## Proposal

<!-- What you'd add. For a new connector, note the source and how its access
control maps to acl:* tags. -->

## Fit check (see docs/team-brain.md)

- [ ] Reuses memento's storage (doesn't rebuild storage/search/graph)
- [ ] Carries source ACL into `acl:*` tags, **fail-closed**
- [ ] Offline-testable (injectable client, no creds in tests)
- [ ] Each memory keeps a `src:` citation back-link

## Alternatives considered

<!-- e.g. adopting an existing MCP instead of building (docs §7). -->
