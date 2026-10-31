import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("kamal_proxy_routes", ROOT / "scripts/kamal_proxy_routes.py")
ROUTES = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ROUTES)

CORE = "a" * 64
MINIAPP = "b" * 64


def table(*rows):
    output = "\x1b[3;94mService\x1b[0m Host Path Target State TLS\n"
    for row in rows:
        output += " ".join(row) + "\n"
    return output


def test_parser_accepts_empty_table_and_exact_candidate_routes():
    assert ROUTES.parse_routes(table()) == []
    routes = ROUTES.parse_routes(table(
        ("gpoint-core", "gpoint.one", "/api", CORE[:12] + ":3000", "running", "yes"),
        ("gpoint-miniapp", "gpoint.one", "/", MINIAPP[:12] + ":80", "running", "yes"),
    ))
    assert ROUTES.validate_gpoint_routes(routes, {
        "gpoint-core": (CORE, 3000), "gpoint-miniapp": (MINIAPP, 80),
    }) == ["gpoint-core", "gpoint-miniapp"]


def test_validator_rejects_nonrunning_owned_routes():
    routes = ROUTES.parse_routes(table(
        ("gpoint-core", "gpoint.one", "/api", CORE[:12] + ":3000", "deploying", "yes"),
    ))
    with pytest.raises(ValueError):
        ROUTES.validate_gpoint_routes(routes, {"gpoint-core": (CORE, 3000)})


@pytest.mark.parametrize("row", [
    ("other-service", "other.example", "/", CORE[:12] + ":80", "running", "yes"),
    ("gpoint-core", "gpoint.one", "/", CORE[:12] + ":3000", "running", "yes"),
    ("gpoint-core", "gpoint.one", "/api", CORE[:12] + ":3000", "running", "no"),
    ("gpoint-core", "gpoint.one", "/api", "c" * 12 + ":3000", "running", "yes"),
])
def test_validator_refuses_any_route_not_owned_by_the_exact_attempt(row):
    routes = ROUTES.parse_routes(table(row))
    with pytest.raises(ValueError):
        ROUTES.validate_gpoint_routes(routes, {
            "gpoint-core": (CORE, 3000), "gpoint-miniapp": (MINIAPP, 80),
        })


def test_parser_rejects_unknown_format_duplicate_services_and_bad_targets():
    for output in (
        "unexpected output\n",
        table(("gpoint-core", "gpoint.one", "/api", "not/a/target", "running", "yes")),
        table(
            ("gpoint-core", "gpoint.one", "/api", CORE[:12] + ":3000", "running", "yes"),
            ("gpoint-core", "gpoint.one", "/api", CORE[:12] + ":3000", "running", "yes"),
        ),
    ):
        with pytest.raises(ValueError):
            ROUTES.parse_routes(output)
