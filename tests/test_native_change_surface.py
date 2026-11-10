"""Contract of the shared changed-surface classifier used by CI and release."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from native_change_surface import classify  # noqa: E402


def test_docs_only_selection_is_minimal():
    surface = classify(["docs/release/README.md", "README.md"])
    assert surface["docs_only"] is True
    assert surface["full_ci"] is False
    assert surface["eros"] is False
    assert surface["miniapp"] is False


def test_staging_eros_config_does_not_select_the_eros_image_build():
    # Config-only Eros change must not rebuild the stock Eros image, but it is
    # still an Eros change for reporting purposes.
    surface = classify(["config/deploy.layer7-staging.eros.yml"])
    assert surface["eros"] is False
    assert surface["eros_config"] is True
    assert surface["full_ci"] is False


def test_eros_toml_alone_does_not_select_the_image_build():
    surface = classify(["services/eros-engine/eros-layer7-staging-model-config.toml"])
    assert surface["eros"] is False
    assert surface["eros_config"] is True


def test_eros_engine_code_selects_the_image_build():
    surface = classify(["services/eros-engine/run-local-e2e.sh"])
    assert surface["eros"] is True
    assert surface["eros_config"] is False


def test_protected_instructions_fail_closed():
    surface = classify(["AGENTS.md"])
    assert surface["protected_paths"] == ["AGENTS.md"]
    assert surface["full_ci"] is True
    assert surface["docs_only"] is False


def test_miniapp_change_selects_only_miniapp_checks():
    surface = classify(["apps/miniapp/src/main.tsx"])
    assert surface["miniapp"] is True
    assert surface["core"] is False   # a Mini App change must not select CORE
    assert surface["docs_only"] is False


def test_backend_change_selects_backend_checks():
    surface = classify(["apps/api/src/messages.ts"])
    assert surface["core"] is True
    assert surface["app_backend"] is True
    assert surface["database"] is False


def test_migration_change_fails_closed_to_the_full_suite():
    surface = classify(["packages/db/prisma/migrations/20261016120000_x/migration.sql"])
    assert surface["database"] is True
    assert surface["full_ci"] is True


def test_unknown_path_fails_closed():
    surface = classify(["weird/unmapped.file"])
    assert surface["unknown_paths"] == ["weird/unmapped.file"]
    assert surface["full_ci"] is True


def test_empty_change_fails_closed():
    surface = classify([])
    assert surface["full_ci"] is True


def test_release_infrastructure_change_fails_closed():
    surface = classify(["scripts/native_change_surface.py"])
    assert surface["release_infra"] is True
    assert surface["full_ci"] is True


def test_staging_only_merge_runs_no_heavy_production_checks():
    # Exact path set of the staging-only merge b997e1a8 that used to run the
    # stock Eros build, Playwright and a production Deploy attempt.
    surface = classify([
        "config/deploy.layer7-staging.eros.yml",
        "tests/test_layer7_staging_workflow.py",
    ])
    assert surface["eros"] is False        # no stock Eros image build
    assert surface["eros_config"] is True
    assert surface["miniapp"] is False     # no Chromium/E2E
    assert surface["database"] is False
    assert surface["release_infra"] is False
    assert surface["full_ci"] is False
