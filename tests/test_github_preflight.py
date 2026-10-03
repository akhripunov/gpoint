import importlib.util
import pathlib
import sys
import unittest
import urllib.error


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("github_preflight", ROOT / "scripts/github-preflight.py")
preflight = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = preflight
SPEC.loader.exec_module(preflight)


class Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class GithubPreflightTests(unittest.TestCase):
    def runner_for(self, results):
        def runner(args):
            key = " ".join(args)
            return results[key]
        return runner

    def success_results(self):
        return {
            "gh auth status --hostname github.com": preflight.CommandResult(0),
            "gh api user --jq .login": preflight.CommandResult(0, "akhripunov\n"),
            "gh api repos/akhripunov/gpoint --jq .full_name": preflight.CommandResult(0, "akhripunov/gpoint\n"),
            "gh api repos/akhripunov/gpoint/actions/runs?per_page=1 --jq .total_count": preflight.CommandResult(0, "35\n"),
        }

    def test_success_checks_network_auth_repository_and_actions_read_only(self):
        seen = []
        def opener(request, timeout):
            seen.append(request.full_url)
            return Response()
        output = preflight.run_preflight(opener, self.runner_for(self.success_results()))
        self.assertEqual(len(seen), 2)
        self.assertEqual(len(output), 5)

    def test_dns_failure_is_network_blocked_before_credentials_are_considered(self):
        def opener(*_, **__):
            raise urllib.error.URLError("nodename nor servname provided")
        with self.assertRaises(preflight.PreflightFailure) as raised:
            preflight.run_preflight(opener, lambda _args: self.fail("gh must not run when DNS fails"))
        self.assertEqual(raised.exception.code, "NETWORK_BLOCKED")

    def test_credential_is_invalid_only_after_authenticated_api_returns_401(self):
        results = self.success_results()
        results["gh api user --jq .login"] = preflight.CommandResult(1, "", "HTTP 401: Bad credentials")
        with self.assertRaises(preflight.PreflightFailure) as raised:
            preflight.run_preflight(lambda *_args, **_kwargs: Response(), self.runner_for(results))
        self.assertEqual(raised.exception.code, "AUTH_INVALID")

    def test_repository_or_actions_forbidden_is_permission_denied(self):
        for key in (
            "gh api repos/akhripunov/gpoint --jq .full_name",
            "gh api repos/akhripunov/gpoint/actions/runs?per_page=1 --jq .total_count",
        ):
            results = self.success_results()
            results[key] = preflight.CommandResult(1, "", "HTTP 403: Resource not accessible by integration")
            with self.subTest(endpoint=key), self.assertRaises(preflight.PreflightFailure) as raised:
                preflight.run_preflight(lambda *_args, **_kwargs: Response(), self.runner_for(results))
            self.assertEqual(raised.exception.code, "PERMISSION_DENIED")

    def test_rate_limit_and_server_errors_are_github_unavailable(self):
        for error in ("HTTP 403: API rate limit exceeded", "HTTP 503: Service Unavailable"):
            results = self.success_results()
            results["gh api user --jq .login"] = preflight.CommandResult(1, "", error)
            with self.subTest(error=error), self.assertRaises(preflight.PreflightFailure) as raised:
                preflight.run_preflight(lambda *_args, **_kwargs: Response(), self.runner_for(results))
            self.assertEqual(raised.exception.code, "GITHUB_UNAVAILABLE")

    def test_successful_api_is_authoritative_when_local_auth_status_disagrees(self):
        results = self.success_results()
        results["gh auth status --hostname github.com"] = preflight.CommandResult(1, "", "stored token rejected")
        output = preflight.run_preflight(lambda *_args, **_kwargs: Response(), self.runner_for(results))
        self.assertIn("local status differs", output[1])


if __name__ == "__main__":
    unittest.main()
