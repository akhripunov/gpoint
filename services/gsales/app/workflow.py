"""CRM approval and delivery state; message text is owned exclusively by Core."""

import asyncio
import hashlib
import json
from uuid import UUID, uuid4

from telethon.errors import FloodWaitError

from app.db.session import Database, GateError


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def approval_binding(row) -> str:
    keys = (
        "telegram_user_id",
        "telegram_username",
        "seller_id",
        "telegram_account_id",
        "message_reference",
        "content_hash",
        "account_user_id",
        "session_secret_ref",
        "agata_character_id",
    )
    return content_hash(
        json.dumps({k: str(row[k]) for k in keys}, sort_keys=True, separators=(",", ":"))
    )


class Workflow:
    def __init__(self, db: Database, core, transport):
        self.db, self.core, self.transport = db, core, transport
        self.settings = db.settings

    async def locked(self, conn, attempt_id):
        row = await (
            await conn.execute(
                """
            SELECT a.*, l.telegram_user_id,l.telegram_username,l.assigned_seller_id,
                   l.contact_eligibility,l.contact_eligibility_checked_at,l.eligibility_evidence,l.current_stage,
                   s.agata_character_id,s.status AS seller_status,s.global_paused,s.auto_reply_enabled,
                   s.telegram_account_id AS seller_account_id,
                   t.connection_status,t.is_enabled,t.session_secret_ref,t.telegram_user_id AS account_user_id,
                   d.ai_mode,d.human_takeover,d.last_inbound_message_id,d.auto_reply_count
            FROM gsales.outreach_attempts a
            JOIN gsales.leads l ON l.id=a.lead_id AND l.tenant_id=a.tenant_id
            JOIN gsales.sellers s ON s.id=a.seller_id AND s.tenant_id=a.tenant_id
            JOIN gsales.telegram_accounts t ON t.id=a.telegram_account_id AND t.tenant_id=a.tenant_id
            JOIN gsales.dialog_controls d ON d.lead_id=l.id AND d.tenant_id=l.tenant_id
            WHERE a.id=%s FOR UPDATE OF a,l,s,t,d
        """,
                (attempt_id,),
            )
        ).fetchone()
        if row is None:
            raise GateError("attempt_not_found")
        return row

    async def draft(
        self,
        lead_id: UUID,
        request_id: str,
        text: str | None = None,
        verified_facts=None,
        inbound_message_id: int | None = None,
    ):
        # Caller supplies an idempotency key. Core verifies exact replay content.
        async with self.db.transaction() as conn:
            await self.db.assert_secure(conn)
            lead = await (
                await conn.execute(
                    """SELECT l.*,s.agata_character_id,s.telegram_account_id
                FROM gsales.leads l JOIN gsales.sellers s ON s.id=l.assigned_seller_id AND s.tenant_id=l.tenant_id
                WHERE l.id=%s FOR UPDATE OF l,s""",
                    (lead_id,),
                )
            ).fetchone()
            if not lead or not lead["telegram_account_id"]:
                raise GateError("lead_or_account_missing")
            if lead["current_stage"] in ("opted_out", "declined"):
                raise GateError("contact_blocked")
            draft = await self.core.draft(
                str(lead["assigned_seller_id"]), request_id, text, verified_facts or []
            )
            if content_hash(draft["text"]) != draft["content_hash"]:
                raise GateError("core_content_hash_mismatch")
            attempt_id = uuid4()
            random_id = int(content_hash(request_id)[:15], 16) or 1
            attempt_type = "reply" if inbound_message_id is not None else "first"
            row = await (
                await conn.execute(
                    """INSERT INTO gsales.outreach_attempts
                (id,tenant_id,lead_id,seller_id,telegram_account_id,attempt_type,message_reference,
                 content_hash,telegram_random_id,idempotency_key,inbound_message_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(tenant_id,idempotency_key) DO NOTHING RETURNING *""",
                    (
                        attempt_id,
                        self.settings.tenant_id,
                        lead_id,
                        lead["assigned_seller_id"],
                        lead["telegram_account_id"],
                        attempt_type,
                        draft["message_reference"],
                        draft["content_hash"],
                        random_id,
                        request_id,
                        inbound_message_id,
                    ),
                )
            ).fetchone()
            if not row:
                row = await (
                    await conn.execute(
                        "SELECT * FROM gsales.outreach_attempts WHERE idempotency_key=%s",
                        (request_id,),
                    )
                ).fetchone()
                if (
                    str(row["lead_id"]) != str(lead_id)
                    or row["content_hash"] != draft["content_hash"]
                    or row["message_reference"] != draft["message_reference"]
                ):
                    raise GateError("idempotency_conflict")
            await conn.execute(
                """INSERT INTO gsales.dialog_controls(tenant_id,lead_id,seller_id,telegram_account_id,ai_mode)
                VALUES(%s,%s,%s,%s,'awaiting_approval') ON CONFLICT(tenant_id,lead_id) DO NOTHING""",
                (
                    self.settings.tenant_id,
                    lead_id,
                    lead["assigned_seller_id"],
                    lead["telegram_account_id"],
                ),
            )
            return row

    async def approve(self, attempt_id: UUID, expected_binding: str, owner_id: int):
        if owner_id != self.settings.owner_telegram_id or not owner_id:
            raise GateError("owner_required")
        async with self.db.transaction() as conn:
            row = await self.locked(conn, attempt_id)
            if row["delivery_status"] != "draft" or row["approval_status"] == "rejected":
                raise GateError("approval_state_invalid")
            binding = approval_binding(row)
            if expected_binding != binding:
                raise GateError("approval_stale")
            await conn.execute(
                """UPDATE gsales.outreach_attempts SET approval_status='approved',approved_by=%s,
                approved_at=now(),approved_content_hash=content_hash,approved_recipient_ref=%s,
                approval_binding_hash=%s WHERE id=%s""",
                (owner_id, str(row["telegram_user_id"]), binding, attempt_id),
            )
            return binding

    def gate(self, row):
        if not self.settings.outreach_enabled:
            raise GateError("outreach_disabled")
        if row["global_paused"] or row["seller_status"] != "active":
            raise GateError("seller_or_global_paused")
        if row["human_takeover"] or row["ai_mode"] in ("disabled", "manual", "paused", "opted_out"):
            raise GateError("dialog_paused")
        if (
            row["current_stage"] in ("declined", "opted_out")
            or row["contact_eligibility"] != "allowed"
            or not row["contact_eligibility_checked_at"]
            or not row["eligibility_evidence"]
        ):
            raise GateError("eligibility_missing")
        if (
            str(row["assigned_seller_id"]) != str(row["seller_id"])
            or row["seller_account_id"] != row["telegram_account_id"]
        ):
            raise GateError("assignment_changed")
        if row["connection_status"] != "authorized" or not row["is_enabled"]:
            raise GateError("account_disabled")
        if row["approval_status"] != "approved" or row["approval_binding_hash"] != approval_binding(
            row
        ):
            raise GateError("approval_missing_or_stale")
        if row["attempt_type"] == "reply" and (
            not self.settings.auto_reply_enabled
            or not row["auto_reply_enabled"]
            or row["ai_mode"] != "auto_reply"
            or row["inbound_message_id"] != row["last_inbound_message_id"]
            or row["auto_reply_count"] >= self.settings.max_auto_replies
        ):
            raise GateError("auto_reply_gate_closed")

    async def send(self, attempt_id: UUID):
        # Persist the claim before I/O: process death cannot reopen a draft.
        async with self.db.transaction() as conn:
            await self.db.assert_secure(conn)
            row = await self.locked(conn, attempt_id)
            self.gate(row)
            if row["delivery_status"] != "draft":
                raise GateError("delivery_requires_reconciliation")
            await self.core.assert_secure()
            draft = await self.core.read_draft(row["message_reference"], str(row["seller_id"]))
            if content_hash(draft["text"]) != row["approved_content_hash"]:
                raise GateError("approved_content_changed")
            await conn.execute(
                "UPDATE gsales.outreach_attempts SET delivery_status='sending',attempted_at=now() WHERE id=%s",
                (attempt_id,),
            )
        # Recheck under all row locks immediately before transport. Pause/opt-out
        # writers acquire the same rows, defining a serialized send boundary.
        async with self.db.transaction() as conn:
            row = await self.locked(conn, attempt_id)
            try:
                self.gate(row)
                if row["delivery_status"] != "sending":
                    raise GateError("delivery_state_changed")
                provider_id = await asyncio.wait_for(
                    self.transport.send(row, draft["text"]), self.settings.send_timeout_seconds
                )
            except GateError as exc:
                await conn.execute(
                    "UPDATE gsales.outreach_attempts SET delivery_status='blocked',error_code=%s WHERE id=%s",
                    (str(exc), attempt_id),
                )
                return {"status": "blocked"}
            except FloodWaitError as exc:
                await conn.execute(
                    "UPDATE gsales.outreach_attempts SET delivery_status='unknown',error_code='telegram_flood_wait',retry_after=now()+(%s * interval '1 second') WHERE id=%s",
                    (exc.seconds, attempt_id),
                )
                await conn.execute(
                    "UPDATE gsales.telegram_accounts SET is_enabled=false,last_error_code='telegram_flood_wait' WHERE id=%s",
                    (row["telegram_account_id"],),
                )
                return {"status": "unknown", "retry_after_seconds": exc.seconds}
            except Exception:
                # Includes transport and network failure. Never retry an uncertain
                # dispatch. Transport exceptions do not enter logs/CRM verbatim.
                await conn.execute(
                    "UPDATE gsales.outreach_attempts SET delivery_status='unknown',error_code='transport_unconfirmed' WHERE id=%s",
                    (attempt_id,),
                )
                return {"status": "unknown"}
            await conn.execute(
                "UPDATE gsales.outreach_attempts SET delivery_status='sent',provider_message_id=%s,sent_at=now() WHERE id=%s",
                (provider_id, attempt_id),
            )
            await conn.execute(
                "UPDATE gsales.leads SET current_stage='contacted' WHERE id=%s AND current_stage IN ('discovered','reviewed','approved')",
                (row["lead_id"],),
            )
            await conn.execute(
                """UPDATE gsales.dialog_controls SET last_outbound_message_id=%s,
                auto_reply_count=auto_reply_count+%s,last_auto_reply_at=CASE WHEN %s THEN now() ELSE last_auto_reply_at END WHERE lead_id=%s""",
                (
                    provider_id,
                    int(row["attempt_type"] == "reply"),
                    row["attempt_type"] == "reply",
                    row["lead_id"],
                ),
            )
        await self.sync_history(row["lead_id"])
        return {"status": "sent", "provider_message_id": provider_id}

    async def pause(self, seller_id=None, lead_id=None, reason="owner_pause"):
        async with self.db.transaction() as conn:
            if lead_id:
                await conn.execute("SELECT id FROM gsales.leads WHERE id=%s FOR UPDATE", (lead_id,))
                await conn.execute(
                    "UPDATE gsales.dialog_controls SET ai_mode='paused',paused_reason=%s WHERE lead_id=%s",
                    (reason, lead_id),
                )
            elif seller_id:
                await conn.execute(
                    "UPDATE gsales.sellers SET status='paused' WHERE id=%s", (seller_id,)
                )
            else:
                await conn.execute("UPDATE gsales.sellers SET global_paused=true")

    async def opt_out(self, lead_id):
        async with self.db.transaction() as conn:
            await conn.execute(
                "UPDATE gsales.leads SET current_stage='opted_out',contact_eligibility='blocked' WHERE id=%s",
                (lead_id,),
            )
            await conn.execute(
                "UPDATE gsales.dialog_controls SET ai_mode='opted_out',paused_reason='recipient_opt_out' WHERE lead_id=%s",
                (lead_id,),
            )

    async def takeover(self, lead_id):
        async with self.db.transaction() as conn:
            await conn.execute("SELECT id FROM gsales.leads WHERE id=%s FOR UPDATE", (lead_id,))
            await conn.execute(
                "UPDATE gsales.dialog_controls SET human_takeover=true,ai_mode='manual',paused_reason='human_takeover' WHERE lead_id=%s",
                (lead_id,),
            )

    async def reconciliation_evidence(self, attempt_id):
        async with self.db.transaction() as conn:
            row = await self.locked(conn, attempt_id)
            if row["delivery_status"] not in ("sending", "unknown"):
                raise GateError("reconciliation_not_required")
            await conn.execute(
                "UPDATE gsales.outreach_attempts SET delivery_status='unknown' WHERE id=%s",
                (attempt_id,),
            )
            # A same-text manual message can be indistinguishable from a lost
            # RPC result. Report a candidate; never infer success or re-dispatch.
            candidate = await self.transport.reconcile(row)
            return {
                "status": "unknown",
                "candidate_message_id": candidate,
                "owner_confirmation_required": True,
            }

    async def confirm_reconciliation(self, attempt_id, provider_id, owner_id):
        if owner_id != self.settings.owner_telegram_id or not owner_id:
            raise GateError("owner_required")
        async with self.db.transaction() as conn:
            row = await self.locked(conn, attempt_id)
            if row["delivery_status"] not in ("sending", "unknown"):
                raise GateError("reconciliation_not_required")
            candidate = await self.transport.reconcile(row)
            if candidate != provider_id or not candidate:
                raise GateError("reconciliation_evidence_missing")
            await conn.execute(
                "UPDATE gsales.outreach_attempts SET delivery_status='sent',provider_message_id=%s,sent_at=now(),error_code='owner_reconciled' WHERE id=%s",
                (provider_id, attempt_id),
            )
            await conn.execute(
                "UPDATE gsales.dialog_controls SET last_outbound_message_id=%s WHERE lead_id=%s",
                (provider_id, row["lead_id"]),
            )
            return {"status": "sent", "provider_message_id": provider_id, "owner_reconciled": True}

    async def sync_history(self, lead_id=None):
        async with self.db.transaction() as c:
            rows = await (
                await c.execute(
                    "SELECT * FROM gsales.outreach_attempts WHERE delivery_status='sent' AND history_confirmed_at IS NULL AND (%s::uuid IS NULL OR lead_id=%s) ORDER BY sent_at LIMIT 50",
                    (lead_id, lead_id),
                )
            ).fetchall()
        for row in rows:
            try:
                result = await self.core.record_delivery(row)
                if result.get("status") != "confirmed":
                    continue
                async with self.db.transaction() as c:
                    await c.execute(
                        "UPDATE gsales.outreach_attempts SET history_confirmed_at=now() WHERE id=%s AND delivery_status='sent'",
                        (row["id"],),
                    )
            except Exception:
                # Telegram is already confirmed; history sync failure never
                # changes delivery to unknown or reopens the send claim.
                continue
