"""Contract of the shared changed-surface classifier used by CI and release."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from native_change_surface import classify  # noqa: E402


def test_docs_only_selection_is_minimal():
    surface = classify(["docs/release/README.md", "AGENTS.md"])
    assert surface["docs_only"] is True
    assert surface["full_ci"] is False
    assert surface["eros"] is False
    assert surface["miniapp"] is False


def test_staging_eros_config_does_not_select_the_eros_image_build():
    # Config-only Eros change must not rebuild the stock Eros image.
    surface = classify([
        "config/deploy.layer7-staging.eros.yml",
        "services/eros-engine/README.md",
    ])
    assert surface["eros"] is True
    assert surface["full_ci"] is False


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
