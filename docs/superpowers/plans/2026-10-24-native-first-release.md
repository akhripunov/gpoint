# ME-52 Native First implementation plan

- [x] Reconstruct the four-job Native Release workflow on Ubuntu.
- [x] Gate Build on Workspace and Python success; publish the three existing
  immutable GPoint/AGATA/GSales image roles with digest outputs, SBOM, and
  provenance.
- [x] Keep candidate branch execution nonproduction and exact-SHA-bound.
- [x] Keep production job read-only until a separately authorized implementation
  provides all migration, backup/restore, resource, rollback, and Verify gates.
- [ ] Run static workflow validation and targeted regression tests locally.
- [ ] Publish only the verified technical branch, then collect hosted CI/Build
  evidence for its exact commit.
- [ ] Record the scoped handoff and gate result in Linear ME-52.

Production Deploy, Eros activation, real-user cutover, and V5 removal are not
part of this implementation step.
