---
name: gpoint-design
description: Design or implement GPoint UI screens and visual systems using the canonical Pen Design Factory, approved references and Goldens, shared components, templates, and owner-approval gates; not for backend, database, ingestion, VPS, ads, or unrelated infrastructure.
---

# GPoint Design

Use this skill for GPoint UI and visual work: Pen/.pen documents, reference-to-screen work, reusable components and page templates, design-to-React, responsive layout, and visual QA.

## Official frontend workflow coordination

This skill remains the GPoint-specific orchestration layer and does not
duplicate official OpenAI frontend skills. For approved/reference-driven React
implementation, coordinate `$gpoint-design` with `frontend-app-builder`. For
rendered browser QA, use `frontend-testing-debugging`: prefer the Browser
plugin when available and use Playwright only as the documented fallback. After
meaningful React edits, apply `react-best-practices`. The
`shadcn-best-practices` skill may be consulted when relevant, but GPoint must
not adopt shadcn, Radix, TelegramUI, or another UI library merely because the
skill is installed. Use `imagegen` only for a genuinely new visual asset or
pattern, under GPoint constraints and after checking existing Factory reuse.

## Source of truth

- Editable design source: `design/gpoint-design-system.pen`.
- Approved visual source: an owner-approved Golden under `design/golden/`.
- Runtime source: the shared React UI and its visual harness.
- `docs/product/DESIGN_SYSTEM.md` is the concise repository constitution.

Use the closest existing page template and canonical components. Existing templates are DashboardTemplate, ProfileTemplate, FeedTemplate, SettingsTemplate, ListTemplate, and StatsTemplate. If reusable coverage is at least 80%, do not invent a new concept by default. Keep detached core duplicates at zero.

## Start every design task

1. Declare `DESIGN_MODE`: A standard existing pattern, B new visual pattern, or C modification of an existing screen.
2. Report the selected template, reuse percentage, existing components, genuinely new components, and whether a new pattern is required.
3. Classify each visual reference as product-screen Golden, concept reference, external inspiration, or runtime screenshot, and actually inspect image content rather than trusting its filename. If the owner explicitly accepts a reference as what GPoint should build, classify the inner product UI as `EXACT VISUAL TARGET`; separate presentation chrome (device frame, browser chrome, status bar, decorative surround) from product UI without downgrading the latter to loose inspiration.
4. Preserve the source hierarchy: owner-approved Golden for appearance, editable Pen for structure, React for runtime behavior. A proven Pen renderer limitation may be documented; never replace the editable Pen screen with a PNG.

## Pen gate

Before reading or mutating Pen, read `references/pen-workflow.md` and the relevant native Pen instructions. Call Pen MCP `read_skill()` first, then read only the referenced instructions needed for the operation, call `get_app_state()`, and use `get_style()` when style guidance is relevant. Inspect before executing. Build in sections, capture a screenshot after each major section, allow at most three correction passes per section, and classify renderer limitations separately from design errors. Before completion save, hash the physical `.pen`, reopen/re-read it, and verify persistence. If no Pen mutation is requested, perform only a non-mutating capability/read probe.

## Screen and approval gates

Use primary mobile viewport 390x844 and verify 360x800, 393x852, and 430x932 when responsive behavior matters. Respect safe areas, fixed navigation, and scrollable content. Use Lucide and existing tokens; do not introduce Radix, shadcn, TelegramUI, or arbitrary raw styles. The owner, not Codex, grants approval: use `DRAFT`, `OWNER_REVIEW_REQUIRED`, or `APPROVED_OWNER`. React synchronization normally starts only after explicit owner approval. Dynamic data in Pen and visual tests is fixture data, never a production identity or hardcoded product truth.

## Fidelity Ledger

For exact-reference work, maintain a Fidelity Ledger for each major region:
`REGION`, `REFERENCE OBSERVATION`, `CURRENT RENDER OBSERVATION`, `DELTA`,
`ACTION`, and `FINAL STATUS`. At minimum cover Header, Navigation/Tabs, Hero,
Primary Card, Secondary Cards, Typography, overall spacing/rhythm, and
BottomNav. Use exact measurements where the pixels support them and mark
estimates as approximate; never finish with only “similar”, “improved”, or
“close enough”.

Required loops:

- Pen: inspect reference → analyze geometry → edit editable Pen structure →
  screenshot → compare → update the Fidelity Ledger → bounded correction →
  owner review.
- React, only after owner approval unless explicitly requested: approved
  reference/Pen → `frontend-app-builder` implementation → Browser-first
  rendered validation (or documented Playwright fallback) → screenshot and
  Fidelity Ledger → responsive QA → owner review → release gates.

Codex cannot self-approve visual design. Preserve `DRAFT`,
`OWNER_REVIEW_REQUIRED`, and `APPROVED_OWNER` states. Stop automated iteration
only when all major regions match within reasonable renderer/font/platform
tolerance, a specific renderer limitation is documented, or a bounded loop is
exhausted and owner review is required.

## Completion

Run the visual harness on exported pixels, not only node geometry. Hard-fail clipping, overlap, overflow, missing components, placeholder icons, empty badges, and wrong section order. Preserve pre-existing worktree changes, stage only files in scope, and keep release rules in `AGENTS.md` intact.

## References

- `references/gpoint-design-system.md` — GPoint tokens, sections, templates, and reusable inventory.
- `references/pen-workflow.md` — native Pen read/inspect/edit/save/reopen protocol.
- `references/reference-fidelity.md` — reference classification, visual inspection, and bounded comparison loop.
- `references/screen-workflow.md` — modes, template selection, reuse, approval, and React handoff.
