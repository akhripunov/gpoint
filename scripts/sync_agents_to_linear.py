#!/usr/bin/env python3
"""Mirror this checkout's AGENTS.md into one pre-existing Linear document."""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable


ENDPOINT = "https://api.linear.app/graphql"
MAX_ATTEMPTS = 3
TIMEOUT_SECONDS = 20
GITHUB_MAIN_REF = "https://api.github.com/repos/akhripunov/gpoint/git/ref/heads/main"
SHA40 = re.compile(r"^[a-f0-9]{40}$")


class SyncError(RuntimeError):
    """A safe-to-display sync failure; never includes request headers or body."""


def _retry_delay(attempt: int, retry_after: str | None = None) -> float:
    if retry_after:
        try:
            return min(max(float(retry_after), 0.0), 8.0)
        except ValueError:
            pass
    return float(attempt)


def graphql_request(
    query: str,
    variables: dict[str, Any],
    api_key: str,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Issue a bounded Linear GraphQL request without logging credentials."""
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    request = urllib.request.Request(
        ENDPOINT,
        data=payload,
        headers={
            "Authorization": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )

    for attempt in range(1, MAX_ATTEMPTS + 1):
        retry_after = None
        try:
            with opener(request, timeout=TIMEOUT_SECONDS) as response:
                status = getattr(response, "status", 200)
                response_body = response.read()
            if status == 429 or status >= 500:
                if attempt < MAX_ATTEMPTS:
                    sleep(_retry_delay(attempt, retry_after))
                    continue
                raise SyncError(f"Linear API unavailable after {MAX_ATTEMPTS} attempts (HTTP {status})")
            try:
                result = json.loads(response_body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SyncError("Linear API returned invalid JSON") from exc
            if not isinstance(result, dict):
                raise SyncError("Linear API returned an invalid response")
            errors = result.get("errors")
            if errors:
                # Do not log arbitrary error messages, GraphQL variables, tokens
                # or document bodies. Only an allowlisted machine-readable code.
                codes = {
                    error.get("extensions", {}).get("code")
                    for error in errors
                    if isinstance(error, dict) and isinstance(error.get("extensions"), dict)
                    and isinstance(error["extensions"].get("code"), str)
                }
                safe_codes = {"RATELIMITED", "INTERNAL_SERVER_ERROR",
                              "SERVICE_UNAVAILABLE", "TIMEOUT", "UNAUTHENTICATED",
                              "FORBIDDEN", "BAD_USER_INPUT", "GRAPHQL_VALIDATION_FAILED"}
                code = sorted(codes & safe_codes)[0] if codes & safe_codes else "UNKNOWN"
                transient_codes = {"RATELIMITED", "INTERNAL_SERVER_ERROR",
                                   "SERVICE_UNAVAILABLE", "TIMEOUT"}
                retryable = bool(codes) and codes.issubset(transient_codes)
                if retryable and attempt < MAX_ATTEMPTS:
                    sleep(_retry_delay(attempt))
                    continue
                raise SyncError(f"Linear GraphQL request failed (code={code})")
            data = result.get("data")
            if not isinstance(data, dict):
                raise SyncError("Linear API response is missing data")
            return data
        except urllib.error.HTTPError as exc:
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            if (exc.code == 429 or exc.code >= 500) and attempt < MAX_ATTEMPTS:
                sleep(_retry_delay(attempt, retry_after))
                continue
            if exc.code == 429 or exc.code >= 500:
                raise SyncError(
                    f"Linear API unavailable after {MAX_ATTEMPTS} attempts (HTTP {exc.code})"
                ) from None
            raise SyncError(f"Linear API rejected request (HTTP {exc.code})") from None
        except (urllib.error.URLError, TimeoutError):
            if attempt < MAX_ATTEMPTS:
                sleep(_retry_delay(attempt))
                continue
            raise SyncError(f"Linear API unavailable after {MAX_ATTEMPTS} attempts (network)") from None
    raise SyncError("Linear API request exhausted retries")


def verify_current_main_sha(
    source_sha: str,
    github_token: str,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> None:
    """Fail closed unless this event still represents the current GPoint main."""
    if not SHA40.fullmatch(source_sha or ""):
        raise SyncError("SOURCE_SHA must be a full commit SHA")
    if not github_token.strip():
        raise SyncError("GITHUB_TOKEN is required to verify current main")
    request = urllib.request.Request(
        GITHUB_MAIN_REF,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {github_token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="GET",
    )
    try:
        with opener(request, timeout=TIMEOUT_SECONDS) as response:
            if getattr(response, "status", 200) != 200:
                raise SyncError("GitHub current-main lookup failed")
            body = response.read(65536)
        if len(body) >= 65536:
            raise SyncError("GitHub current-main response is invalid")
        payload = json.loads(body.decode("utf-8"))
        current_sha = payload.get("object", {}).get("sha") if isinstance(payload, dict) else None
    except SyncError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError, UnicodeError, json.JSONDecodeError):
        raise SyncError("GitHub current-main lookup failed") from None
    if not isinstance(current_sha, str) or not SHA40.fullmatch(current_sha):
        raise SyncError("GitHub current-main response is invalid")
    if current_sha != source_sha:
        raise SyncError("source commit is no longer current main")


def _linear_markdown_comparison_form(content: str) -> str:
    """Apply only Markdown-equivalent formatting observed in Linear read-back."""
    output: list[str] = []
    plain: list[str] = []
    fence: tuple[str, int] | None = None
    just_closed_fence = False

    def flush_plain() -> None:
        if not plain:
            return
        text = "".join(plain)
        text = re.sub(r"(?m)^([ \t]*)-(?= )", r"\1*", text)
        text = re.sub(r"\]\(<([^>\s()]*)>\)", r"](\1)", text)
        text = re.sub(
            r"(`+)(.+?)\1",
            lambda match: match.group(1)
            + re.sub(r"[ \t]*(?:\r\n|\r|\n)[ \t]*", " ", match.group(2))
            + match.group(1),
            text,
            flags=re.DOTALL,
        )
        lines = text.splitlines(keepends=True)
        compacted: list[str] = []
        for index, line in enumerate(lines):
            next_line = lines[index + 1] if index + 1 < len(lines) else ""
            if (
                line.strip() == ""
                and next_line
                and re.match(r"^[ \t]*[*+\-][ \t]+", next_line)
                and compacted
                and compacted[-1].strip()
                and not re.match(r"^[ \t]*[*+\-][ \t]+", compacted[-1])
            ):
                continue
            compacted.append(line)
        text = "".join(compacted)
        output.append(text)
        plain.clear()

    for line in content.splitlines(keepends=True):
        fence_match = re.match(r"^[ \t]{0,3}(`{3,}|~{3,})", line)
        if fence is not None:
            output.append(line)
            if fence_match and fence_match.group(1)[0] == fence[0] and len(fence_match.group(1)) >= fence[1]:
                fence = None
                just_closed_fence = True
            continue
        if fence_match:
            flush_plain()
            marker = fence_match.group(1)
            fence = (marker[0], len(marker))
            output.append(line)
            continue
        if just_closed_fence and line.strip() == "":
            just_closed_fence = False
            continue
        just_closed_fence = False
        plain.append(line)
    flush_plain()
    return "".join(output)


def _content_matches(source: str, returned: str) -> bool:
    source_form = _linear_markdown_comparison_form(source)
    returned_form = _linear_markdown_comparison_form(returned)
    if source_form == returned_form:
        return True
    # Linear may remove or add one terminal LF. No other normalization is allowed.
    if source_form.endswith("\n") and source_form[:-1] == returned_form:
        return True
    if returned_form.endswith("\n") and returned_form[:-1] == source_form:
        return True
    return False


def sync_document(
    source: str,
    document_id: str,
    api_key: str,
    graphql_call: Callable[[str, dict[str, Any], str], dict[str, Any]],
    *,
    expected_title: str = "AGENTS.md",
    before_update: Callable[[], None] | None = None,
) -> bool:
    if not api_key.strip():
        raise SyncError("LINEAR_API_KEY is required")
    try:
        parsed_id = uuid.UUID(document_id)
    except (ValueError, AttributeError, TypeError) as exc:
        raise SyncError("LINEAR_DOCUMENT_ID must be a UUID") from exc
    if str(parsed_id) != document_id.lower():
        raise SyncError("LINEAR_DOCUMENT_ID must be a canonical UUID")

    read_query = """
      query ReadDocument($id: String!) {
        document(id: $id) { id title content }
      }
    """
    existing_data = graphql_call(read_query, {"id": document_id}, api_key)
    existing = existing_data.get("document")
    if not isinstance(existing, dict):
        raise SyncError("Linear document does not exist or is not readable")
    if existing.get("id") != document_id:
        raise SyncError("Linear returned a different document ID")
    if existing.get("title") != expected_title:
        raise SyncError("Linear document title does not match expected AGENTS.md")
    existing_content = existing.get("content")
    if not isinstance(existing_content, str):
        raise SyncError("Linear document content is unavailable")
    if _content_matches(source, existing_content):
        return False

    if before_update is not None:
        before_update()

    update_mutation = """
      mutation UpdateDocument($id: String!, $input: DocumentUpdateInput!) {
        documentUpdate(id: $id, input: $input) {
          success
          document { id title }
        }
      }
    """
    update_data = graphql_call(
        update_mutation,
        {"id": document_id, "input": {"content": source}},
        api_key,
    )
    update_payload = update_data.get("documentUpdate")
    updated = update_payload.get("document") if isinstance(update_payload, dict) else None
    if not isinstance(update_payload, dict) or update_payload.get("success") is not True:
        raise SyncError("Linear document update was rejected")
    if not isinstance(updated, dict) or updated.get("id") != document_id:
        raise SyncError("Linear update returned a different document ID")

    readback_data = graphql_call(read_query, {"id": document_id}, api_key)
    readback = readback_data.get("document")
    if not isinstance(readback, dict) or readback.get("id") != document_id:
        raise SyncError("Linear document read-back failed")
    if readback.get("title") != expected_title:
        raise SyncError("Linear document title changed unexpectedly")
    returned_content = readback.get("content")
    if not isinstance(returned_content, str) or not _content_matches(source, returned_content):
        raise SyncError("Linear document read-back content does not match AGENTS.md")
    return True


def main() -> int:
    api_key = os.environ.get("LINEAR_API_KEY", "")
    document_id = os.environ.get("LINEAR_DOCUMENT_ID", "")
    source_sha = os.environ.get("SOURCE_SHA", "")
    github_token = os.environ.get("GITHUB_TOKEN", "")
    source_path = Path(__file__).resolve().parents[1] / "AGENTS.md"
    try:
        if not source_path.is_file():
            raise SyncError("AGENTS.md is missing from the checked-out commit")
        source = source_path.read_text(encoding="utf-8")
        if not document_id:
            raise SyncError("LINEAR_DOCUMENT_ID is required")
        def check_current_main() -> None:
            verify_current_main_sha(source_sha, github_token)

        check_current_main()
        updated = sync_document(
            source, document_id, api_key, graphql_request, before_update=check_current_main
        )
    except (OSError, UnicodeError) as exc:
        raise SyncError("AGENTS.md could not be read as UTF-8") from exc
    except SyncError as exc:
        print(f"AGENTS_LINEAR_SYNC=FAIL REASON={exc}", file=sys.stderr)
        return 1
    print(
        f"AGENTS_LINEAR_SYNC=PASS DOCUMENT_ID={document_id} "
        f"SOURCE_SHA={source_sha or 'UNAVAILABLE'} CONTENT_MATCH=YES "
        f"CONTENT_UPDATED={'YES' if updated else 'NO'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
