# GPoint first cutover: one server-side owner

OWNER approved on 2026-10-31 in this chat: one temporary VPS systemd service
and temporary localhost-only SSH access for the first Kamal cutover. Ordinary
releases remain GitHub CI/Build/GHCR -> stock Kamal -> runtime Verify.

The previous observer-owned SSH sequence lost its transport during Docker
stop. A lease-only watchdog is rejected: it could restart Compose while a
late Kamal command still creates consumers. A pause-only unit is insufficient
after Caddy retirement. One attempt-scoped transient service therefore owns
all state-changing cutover phases and their existing verification scripts.
SSH only submits and observes; observer death does not terminate the owner.

Use unchanged stock Kamal 2.12.0 by pinned tool image, no VPS builds and no
permanent controller. Stage exact-SHA release tooling/config/hooks and a
SHA-256 manifest in a root-owned private /run directory. Create an expiring,
restricted localhost SSH key only for this attempt; preserve unrelated keys
and remove the exact added line and private credentials on terminal exit.
Serialize with a VPS flock in addition to GitHub concurrency and Kamal locks.

Execute pause, Caddy retirement, request roles, route checks, consumers and
complete Verify serially. Run recovery only after the failed command has
returned and fresh ownership/DB/consumer checks pass. Pre-Caddy recovery may
start only exact baseline IDs while Caddy/config/DB/private invariants are
unchanged and Kamal is absent. Later recovery must prove all candidate
consumers and Proxy stopped before starting legacy consumers. Unknown state
is BLOCKED. Owner-process crash or VPS reboot is reconciliation-required,
never a competing timer-based Apply or rollback.

ME-54 remains open until real isolated Docker fault injection (loss/killing of
the observer during first/second/last stops and after Caddy handoff), exact-ID
recovery and exclusion tests pass. Keep the production hard stop until then.
Production first cutover additionally needs fresh identity/migration parity,
backup/offsite/isolated restore, capacity and explicit protected authorization.
No Eros/migrations/GSales/Sensitive Detector/MAXIBOT/network configuration
change is included. Shutdown f30bc776 remains a separate app-source candidate.
