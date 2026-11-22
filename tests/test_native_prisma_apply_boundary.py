from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/gpoint-release.yml"


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
    assert apply["if"] == "inputs.mode == 'deploy' && (inputs.mode != 'rollback')"
    assert 'if [[ "$APPLY" == false ]]' in apply["run"]
    assert "AGATA" not in apply["run"]
    assert not any("steps.migration_apply.outputs." in value for value in apply["env"].values())
    assert "gpoint-scheduled-backup.py" in apply["run"]
    assert "--release-backup" in apply["run"]
    assert "systemctl start gpoint-backup.service" not in apply["run"]
    assert 'gpoint-backup-policy.py" freshness' in apply["run"]
    assert "prisma migrate deploy" in apply["run"]
    assert apply["env"]["APP_IMAGE"] == "ghcr.io/akhripunov/gpoint-runtime@${{ steps.component_plan.outputs.migration_app_digest }}"
    assert "apply_failed" not in apply
    kamal = next(step for step in steps if step.get("id") == "kamal_apply")
    assert kamal["if"] == "inputs.mode == 'deploy' && ((success()) && inputs.mode != 'rollback')"
    verify = next(step for step in steps if step.get("id") == "verify")
    assert "steps.migration_apply.outputs.prisma_migrations" in verify["env"]["EXPECTED_PRISMA_MIGRATIONS"]
    assert "BASELINE_AGATA_HEAD" not in verify["env"]
    assert '"$EXPECTED_ROLE_IDENTITIES_B64" "${EROS_DIGEST:--}" "$expected_eros_upstream" "$EROS_INSTALLED" <<\'REMOTE\'' in verify["run"]
    assert set(jobs) == {"tests", "build", "deploy"}
    assert "ARTIFACT_PREPARE" not in jobs["deploy"]["if"]


def test_prisma_apply_does_not_touch_retired_ledgers_or_roll_back_database():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    deploy = workflow["jobs"]["deploy"]
    apply = next(step for step in deploy["steps"] if step.get("id") == "migration_apply")
    body = apply["run"]
    baseline = next(step["run"] for step in deploy["steps"] if step.get("id") == "migration_baseline")
    assert "agata.alembic_version" not in baseline
    assert "AGATA_HEAD" not in baseline
    assert "prisma migrate resolve" not in body
    assert "prisma migrate reset" not in body
    assert "prisma migrate diff" not in body
    assert "DROP DATABASE" not in body


def test_prisma_migration_plan_and_apply_remain_exact_sha_and_environment_scoped():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    deploy = workflow["jobs"]["deploy"]
    assert deploy["environment"]["name"] == "production"
    assert "gpoint-production-release" in deploy["concurrency"]["group"]
    assert "REQUESTED_SHA" in "\n".join(step.get("run", "") for step in deploy["steps"])
    plan = next(step for step in deploy["steps"] if step.get("id") == "migration_plan")
    assert "packages/db/prisma/production-approved-migrations.json" in plan["run"]
    assert "--baseline-sha \"$BASELINE_SOURCE_SHA\"" in plan["run"]
    assert "--candidate-sha \"$CANDIDATE_SHA\"" in plan["run"]
    eros = next(step for step in deploy["steps"] if step.get("id") == "eros_database")
    assert eros["if"].startswith("inputs.mode == 'deploy'")


def test_production_job_reads_host_and_ssh_from_secrets():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    deploy = workflow["jobs"]["deploy"]
    assert "GPOINT_PRODUCTION_HOST" not in deploy["env"]
    assert "GPOINT_SSH_KEY" not in deploy["env"]
    assert "GPOINT_KNOWN_HOSTS_FILE" not in deploy["env"]
    credentials = next(step for step in deploy["steps"]
                       if step.get("name") == "Install verified Kamal SSH credentials")
    assert "if" not in credentials
    assert credentials["env"]["GPOINT_PRODUCTION_HOST"] == "${{ secrets.GPOINT_KAMAL_HOST }}"


def test_ci_rehearses_prisma_history_schema_parity_and_idempotent_redeploy():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    workspace = workflow["jobs"]["tests"]
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


def test_prisma_apply_stages_every_backup_dependency_and_reads_manifest_argument():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    apply = next(step for step in workflow["jobs"]["deploy"]["steps"]
                 if step.get("id") == "migration_apply")["run"]

    assert "agata-production-migration.py" in apply
    assert 'python3 - "$BASELINE_PRISMA_HEAD" "$backup_manifest" <<\'PY\'' in apply
    assert "manifest = json.loads(sys.argv[2])" in apply


def test_prisma_apply_passes_backup_path_as_the_remote_scripts_ninth_argument():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    apply = next(step for step in workflow["jobs"]["deploy"]["steps"]
                 if step.get("id") == "migration_apply")["run"]

    assert '"$BASELINE_PRISMA_HEAD" "$backup_path" <<\'REMOTE_APPLY\'' in apply
    assert 'backup_path="${10}"' not in apply
    assert 'backup_path="$9"' in apply
    assert '--manifest "$backup_path"' in apply


def test_runtime_verify_preserves_an_empty_eros_digest_argument_position():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    verify = next(step for step in workflow["jobs"]["deploy"]["steps"]
                  if step.get("id") == "verify")["run"]

    assert '"${EROS_DIGEST:--}"' in verify
    assert '[[ "$eros_digest" != "-" ]] || eros_digest=""' in verify


def test_rollback_prisma_admission_rejects_unfinished_migration_rows():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    deploy = workflow["jobs"]["deploy"]
    preflight = next(step for step in deploy["steps"] if step.get("id") == "baseline")

    assert "finished_at IS NULL AND rolled_back_at IS NULL" in preflight["run"]


def test_eros_sqlx_ledger_is_verified_after_kamal_boots_the_candidate():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["deploy"]["steps"]
    kamal = next(index for index, step in enumerate(steps) if step.get("id") == "kamal_apply")
    verify = next(index for index, step in enumerate(steps)
                  if step.get("id") == "eros_database_verify")

    assert verify > kamal
    assert steps[verify]["if"].startswith("steps.component_plan.outputs.deploy_eros == 'true'")
    assert "--mode verify-applied" in steps[verify]["run"]
