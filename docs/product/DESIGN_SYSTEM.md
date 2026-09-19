# GPoint Design System V1 / Design Factory

GPoint uses a single canonical editable design source plus explicit visual and runtime roles:

- **PEN** — `design/gpoint-design-system.pen`: canonical editable structure, tokens, reusable components, patterns, page templates and screen composition.
- **APPROVED GOLDEN** — `design/golden/<screen>-approved-<version>-390x844.png`: owner-approved visual appearance. Goldens are immutable evidence and are never silently replaced.
- **REACT** — `apps/miniapp/src/ui` and the Mini App runtime: production implementation mapped conceptually one-to-one to Pen components.
- **VISUAL HARNESS** — `tools/visual-diff/`: regression detector for approved runtime/UI, not an autonomous designer and not a requirement that Pen-renderer pixels equal browser pixels.

If Pen rendering conflicts with an owner-approved Golden because of the proven Pen ref-rendering limitation, the Golden visual appearance wins. Pen remains the editable structural source; the Golden remains the visual source of truth.

## Design constitution

- Primary mobile viewport: **390×844**.
- Responsive validation: **360×800**, **393×852**, **430×932**.
- Editorial type: **Cormorant Garamond**. UI type: **Inter**.
- Visual language: near-black navy, warm off-white, peach/rose-gold accents, restrained surfaces/glow, thin warm borders and editorial hierarchy. Never neon or generic SaaS.
- Spacing: 4, 8, 12, 16, 20, 22, 24, 28, 32, 40, 48.
- Default page padding: 16px. Radii: 10, 14, 18, 20, 999.
- Lucide icons where appropriate; icon sizes 16, 18, 22, 24; minimum touch target 44×44.
- No horizontal overflow. Bottom navigation is fixed/safe-area aware and never covers content. Nested screens use an internal Back control.

## Pen sections

The canonical Pen file is organized as:

`00 FOUNDATIONS` → `01 COMPONENTS` → `02 PATTERNS` → `03 SCREENS` → `04 PAGE TEMPLATES` → `90 REFERENCES` → `99 ARCHIVE / LEGACY`.

Creator Dashboard V5 remains a screen, not a template replacement. Its reusable composition is the first DashboardTemplate reference.

## Reusable inventory and React mapping

| Pen component | React boundary | Status |
|---|---|---|
| Navigation / Back | `BackButton` | existing |
| Button | `Button` | existing |
| Status Badge | `StatusBadge` | existing |
| Navigation / Tabs | `Tabs` | existing |
| Navigation / Bottom | `BottomNav` | existing |
| Section / Eyebrow | `SectionEyebrow` | existing |
| Creator / Workspace Status | `WorkspaceStatusCard` | existing |
| Creator / Telegram Channel | `TelegramChannelCard` | existing |
| Creator / Tool Card | `CreatorToolCard` | existing |
| Creator / Import Settings | `ImportSettingsCard` | existing |

Current future-facing primitives are intentionally deferred until a real screen needs them: `PageHeader`, `IconButton`, `Avatar`, `SearchField`, `SegmentedControl`, `Select`, `Input`, `Textarea`, `SettingsRow`, `ListRow`, `StatCard`, `MetricCard`, `ChartCard`, `EmptyState`, `LoadingSkeleton`, `ErrorState`, `ContentCard`, `CreatorCard`, `MediaCard`, `SectionHeader`, `Modal`, `Sheet`, `ConfirmationDialog`, `Toast`, `Divider`, `Badge`, `Pill`, and `FilterChip`.

Component states are variants, not duplicate components: channel connected/disconnected/loading/no-avatar/long-title; badges connected/pending/published/rejected; buttons primary/secondary/destructive/disabled/loading; and empty/loading/error feed states.

## Page templates

The Pen `04 PAGE TEMPLATES` section defines reusable structural patterns:

- **DashboardTemplate** — InternalHeader, optional Eyebrow, PageTitle, optional Subtitle/Tabs, ScrollableContent, fixed/safe BottomNav.
- **ProfileTemplate** — InternalHeader, ProfileHero, ActionArea, ContentSections, optional BottomNav.
- **FeedTemplate** — TopBar, FeedViewport, optional filters/search, BottomNav.
- **SettingsTemplate** — InternalHeader, PageTitle, grouped SettingsSections, bottom safe area.
- **ListTemplate** — InternalHeader, optional Tabs/Filters, ScrollableList, Empty/Loading states.
- **StatsTemplate** — InternalHeader, PeriodSelector, MetricCards, ChartArea, BreakdownSections, optional BottomNav.

Templates expose documented slots where Pen supports them: `HEADER_SLOT`, `TITLE_SLOT`, `TABS_SLOT`, `PRIMARY_CONTENT_SLOT`, `SECONDARY_CONTENT_SLOT`, `BOTTOM_NAV_SLOT`, `PERIOD_SLOT`, `SUMMARY_METRICS_SLOT`, `PRIMARY_CHART_SLOT`, and `BREAKDOWN_SLOT`.

### How to build a new GPoint screen

Product requirement → choose the closest Page Template → inventory reusable components → calculate reuse → create only genuinely missing components/patterns → assemble Pen candidate → mechanical layout checks → OWNER review → save/reopen Pen → freeze approved Golden → React composition → visual/runtime QA → tests → release.

**Mode A — Standard screen:** no concept image; compose directly from the Factory.

**Mode B — New visual pattern:** concept/reference first, already constrained by the viewport, typography, spacing, radii, navigation and GPoint visual language.

**Mode C — Existing screen modification:** edit the existing Pen screen and canonical components; never start a detached replacement.

Reuse classification for every future screen: `PAGE_TEMPLATE`, `EXISTING_COMPONENT_REUSE_PERCENT`, `NEW_COMPONENTS_REQUIRED`, `NEW_VISUAL_PATTERN_REQUIRED`. If reuse is at least 80%, build directly in Pen by default.

## Golden and persistence workflow

Approved assets live in `design/golden/`, references in `design/references/`, and review exports in `design/previews/`. Each Golden records screen, version, owner approval, viewport, Pen source, date/version identifier, dynamic regions and known Pen limitations in a matching `.meta.json` file.

Every approved Pen modification must follow: **SAVE → verify disk hash changed → reopen → verify screen and reusable components exist**. Approved screens must remain editable, physically persisted, clearly labeled and easy to locate. A PNG may sit beside a screen as a locked visual reference; it never replaces editable structure.

## Tooling audits

- **Storybook:** absent. Not added in this phase because the current Vite/React stack has no Storybook dependency and the request does not justify adding a dev-only dependency surface. Component fixtures remain covered by the existing UI/test harness.
- **Lucide:** actual usage confirmed through `lucide-react` imports in `apps/miniapp/src/main.tsx`, `platform-ui.tsx`, and `src/ui/components.tsx`; individual icons are preferred for tree-shaking.
- **Radix:** no actual repository usage found; deferred.
- **shadcn/ui:** no actual repository usage found; patterns may be consulted, but no catalogue is imported.
- **Telegram UI:** use only as a source of Telegram Mini App behavior/patterns; no wholesale migration.
- **Pen/Pencil MCP:** current MCP is already connected and is sufficient for this Factory. The official OpenAI `build-web-apps` plugin is installed in Codex for frontend implementation and rendered QA; it does not replace the repo-local GPoint skill.

## Codex Design Agent Workflow

For GPoint UI/design, Pen, visual-reference, Golden, and design-to-React tasks,
use the repo-local `.agents/skills/gpoint-design` skill. Its sequence is:
classify the design mode → select a template and inventory reusable components
→ read native Pen instructions before inspection or mutation → inspect the
actual reference pixels → build and screenshot incrementally → require explicit
owner approval before React synchronization → run responsive visual QA.

Design fixtures are not production data. Pen/Storybook/visual tests may use stable fixture avatars, titles and timestamps; runtime uses actual connected Telegram data.

## Official Codex Frontend Skill Coordination

GPoint keeps `$gpoint-design` as the product-specific orchestration layer and
coordinates it with the official OpenAI `build-web-apps` skills:

- `$gpoint-design` owns GPoint's Design Factory, Pen source of truth, approved
  references/Goldens, tokens, components, viewport rules, and owner-approval
  gates.
- `frontend-app-builder` is used for faithful React implementation after an
  approved Pen/reference handoff. It does not authorize reinterpretation of
  GPoint's accepted visual direction.
- `frontend-testing-debugging` owns rendered UI validation. Prefer the official
  Browser plugin when available; use Playwright as the documented fallback when
  Browser is unavailable or unreliable.
- `react-best-practices` is applied after meaningful React component edits for
  implementation quality and performance review.

`shadcn-best-practices` is not a request to install or introduce shadcn,
Radix, or TelegramUI into GPoint. `imagegen` is used only when a genuinely new
visual asset/pattern is required and remains subordinate to GPoint constraints.

### Exact-reference fidelity

When the owner accepts a reference as “this is what we should build”, the inner
product UI is the `EXACT VISUAL TARGET`. Device frames, browser chrome, status
bars, and decorative presentation backgrounds are `PRESENTATION CHROME` and
may be excluded from the product screen; they do not downgrade the inner UI to
loose inspiration.

Every exact-reference task requires a Fidelity Ledger with, for each major
region, `REGION`, `REFERENCE OBSERVATION`, `CURRENT RENDER OBSERVATION`,
`DELTA`, `ACTION`, and `FINAL STATUS`. At minimum: Header, Navigation/Tabs,
Hero, Primary Card, Secondary Cards, Typography, spacing/rhythm, and BottomNav.
Measurements are exact only when supported by the source pixels; otherwise
they are explicitly approximate. The ledger is checked during the Pen
reference → editable screen → screenshot loop and again during the approved
Pen/reference → React → rendered browser QA loop. Passing tests or SSIM alone
does not approve the design; owner approval remains explicit.
