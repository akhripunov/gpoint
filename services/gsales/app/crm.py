from uuid import UUID, uuid4

from fastapi import Depends, HTTPException
from psycopg.errors import UniqueViolation
from pydantic import BaseModel, ConfigDict, Field

from app.core import AgataClient
from app.db.session import GateError
from app.workflow import Workflow, approval_binding


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SellerInput(Input):
    display_name: str = Field(min_length=1, max_length=100)
    personality: dict
    language: str = Field(default="ru", min_length=2, max_length=10)
    telegram_account_id: UUID | None = None


class LeadInput(Input):
    seller_id: UUID
    telegram_user_id: int = Field(gt=0)
    telegram_username: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
    display_name: str = Field(min_length=1, max_length=100)
    source_type: str = Field(pattern=r"^(manual|public_profile|opt_in)$")
    source_reference: str = Field(min_length=1, max_length=500)


class DraftInput(Input):
    request_id: str = Field(min_length=1, max_length=100)
    text: str | None = Field(default=None, min_length=1, max_length=4000)
    verified_facts: list[dict] = Field(default_factory=list, max_length=12)


class ApprovalInput(Input):
    expected_binding: str = Field(pattern=r"^[a-f0-9]{64}$")


class EligibilityInput(Input):
    allowed: bool
    evidence: str = Field(min_length=1, max_length=1000)


class ReconciliationInput(Input):
    provider_message_id: int = Field(gt=0)


class ResumeInput(Input):
    scope: str = Field(pattern=r"^(global|seller|dialog)$")
    target_id: UUID | None = None
    auto_reply: bool = False


def install(app, require_owner):
    auth = [Depends(require_owner)]
    core = AgataClient(app.state.settings)
    app.state.core = core

    # A configured transport is installed explicitly; startup never logs in.
    def workflow():
        transport = getattr(app.state, "telegram", None)
        if transport is None:

            class Disabled:
                async def send(self, *args):
                    raise GateError("telegram_not_configured")

            transport = Disabled()
        return Workflow(app.state.db, app.state.core, transport)

    @app.exception_handler(GateError)
    async def gate_error(request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=409, content={"detail": {"code": str(exc)}})

    @app.exception_handler(UniqueViolation)
    async def duplicate(request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=409, content={"detail": {"code": "duplicate_or_inflight"}})

    @app.post("/v1/sellers", dependencies=auth)
    async def create_seller(payload: SellerInput):
        seller_id = uuid4()
        profile = await app.state.core.sync_seller(
            str(seller_id), payload.display_name, payload.personality
        )
        async with app.state.db.transaction() as c:
            await app.state.db.assert_secure(c)
            row = await (
                await c.execute(
                    """INSERT INTO gsales.sellers
                (id,tenant_id,agata_character_id,telegram_account_id,display_name,language)
                VALUES(%s,%s,%s,%s,%s,%s) RETURNING *""",
                    (
                        seller_id,
                        app.state.settings.tenant_id,
                        UUID(profile["character_id"]),
                        payload.telegram_account_id,
                        payload.display_name,
                        payload.language,
                    ),
                )
            ).fetchone()
            return row

    @app.get("/v1/sellers", dependencies=auth)
    async def sellers():
        async with app.state.db.transaction() as c:
            return await (
                await c.execute("SELECT * FROM gsales.sellers ORDER BY created_at")
            ).fetchall()

    @app.post("/v1/leads", dependencies=auth)
    async def create_lead(payload: LeadInput):
        async with app.state.db.transaction() as c:
            row = await (
                await c.execute(
                    """INSERT INTO gsales.leads
                (tenant_id,assigned_seller_id,telegram_user_id,telegram_username,display_name)
                VALUES(%s,%s,%s,%s,%s) ON CONFLICT(tenant_id,telegram_user_id) DO NOTHING RETURNING *""",
                    (
                        app.state.settings.tenant_id,
                        payload.seller_id,
                        payload.telegram_user_id,
                        payload.telegram_username,
                        payload.display_name,
                    ),
                )
            ).fetchone()
            if not row:
                row = await (
                    await c.execute(
                        "SELECT * FROM gsales.leads WHERE telegram_user_id=%s",
                        (payload.telegram_user_id,),
                    )
                ).fetchone()
            await c.execute(
                """INSERT INTO gsales.lead_sources(tenant_id,lead_id,source_type,source_reference)
                VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (
                    app.state.settings.tenant_id,
                    row["id"],
                    payload.source_type,
                    payload.source_reference,
                ),
            )
            return row

    @app.get("/v1/leads", dependencies=auth)
    async def leads():
        async with app.state.db.transaction() as c:
            return await (
                await c.execute("SELECT * FROM gsales.leads ORDER BY created_at LIMIT 500")
            ).fetchall()

    @app.put("/v1/leads/{lead_id}/eligibility", dependencies=auth)
    async def eligibility(lead_id: UUID, payload: EligibilityInput):
        async with app.state.db.transaction() as c:
            row = await (
                await c.execute(
                    """UPDATE gsales.leads SET contact_eligibility=%s,
                contact_eligibility_checked_at=now(),eligibility_evidence=%s WHERE id=%s AND current_stage NOT IN ('declined','opted_out') RETURNING id""",
                    ("allowed" if payload.allowed else "blocked", payload.evidence, lead_id),
                )
            ).fetchone()
            if not row:
                raise HTTPException(404, detail={"code": "lead_not_eligible"})
            return {"id": row["id"]}

    @app.post("/v1/leads/{lead_id}/verify-registration", dependencies=auth)
    async def verify_registration(lead_id: UUID):
        from app.gpoint import GPointClient

        async with app.state.db.transaction() as c:
            row = await (
                await c.execute(
                    "SELECT telegram_user_id FROM gsales.leads WHERE id=%s FOR UPDATE", (lead_id,)
                )
            ).fetchone()
            if not row:
                raise GateError("lead_not_found")
            result = await GPointClient(app.state.settings).registration(row["telegram_user_id"])
            if not result:
                return {"registered": False}
            await c.execute(
                "UPDATE gsales.leads SET current_stage='registered',gpoint_creator_external_id=%s WHERE id=%s AND current_stage NOT IN ('declined','opted_out')",
                (result.creator_id, lead_id),
            )
            return {"registered": True, "creator_id": result.creator_id}

    @app.post("/v1/leads/{lead_id}/drafts", dependencies=auth)
    async def draft(lead_id: UUID, payload: DraftInput):
        return await workflow().draft(
            lead_id, payload.request_id, payload.text, payload.verified_facts
        )

    @app.get("/v1/pending", dependencies=auth)
    async def pending():
        async with app.state.db.transaction() as c:
            ids = await (
                await c.execute(
                    "SELECT id FROM gsales.outreach_attempts WHERE delivery_status='draft' AND approval_status='pending' ORDER BY created_at LIMIT 100"
                )
            ).fetchall()
            rows = []
            for item in ids:
                row = await workflow().locked(c, item["id"])
                draft = await app.state.core.read_draft(
                    row["message_reference"], str(row["seller_id"])
                )
                rows.append(
                    {
                        "id": row["id"],
                        "lead_id": row["lead_id"],
                        "seller_id": row["seller_id"],
                        "telegram_account_id": row["telegram_account_id"],
                        "telegram_user_id": row["telegram_user_id"],
                        "telegram_username": row["telegram_username"],
                        "text": draft["text"],
                        "approval_binding": approval_binding(row),
                    }
                )
            return rows

    @app.post("/v1/attempts/{attempt_id}/approve", dependencies=auth)
    async def approve(attempt_id: UUID, payload: ApprovalInput):
        binding = await workflow().approve(
            attempt_id, payload.expected_binding, app.state.settings.owner_telegram_id
        )
        return {"approval_binding": binding}

    @app.post("/v1/attempts/{attempt_id}/reject", dependencies=auth)
    async def reject(attempt_id: UUID):
        async with app.state.db.transaction() as c:
            row = await workflow().locked(c, attempt_id)
            if row["delivery_status"] != "draft":
                raise GateError("delivery_requires_reconciliation")
            await c.execute(
                "UPDATE gsales.outreach_attempts SET approval_status='rejected' WHERE id=%s",
                (attempt_id,),
            )
        return {"status": "rejected"}

    @app.post("/v1/attempts/{attempt_id}/send", dependencies=auth)
    async def send(attempt_id: UUID):
        return await workflow().send(attempt_id)

    @app.post("/v1/attempts/{attempt_id}/reconcile", dependencies=auth)
    async def reconcile(attempt_id: UUID):
        return await workflow().reconciliation_evidence(attempt_id)

    @app.post("/v1/attempts/{attempt_id}/confirm-reconciliation", dependencies=auth)
    async def confirm_reconciliation(attempt_id: UUID, payload: ReconciliationInput):
        return await workflow().confirm_reconciliation(
            attempt_id, payload.provider_message_id, app.state.settings.owner_telegram_id
        )

    @app.post("/v1/pause", dependencies=auth)
    async def pause(seller_id: UUID | None = None, lead_id: UUID | None = None):
        await workflow().pause(seller_id, lead_id)
        return {"status": "paused"}

    @app.post("/v1/resume", dependencies=auth)
    async def resume(payload: ResumeInput):
        async with app.state.db.transaction() as c:
            await app.state.db.assert_secure(c)
            await app.state.core.assert_secure()
            if payload.scope == "global":
                await c.execute("UPDATE gsales.sellers SET global_paused=false")
            elif payload.scope == "seller" and payload.target_id:
                await c.execute("SELECT pg_advisory_xact_lock(723801)")
                n = await (
                    await c.execute(
                        "SELECT count(*) AS n FROM gsales.sellers WHERE status='active' AND id<>%s",
                        (payload.target_id,),
                    )
                ).fetchone()
                if n["n"]:
                    raise GateError("mvp_one_active_seller")
                await c.execute(
                    "UPDATE gsales.sellers SET status='active',auto_reply_enabled=%s WHERE id=%s",
                    (payload.auto_reply, payload.target_id),
                )
            elif payload.scope == "dialog" and payload.target_id:
                await c.execute(
                    "SELECT id FROM gsales.leads WHERE id=%s FOR UPDATE", (payload.target_id,)
                )
                row = await (
                    await c.execute(
                        "UPDATE gsales.dialog_controls SET ai_mode=%s,human_takeover=false,paused_reason=NULL WHERE lead_id=%s AND ai_mode<>'opted_out' RETURNING id",
                        (
                            "auto_reply" if payload.auto_reply else "awaiting_approval",
                            payload.target_id,
                        ),
                    )
                ).fetchone()
                if not row:
                    raise GateError("dialog_not_resumable")
            else:
                raise GateError("resume_target_required")
        return {"status": "resumed"}

    @app.post("/v1/leads/{lead_id}/takeover", dependencies=auth)
    async def takeover(lead_id: UUID):
        await workflow().takeover(lead_id)
        return {"status": "manual"}

    @app.post("/v1/leads/{lead_id}/opt-out", dependencies=auth)
    async def optout(lead_id: UUID):
        await workflow().opt_out(lead_id)
        return {"status": "opted_out"}
