"""Bounded memory-task adapter over the authenticated GPOINT Mac Worker queue."""

from __future__ import annotations

import json
from collections.abc import Callable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.memory.shadow import (
    EXTRACTION_RESPONSE_SCHEMA,
    ExtractionRequest,
    ProviderResult,
    parse_structured_extraction_result,
)
from app.messages.crypto import AeadCrypto
from app.responses.mac_worker import MacWorkerResponseProvider
from app.responses.pipeline import ProviderEnvelope, ResponseContext


class MacWorkerMemoryTaskClient:
    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        crypto: AeadCrypto,
        tenant_id: UUID,
        model: str,
        wait_seconds: float,
    ) -> None:
        self.session_factory = session_factory
        self.crypto = crypto
        self.tenant_id = tenant_id
        self.model = model
        # Memory jobs retain their parent lease while waiting. The configured
        # Mac Worker inference timeout is at most 30 seconds; allow a small
        # queue/polling margin without outliving the 60-second parent lease.
        self.wait_seconds = min(max(wait_seconds, 35.0), 50.0)

    async def complete(
        self, context: ResponseContext, *, purpose: str, parent_job_id: UUID
    ) -> ProviderEnvelope:
        async with self.session_factory() as session:
            provider = MacWorkerResponseProvider(
                session,
                crypto=self.crypto,
                tenant_id=self.tenant_id,
                model=self.model,
                wait_seconds=self.wait_seconds,
                task_purpose=purpose,
                parent_job_id=parent_job_id,
            )
            return await provider.complete(context)


class MacWorkerExtractionProvider:
    provider_name = "mac_worker"

    def __init__(self, client: MacWorkerMemoryTaskClient) -> None:
        self.client = client
        self.model_name = client.model

    async def extract(self, request: ExtractionRequest) -> ProviderResult:
        if not isinstance(request, ExtractionRequest) or request.parent_job_id is None:
            raise ValueError("extractor_request_invalid")
        prepared = {
            "scope": request.scope,
            "schema_version": request.schema_version,
            "prompt_version": request.prompt_version,
            "schema": EXTRACTION_RESPONSE_SCHEMA,
            "text": request.text,
        }
        context = ResponseContext(
            profile_version=0,
            route_tuple=("", "", "", "", ""),
            current_message=json.dumps(prepared, ensure_ascii=False, separators=(",", ":")),
            response_language="ru",
            personality={"name": "memory_extractor"},
            source_message_id=request.source_message_id,
            source_idempotency_key=f"memory-extract:{request.parent_job_id}",
        )
        envelope = await self.client.complete(
            context, purpose="memory_extract", parent_job_id=request.parent_job_id
        )
        return parse_structured_extraction_result(envelope.text)


class MacWorkerSummaryProvider:
    provider = "mac_worker"

    def __init__(self, client: MacWorkerMemoryTaskClient) -> None:
        self.client = client
        self.model = client.model

    async def complete_for_job(
        self, context: ResponseContext, *, parent_job_id: UUID
    ) -> ProviderEnvelope:
        return await self.client.complete(
            context, purpose="conversation_summary", parent_job_id=parent_job_id
        )
