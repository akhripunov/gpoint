import importlib.util
from datetime import datetime, timezone
from pathlib import Path

spec = importlib.util.spec_from_file_location('release_telemetry', Path(__file__).with_name('release-telemetry.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_release_timeline_uses_completed_stage_jobs():
    result = module.timeline_metrics(
        commit_at='2026-10-05T09:00:00Z',
        prepare={'run_started_at': '2026-10-05T09:03:00Z', 'updated_at': '2026-10-05T09:15:00Z'},
        preflight={'run_started_at': '2026-10-05T09:16:00Z', 'updated_at': '2026-10-05T09:22:00Z'},
        production={'run_started_at': '2026-10-05T09:30:00Z', 'updated_at': '2026-10-05T09:45:00Z'},
    )
    assert result['COMMIT_TO_PREPARE_START_SECONDS'] == 180
    assert result['PREPARE_DURATION_SECONDS'] == 720
    assert result['PREPARE_TO_PREFLIGHT_START_SECONDS'] == 60
    assert result['PREFLIGHT_DURATION_SECONDS'] == 360
    assert result['PREFLIGHT_TO_DEPLOY_START_SECONDS'] == 480
    assert result['DEPLOY_DURATION_SECONDS'] == 900
    assert result['COMMIT_TO_COMPLETE_SECONDS'] == 2700


def test_runner_usage_is_separated_by_runner_labels():
    now = datetime(2026, 10, 5, 10, 0, 30, tzinfo=timezone.utc)
    result = module.summarize([[{'labels': ['ubuntu-latest'], 'runner_name': 'GitHub Actions 3',
        'started_at': '2026-10-05T10:00:00Z', 'completed_at': '2026-10-05T10:00:12Z'}]], now)
    assert result['GITHUB_HOSTED_JOB_COUNT'] == 1
    assert result['GITHUB_HOSTED_RUNNER_SECONDS'] == 12
