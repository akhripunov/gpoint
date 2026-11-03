from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/gpoint-native-release.yml"


def test_approved_prisma_apply_is_inside_the_protected_deploy_before_kamal():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    deploy = jobs["deploy"]
    steps = deploy["steps"]
    ids = [step.get("id") for step in steps]
    assert ids.index("component_plan") < ids.index("migration_baseline")
    assert ids.index("component_plan") < ids.index("migration_apply")
    assert ids.index("migration_plan") < ids.index("deployment")
    assert ids.index("deployment") < ids.index("migration_apply") < ids.index("kamal_apply")
    apply = next(step for step in steps if step.get("id") == "migration_apply")
    assert "if" not in apply or "success()" in apply["if"]
    assert 'if [[ "$APPLY" == false ]]' in apply["run"]
    assert 'echo "agata_head=$BASELINE_AGATA_HEAD"' in apply["run"]
    assert apply["env"]["BASELINE_AGATA_HEAD"] == "${{ steps.migration_baseline.outputs.agata_head }}"
    assert not any("steps.migration_apply.outputs." in value for value in apply["env"].values())
    assert "systemctl start gpoint-backup.service" in apply["run"]
    assert "gpoint-backup-policy.py freshness" in apply["run"]
    assert "prisma migrate deploy" in apply["run"]
    assert apply["env"]["APP_IMAGE"] == "ghcr.io/akhripunov/gpoint-runtime@${{ steps.component_plan.outputs.app_digest }}"
    assert "apply_failed" not in apply
    kamal = next(step for step in steps if step.get("id") == "kamal_apply")
    assert not kamal.get("if") or "success()" in kamal["if"]
    verify = next(step for step in steps if step.get("id") == "verify")
    assert "steps.migration_apply.outputs.prisma_migrations" in verify["env"]["EXPECTED_PRISMA_MIGRATIONS"]
    assert verify["env"]["BASELINE_AGATA_HEAD"] == "${{ steps.migration_baseline.outputs.agata_head }}"
    assert '"$BASELINE_AGATA_HEAD" "$EXPECTED_ROLE_IDENTITIES_B64" <<\'REMOTE\'' in verify["run"]
    assert set(jobs) == {"workspace", "python", "build", "deploy", "rollback", "backup_maintenance", "migration_rehearsal"}


def test_prisma_apply_never_changes_agata_migrations_or_rolls_back_database():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    deploy = workflow["jobs"]["deploy"]
    apply = next(step for step in deploy["steps"] if step.get("id") == "migration_apply")
    body = apply["run"]
    assert "--require-no-agata-pending" in next(
        step["run"] for step in deploy["steps"] if step.get("id") == "migration_baseline"
    )
    assert "prisma migrate resolve" not in body
    assert "prisma migrate reset" not in body
    assert "prisma migrate diff" not in body
    assert "DROP DATABASE" not in body


def test_prisma_migration_plan_and_apply_remain_exact_sha_and_environment_scoped():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    deploy = workflow["jobs"]["deploy"]
    assert deploy["environment"]["name"] == "production"
    assert deploy["concurrency"]["group"] == "gpoint-production-release"
    assert "REQUESTED_SHA" in "\n".join(step.get("run", "") for step in deploy["steps"])
    plan = next(step for step in deploy["steps"] if step.get("id") == "migration_plan")
    assert "packages/db/prisma/production-approved-migrations.json" in plan["run"]


def test_ci_rehearses_prisma_history_schema_parity_and_idempotent_redeploy():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    workspace = workflow["jobs"]["workspace"]
    step = next(step for step in workspace["steps"]
                if step.get("name") == "Generate Prisma client and apply test migrations")
    assert workspace["env"]["SHADOW_DATABASE_URL"].endswith("/gpoint_shadow")
    create_shadow = 'createdb --maintenance-db="$DATABASE_URL" --owner=gpoint gpoint_shadow'
    assert "command -v createdb" in step["run"]
    assert step["run"].index(create_shadow) < step["run"].index("prisma migrate diff")
    assert "--from-migrations prisma/migrations" in step["run"]
    assert "--to-schema-datamodel prisma/schema.prisma" in step["run"]
    assert "--shadow-database-url \"$SHADOW_DATABASE_URL\"" in step["run"]
    assert "--exit-code" in step["run"]
    assert step["run"].count("prisma migrate deploy") == 2
    assert "prisma migrate status" in step["run"]
