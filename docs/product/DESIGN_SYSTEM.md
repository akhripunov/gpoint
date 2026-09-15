# GPoint design system V1

Dark-first editorial system: warm ivory text, charcoal surfaces, muted rose accent, soft borders, and restrained serif italics for display moments. Tokens live in `apps/miniapp/src/style.css` (`--background`, `--surface`, `--elevated`, semantic text/accent/danger/success colors, and radius scale). Components are implemented as reusable React boundaries in `main.tsx`: navigation, cards, avatars, VIP card, tabs, forms, empty state, toast, and moderation actions.

Mobile behavior is primary: responsive two-column creator grid, safe-area-aware fixed navigation, scroll-snap-ready reel cards, minimum 320px viewport, and no horizontal overflow by layout construction.
