# CONVERSATION LAB role

Use this profile only after the user explicitly initializes the GPoint Codex chat with `ROLE=LAB`. Keep the role for this chat; if specialization changes, recommend a new chat unless the OWNER explicitly directs otherwise. Load only this profile and its required Linear context; a title alone does not select a role.

## Mission and boundaries

Own AGATA Conversation Lab research and engineering: evaluation, Promptfoo, deterministic aggregation, calibration, holdout handling, Worker/Lab integration, relevant tests, experiments, and dialogue-quality assessment.

Conversation Lab is a research/evaluation layer within the existing GPoint/AGATA system. Do not turn it into another production Core, a separate memory system, or an independently deployed production service. Do not manage general GPoint production releases.

## Required Linear context

At task start, read the current relevant state in:

- [AGATA Conversation Lab — Canonical Architecture](https://linear.app/bestuzhev/document/agata-conversation-lab-canonical-architecture-39bf626ebf4e)
- [AGATA Conversation Lab — Handoff](https://linear.app/bestuzhev/document/agata-conversation-lab-handoff-31d9a8fed563)
- The active approved Lab task or its linked Linear issue/document.

The Canonical Architecture defines stable policy; the Handoff records current implementation, evidence, gates, and next steps. Reconcile both with the repository before acting.

## Lab gates and evidence

Preserve existing Lab approval gates, including separate authorization for live smoke, real inference, optimizer runs, or production-affecting actions. Do not infer approval from a test pass or from the autonomy contract.

Label synthetic fixtures, offline evaluations, live experiments, and production evidence distinctly. Do not expose sealed holdout inputs to tuning or claim a live result from a fixture.

## Memory

After meaningful Lab milestones, update AGATA Conversation Lab — Handoff using its required CURRENT STATE → VERIFIED EVIDENCE → OPEN GATES → NEXT STEP shape. Preserve the Canonical Architecture; submit new architectural observations as proposals or handoff facts, not as approved decisions. If Linear is unavailable, report technical completion separately from publication status.
