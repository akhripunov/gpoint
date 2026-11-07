#!/usr/bin/env python3
"""Generate a staging-specific Eros API file from isolated tenant settings."""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import sys


def write(*, eros_env: Path, source_env: Path, output: Path,
          implementation: Path = Path(__file__).with_name("eros-production-secrets.py")) -> None:
    spec = importlib.util.spec_from_file_location("eros_production_secrets", implementation)
    if spec is None or spec.loader is None:
        raise RuntimeError("canonical Eros secret writer is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.write_native_api_env(path=eros_env, source_env=source_env, output=output)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--eros-env", type=Path, default=Path("/opt/gpoint/env/eros.env"))
    parser.add_argument("--source-env", type=Path, default=Path("/opt/gpoint/env/staging.env"))
    parser.add_argument("--output", type=Path,
                        default=Path("/opt/gpoint/env/layer7-staging-eros-api.env"))
    args = parser.parse_args()
    try:
        write(eros_env=args.eros_env, source_env=args.source_env, output=args.output)
    except Exception as error:
        print("LAYER7_STAGING_EROS_ENV=BLOCKED reason=" + type(error).__name__, file=sys.stderr)
        return 1
    print("LAYER7_STAGING_EROS_ENV=PASS mode=isolated tenant_bound=true test_allowlists=empty")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
