import unittest

import coalesce_release_candidate as coalescing

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40


def push_event(sha):
    return {"ref": "refs/heads/main", "after": sha,
            "repository": {"full_name": "akhripunov/gpoint"}}


def prepare_run(sha, run_id):
    return {"id": run_id, "head_sha": sha, "head_branch": "main", "status": "in_progress",
            "path": ".github/workflows/gpoint-prepare.yml"}


class FakeAPI:
    def __init__(self, main_sha, runs=()):
        self.main_sha = main_sha
        self.runs = list(runs)
        self.calls = []

    def get(self, path):
        self.calls.append(("GET", path))
        if path == "git/ref/heads/main":
            return {"object": {"sha": self.main_sha}}
        if path.startswith("actions/workflows/gpoint-prepare.yml/runs"):
            return {"workflow_runs": self.runs}
        raise AssertionError(path)

    def post(self, path, body):
        self.calls.append(("POST", path, body))
        return {}


class CandidateCoalescingTests(unittest.TestCase):
    def test_only_latest_stable_c_candidate_is_dispatched(self):
        api = FakeAPI(SHA_C, [prepare_run(SHA_A, 11), prepare_run(SHA_B, 12)])
        waits = []
        a = coalescing.coalesce_candidate(push_event(SHA_A), api, sleep=waits.append)
        b = coalescing.coalesce_candidate(push_event(SHA_B), api, sleep=waits.append)
        c = coalescing.coalesce_candidate(push_event(SHA_C), api, sleep=waits.append)
        self.assertEqual(a["state"], "SUPERSEDED")
        self.assertEqual(b["state"], "SUPERSEDED")
        self.assertEqual(c["state"], "PREPARE_DISPATCHED")
        self.assertEqual(waits, [180, 180, 180])
        self.assertEqual(sum(call[0] == "POST" and call[1].endswith("/dispatches") for call in api.calls), 1)
        cancelled = {call[1].split("/")[-2] for call in api.calls
                     if call[0] == "POST" and call[1].endswith("/cancel")}
        self.assertEqual(cancelled, {"11", "12"})

    def test_dispatch_requires_exact_current_main(self):
        api = FakeAPI(SHA_C)
        result = coalescing.coalesce_candidate(push_event(SHA_B), api, sleep=lambda _: None)
        self.assertEqual(result["state"], "SUPERSEDED")
        self.assertFalse(any(call[0] == "POST" and call[1].endswith("/dispatches") for call in api.calls))

    def test_cancel_uncertainty_blocks_prepare_dispatch(self):
        api = FakeAPI(SHA_C, [prepare_run(SHA_A, 11)])
        def fail(path, body):
            raise coalescing.APIError(503, path)
        api.post = fail
        with self.assertRaisesRegex(coalescing.OrchestrationError, "cancel"):
            coalescing.coalesce_candidate(push_event(SHA_C), api, sleep=lambda _: None)
        self.assertFalse(any(call[0] == "POST" and call[1].endswith("/dispatches") for call in api.calls))

    def test_accepted_but_timed_out_dispatch_is_read_back_without_second_post(self):
        class AcceptedButTimedOut(FakeAPI):
            def post(self, path, body):
                self.calls.append(("POST", path, body))
                if path.endswith("/dispatches"):
                    self.runs.append({"id": 14, "head_sha": SHA_C, "head_branch": "main",
                                      "event": "workflow_dispatch", "path": coalescing.PREPARE_PATH,
                                      "display_title": "Prepare GPoint %s coalescer/owner-run" % SHA_C,
                                      "run_attempt": 1, "status": "queued"})
                    raise coalescing.APIError(503, path)
                return {}

        api = AcceptedButTimedOut(SHA_C)
        result = coalescing.coalesce_candidate(push_event(SHA_C), api, sleep=lambda _: None,
                                                controller_run_id="owner-run")
        self.assertEqual(result["state"], "PREPARE_DISPATCH_RECONCILED")
        self.assertEqual(result["prepare_run_id"], "14")
        self.assertEqual(sum(call[0] == "POST" and call[1].endswith("/dispatches") for call in api.calls), 1)

    def test_prepare_push_trigger_is_replaced_by_coalescer_dispatch(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        prepare = (root / ".github/workflows/gpoint-prepare.yml").read_text()
        coalescer = (root / ".github/workflows/gpoint-candidate-coalescer.yml").read_text()
        self.assertNotIn("push:", prepare.split("permissions:", 1)[0])
        self.assertIn("candidate:", prepare.split("permissions:", 1)[0])
        self.assertIn("actions: write", coalescer)
        self.assertIn("gpoint-production", coalescer)


if __name__ == "__main__":
    unittest.main()
