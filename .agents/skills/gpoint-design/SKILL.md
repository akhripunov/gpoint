---
name: gpoint-design
description: Design or implement GPoint UI screens and visual systems using the canonical Pen Design Factory, approved references and Goldens, shared components, templates, and owner-approval gates; not for backend, database, ingestion, VPS, ads, or unrelated infrastructure.
---

# GPoint Design

Use this skill for GPoint UI and visual work: Pen/.pen documents, reference-to-screen work, reusable components and page templates, design-to-React, responsive layout, and visual QA.

## Source of truth

- Editable design source: `design/gpoint-design-system.pen`.
- Approved visual source: an owner-approved Golden under `design/golden/`.
- Runtime source: the shared React UI and its visual harness.
- `docs/product/DESIGN_SYSTEM.md` is the concise repository constitution.

Use the closest existing page template and canonical components. Existing templates are DashboardTemplate, ProfileTemplate, FeedTemplate, SettingsTemplate, ListTemplate, and StatsTemplate. If reusable coverage is at least 80%, do not invent a new concept by default. Keep detached core duplicates at zero.

## Start every design task

1. Declare `DESIGN_MODE`: A standard existing pattern, B new visual pattern, or C modification of an existing screen.
2. Report the selected template, reuse percentage, existing components, genuinely new components, and whether a new pattern is required.
3. Classify each visual reference as product-screen Golden, concept reference, external inspiration, or runtime screenshot, and actually inspect image content rather than trusting its filename.
4. Preserve the source hierarchy: owner-approved Golden for appearance, editable Pen for structure, React for runtime behavior. A proven Pen renderer limitation may be documented; never replace the editable Pen screen with a PNG.

## Pen gate

Before reading or mutating Pen, read `references/pen-workflow.md` and the relevant native Pen instructions. Call Pen MCP `read_skill()` first, then read only the referenced instructions needed for the operation, call `get_app_state()`, and use `get_style()` when style guidance is relevant. Inspect before executing. Build in sections, capture a screenshot after each major section, allow at most three correction passes per section, and classify renderer limitations separately from design errors. Before completion save, hash the physical `.pen`, reopen/re-read it, and verify persistence. If no Pen mutation is requested, perform only a non-mutating capability/read probe.

## Screen and approval gates

Use primary mobile viewport 390x844 and verify 360x800, 393x852, and 430x932 when responsive behavior matters. Respect safe areas, fixed navigation, and scrollable content. Use Lucide and existing tokens; do not introduce Radix, shadcn, TelegramUI, or arbitrary raw styles. The owner, not Codex, grants approval: use `DRAFT`, `OWNER_REVIEW_REQUIRED`, or `APPROVED_OWNER`. React synchronization normally starts only after explicit owner approval. Dynamic data in Pen and visual tests is fixture data, never a production identity or hardcoded product truth.

## Completion

Run the visual harness on exported pixels, not only node geometry. Hard-fail clipping, overlap, overflow, missing components, placeholder icons, empty badges, and wrong section order. Preserve pre-existing worktree changes, stage only files in scope, and keep release rules in `AGENTS.md` intact.

## References

- `references/gpoint-design-system.md` — GPoint tokens, sections, templates, and reusable inventory.
- `references/pen-workflow.md` — native Pen read/inspect/edit/save/reopen protocol.
- `references/reference-fidelity.md` — reference classification, visual inspection, and bounded comparison loop.
- `references/screen-workflow.md` — modes, template selection, reuse, approval, and React handoff.
