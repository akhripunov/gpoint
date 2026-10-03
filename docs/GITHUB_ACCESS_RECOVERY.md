# GitHub access preflight and recovery

Run this read-only check from the GPoint repository before using GitHub for a task:

```sh
python3 scripts/github-preflight.py
```

Proceed only when it prints `GITHUB_PREFLIGHT=PASS`. The check does not dispatch workflows, change credentials, or write to GitHub. It verifies DNS/TLS reachability for `github.com` and `api.github.com`, calls `gh auth status`, then validates the effective credential with `gh api user`, reads the GPoint repository, and reads one Actions runs page. Raw `gh` output is never printed because it may contain incidental account or configuration details.

The first failure is reported with one stable code:

| Code | Meaning | Recovery |
| --- | --- | --- |
| `NETWORK_BLOCKED` | DNS, TLS, routing, or timeout prevented reaching GitHub. Credentials have not been judged. | Confirm the GPoint sandbox profile is loaded; retry after the Codex session reloads. If it persists, report the exact failing host and ask the workspace administrator to inspect managed network policy. Do not switch to Full Access or use another network path. |
| `AUTH_INVALID` | The authenticated GitHub user API returned HTTP 401, proving GitHub rejected the effective credential. | Codex starts the official `gh auth login` process. The OWNER may confirm GitHub authorization in the official browser page; never send a token through chat. |
| `PERMISSION_DENIED` | GitHub authenticated the account but denied repository or Actions read access. | An OWNER/repository administrator grants only the missing repository/Actions permission, then rerun preflight. |
| `GITHUB_UNAVAILABLE` | GitHub/API is rate limited, returning a server error, or `gh` cannot complete a request for a reason other than auth or permission denial. A local `gh auth status` mismatch is shown as a warning when the authenticated-user request succeeds. | Wait and rerun; if persistent, inspect GitHub status and `gh` configuration without printing secrets. |

## Diagnostic rules

- DNS failure is a network problem, not evidence of an expired token.
- `gh auth status` is useful local state, but a credential is called invalid only after an authenticated API request receives HTTP 401. If the authenticated-user request succeeds while `gh auth status` disagrees, the effective credential works; the preflight reports the mismatch and does not mislabel it as revoked.
- A 403/404 from the known private repository or Actions endpoint is reported as a permission problem; a rate-limit response is reported as GitHub unavailable.
- Codex checks whether `GH_TOKEN` or `GITHUB_TOKEN` is set without displaying values. Either environment variable can override the credential stored by `gh`; if one is present, resolve it through the approved secret-management mechanism rather than printing it.
- The repository-local Codex profile at `.codex/config.toml` inherits `:workspace`; command networking is limited to `github.com` and `api.github.com` through the sandbox network proxy. Do not change the global Codex configuration for this purpose.

## Reauthentication

Only after preflight proves `AUTH_INVALID`, Codex may run the official CLI login flow using HTTPS and the minimum OAuth scopes needed for the requested GitHub work. If GitHub requires interactive confirmation, ask the OWNER only to approve the official GitHub page. Never ask for a password, token, copied browser URL, terminal command, or credential file. Rerun the read-only preflight after login.
