#!/usr/bin/env python3
"""Debounce GPoint main pushes and prepare only the newest stable SHA."""
from __future__ import print_function

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

REPOSITORY = "akhripunov/gpoint"
PREPARE_PATH = ".github/workflows/gpoint-prepare.yml"
PREPARE_WORKFLOW = "gpoint-prepare.yml"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
STABILITY_SECONDS = 180


class OrchestrationError(RuntimeError):
    pass


class APIError(RuntimeError):
    def __init__(self, status, path):
        RuntimeError.__init__(self, "HTTP %s" % status)
        self.status = status
        self.path = path


class GitHubAPI(object):
    def __init__(self, token, opener=urllib.request.urlopen):
        if not token:
            raise OrchestrationError("GITHUB_TOKEN is required")
        self.token = token
        self.opener = opener

    def request(self, method, path, body=None):
        payload = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            "https://api.github.com/repos/%s/%s" % (REPOSITORY, path.lstrip("/")),
            data=payload, method=method,
            headers={"Authorization": "Bearer " + self.token,
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28",
                     "Content-Type": "application/json"})
        try:
            with self.opener(request, timeout=20) as response:
                raw = response.read()
                return json.loads(raw.decode("utf-8")) if raw else {}
        except urllib.error.HTTPError as error:
            raise APIError(error.code, path)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise OrchestrationError("GitHub API unavailable: %s" % type(error).__name__)

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, body=None):
        return self.request("POST", path, body)


def _matching_dispatch(api, sha, controller_run_id):
    runs = api.get("actions/workflows/%s/runs?branch=main&head_sha=%s&per_page=100" %
                   (PREPARE_WORKFLOW, sha)).get("workflow_runs", [])
    title = "Prepare GPoint %s coalescer/%s" % (sha, controller_run_id)
    matches = [run for run in runs if run.get("head_sha") == sha
               and run.get("head_branch") == "main" and run.get("event") == "workflow_dispatch"
               and run.get("path") == PREPARE_PATH and run.get("display_title") == title]
    if len(matches) > 1:
        raise OrchestrationError("Multiple Prepare runs match one coalescer dispatch")
    return matches[0] if matches else None


def coalesce_candidate(event, api, sleep=time.sleep, controller_run_id="local"):
    repository = event.get("repository", {}).get("full_name", "").lower()
    sha = event.get("after", "")
    if repository != REPOSITORY or event.get("ref") != "refs/heads/main" or not SHA_RE.fullmatch(sha):
        raise OrchestrationError("Only exact main push events from GPoint are eligible")
    sleep(STABILITY_SECONDS)
    latest = api.get("git/ref/heads/main").get("object", {}).get("sha", "")
    runs = api.get("actions/workflows/%s/runs?branch=main&per_page=100" % PREPARE_WORKFLOW).get("workflow_runs", [])
    active = [run for run in runs if run.get("head_branch") == "main"
              and run.get("path") == PREPARE_PATH and run.get("status") in ("queued", "in_progress")]
    for run in active:
        run_id, run_sha = str(run.get("id", "")), run.get("head_sha", "")
        if not run_id.isdigit() or not SHA_RE.fullmatch(run_sha):
            raise OrchestrationError("Unverified active Prepare run prevents safe coalescing")
        if run_sha != latest:
            try:
                api.post("actions/runs/%s/cancel" % run_id, {})
            except APIError:
                raise OrchestrationError("Unable to cancel superseded non-mutating Prepare run")
    if latest != sha:
        return {"state": "SUPERSEDED", "candidate_sha": sha, "latest_sha": latest}
    if any(run.get("head_sha") == sha for run in active):
        return {"state": "PREPARE_ALREADY_ACTIVE", "candidate_sha": sha}
    try:
        api.post("actions/workflows/%s/dispatches" % PREPARE_WORKFLOW,
                 {"ref": "main", "inputs": {"candidate": sha,
                                               "coalescer_run_id": str(controller_run_id)}})
    except (APIError, OrchestrationError):
        # Correlate an accepted-but-timed-out request. Never repeat an uncertain POST.
        for attempt in range(3):
            existing = _matching_dispatch(api, sha, controller_run_id)
            if existing:
                return {"state": "PREPARE_DISPATCH_RECONCILED", "candidate_sha": sha,
                        "prepare_run_id": str(existing.get("id", "")),
                        "prepare_run_attempt": str(existing.get("run_attempt", 1))}
            if attempt < 2:
                sleep(2)
        raise OrchestrationError("Prepare dispatch outcome is uncertain; no blind retry is allowed")
    return {"state": "PREPARE_DISPATCHED", "candidate_sha": sha}


def main():
    try:
        with open(os.environ["GITHUB_EVENT_PATH"], "r") as source:
            event = json.load(source)
        result = coalesce_candidate(event, GitHubAPI(os.environ.get("GITHUB_TOKEN", "")),
                                    controller_run_id=os.environ.get("GITHUB_RUN_ID", "local"))
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
            summary.write("## GPoint candidate coalescing\n\n")
            summary.write("- State: `%s`\n- Candidate: `%s`\n" %
                          (result["state"], result["candidate_sha"]))
            if result.get("latest_sha"):
                summary.write("- Newer main: `%s`\n" % result["latest_sha"])
        print("COALESCER_STATE=%s CANDIDATE_SHA=%s" % (result["state"], result["candidate_sha"]))
        return 0
    except (KeyError, ValueError, OSError, OrchestrationError) as error:
        print("COALESCER_STATE=FAILED REASON=%s" % type(error).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
