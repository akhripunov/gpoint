# GPoint design system reference

The canonical editable design is `design/gpoint-design-system.pen`. Keep its
top-level organization: `00 FOUNDATIONS`, `01 COMPONENTS`, `02 PATTERNS`,
`03 SCREENS`, `04 PAGE TEMPLATES`, `90 REFERENCES`, and `99 ARCHIVE / LEGACY`.

Use the existing page templates before composing a screen: DashboardTemplate,
ProfileTemplate, FeedTemplate, SettingsTemplate, ListTemplate, and StatsTemplate.
The reusable component inventory includes Navigation / Back, Button, Status Badge,
Navigation / Tabs, Navigation / Bottom, Section / Eyebrow, Creator / Workspace
Status, Creator / Telegram Channel, Creator / Tool Card, and Creator / Import
Settings. The React mapping is documented in `docs/product/DESIGN_SYSTEM.md`.

The design system and approved Goldens are visual sources, not authorization to
change product behavior. Keep tokens, safe areas, responsive viewports, and
Lucide icon usage canonical. A fixture avatar or content value is not a
production identity.
