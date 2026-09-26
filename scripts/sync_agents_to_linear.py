#!/usr/bin/env python3
"""Mirror this checkout's AGENTS.md into one pre-existing Linear document."""

from __future__ import annotations

import json
import os
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
                retryable = any(
                    isinstance(error, dict)
                    and isinstance(error.get("extensions"), dict)
                    and error["extensions"].get("code") == "RATELIMITED"
                    for error in errors
                )
                if retryable and attempt < MAX_ATTEMPTS:
                    sleep(_retry_delay(attempt))
                    continue
                raise SyncError("Linear GraphQL request failed")
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
        except urllib.error.URLError:
            if attempt < MAX_ATTEMPTS:
                sleep(_retry_delay(attempt))
                continue
            raise SyncError(f"Linear API unavailable after {MAX_ATTEMPTS} attempts (network)") from None
    raise SyncError("Linear API request exhausted retries")


def _content_matches(source: str, returned: str) -> bool:
    if source == returned:
        return True
    # Linear may remove or add one terminal LF. No other normalization is allowed.
    if source.endswith("\n") and source[:-1] == returned:
        return True
    if returned.endswith("\n") and returned[:-1] == source:
        return True
    return False


def sync_document(
    source: str,
    document_id: str,
    api_key: str,
    graphql_call: Callable[[str, dict[str, Any], str], dict[str, Any]],
    *,
    expected_title: str = "AGENTS.md",
) -> None:
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


def main() -> int:
    api_key = os.environ.get("LINEAR_API_KEY", "")
    document_id = os.environ.get("LINEAR_DOCUMENT_ID", "")
    source_sha = os.environ.get("SOURCE_SHA", "")
    source_path = Path(__file__).resolve().parents[1] / "AGENTS.md"
    try:
        if not source_path.is_file():
            raise SyncError("AGENTS.md is missing from the checked-out commit")
        source = source_path.read_text(encoding="utf-8")
        if not document_id:
            raise SyncError("LINEAR_DOCUMENT_ID is required")
        sync_document(source, document_id, api_key, graphql_request)
    except (OSError, UnicodeError) as exc:
        raise SyncError("AGENTS.md could not be read as UTF-8") from exc
    except SyncError as exc:
        print(f"AGENTS_LINEAR_SYNC=FAIL REASON={exc}", file=sys.stderr)
        return 1
    print(
        f"AGENTS_LINEAR_SYNC=PASS DOCUMENT_ID={document_id} "
        f"SOURCE_SHA={source_sha or 'UNAVAILABLE'} CONTENT_MATCH=YES"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
