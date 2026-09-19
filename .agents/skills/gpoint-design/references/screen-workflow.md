# Screen workflow

Declare one mode before work:

- **A — Standard:** existing template and components cover the screen.
- **B — New visual pattern:** a genuinely reusable pattern is missing.
- **C — Existing screen modification:** preserve the approved screen and change
  only the requested behavior or visual component.

Audit the nearest template and component inventory. Report reuse percentage,
new components, and new patterns. Prefer DashboardTemplate for creator
dashboard work and reuse existing instances; >=80% reuse means no new concept by
default. Build Pen structure before React. For a product-screen Golden, follow
the approved composition exactly; for a concept, preserve product geometry while
separating presentation-only device chrome.

The normal handoff is:
`Pen draft → visual screenshot/metrics → OWNER_REVIEW_REQUIRED → explicit
APPROVED_OWNER → React implementation → responsive runtime QA`.
React, backend, E2E, and deployment are out of scope for a design-only task.
