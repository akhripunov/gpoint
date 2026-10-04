# G Sales production release completion

Goal: complete the OWNER-approved existing G Sales integration and canonical GPoint release. No production mutation until exact-SHA/schema-impact approval.

Source brief: OWNER production-integration attachment and release-efficiency requirements in Dev, plus the 2026-10-04 release audit. No new architecture: immutable Prepare Releases -> Mac -> canonical Deploy -> immutable evidence -> Linear/ledger.

## Constraints

- Existing installed V3 stays on Mac; preserve source parity and GPOINT service. Include only approved PR11/PR15 necessary source; no separate research PR merge.
- Three Linux/amd64 application images, exact SHA/config/native identity and checksum, no apply rebuild.
- AGATA0011 role-bound FORCE RLS and six-table checksummed G Sales migration; GPoint-only full backup including identity and checksum schemas. Separate runtime/migration roles. Secrets outside Git/output.
- Outreach, auto-reply, management and listener OFF on first technical rollout. Real Telegram acceptance separate.
- One final integration main candidate/Prepare after regression/review; no repeated intermediate main cycles.

## Execution ledger

- [x] Preserve original dirty patch; isolate candidate based on approved V3 e933b8a plus G Sales release patch.
- [x] Fix worker gateway cold-start readiness deadlock and verify bound tenant in runtime readiness, with failing regression tests first.
- [x] Complete Compose Mac-provider contract and isolated production-configuration tests.
- [x] Complete existing release migration/config/backup integration, including gsales_meta and checksummed migration read-back.
- [x] Wire G Sales overlay/start/health/image parity/evidence into canonical adapter and preflight.
- [x] Complete production provisioning plan and fail-closed validation without mutating VPS before OWNER approval.
- [ ] Run local disposable DB/worker/workspace/release suites, three image builds and bundle rehearsal; independent review/fix blocking findings.
- [ ] Publish one reviewed SHA to main, run one main Prepare, verify immutable receipt/publication; present exact-SHA/schema-impact approval packet.
- [ ] After approval: fresh baseline/lock/full backup, provisioning and migrations, canonical runtime switch, health/parity/evidence/Linear. Verify repo clean gates and preserve all dirty-source accounting.

Ruling: earlier scratch was based on PR11 alone and differed from installed V3; this candidate starts at approved PR15 which already includes PR11, without changing installed worker.

Local verification: workspace build PASS; 117 release tests PASS; Mac controller 17 tests PASS; real PostgreSQL 71 GSales + 147 Core + shared/unread checks PASS; hardened-image guarded migration and protected provisioning/backup restore PASS. Four installed Mac source assets match this candidate; production remains unchanged. Hosted branch rehearsal and final main Prepare remain required.
