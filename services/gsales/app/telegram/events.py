"""Only new updates in explicitly managed private Sales dialogs enter Core."""

from uuid import UUID, uuid4

from app.db.session import GateError
from app.workflow import Workflow, approval_binding, content_hash

OPT_OUT = {"стоп", "не пишите", "не пиши", "stop", "unsubscribe", "/stop", "/optout"}
CONSENT_COMMAND = "/gsales_consent 18+ history memory_off"


class EventRouter:
    def __init__(self, db, core, transport):
        self.db, self.core, self.transport = db, core, transport
        self.w = Workflow(db, core, transport)

    async def handle(self, account_id, peer_user_id, message_id, text, outgoing=False):
        async with self.db.transaction() as c:
            row = await (
                await c.execute(
                    """SELECT l.id AS lead_id,l.current_stage,l.assigned_seller_id AS seller_id,s.language,
                d.ai_mode,d.last_inbound_message_id,d.human_takeover,s.telegram_account_id,s.auto_reply_enabled,
                s.status,s.global_paused,t.telegram_user_id AS account_user_id
                FROM gsales.leads l JOIN gsales.sellers s ON s.id=l.assigned_seller_id AND s.tenant_id=l.tenant_id
                JOIN gsales.dialog_controls d ON d.lead_id=l.id AND d.tenant_id=l.tenant_id
                JOIN gsales.telegram_accounts t ON t.id=s.telegram_account_id AND t.tenant_id=s.tenant_id
                WHERE l.telegram_user_id=%s AND s.telegram_account_id=%s FOR UPDATE OF l,s,d,t""",
                    (peer_user_id, account_id),
                )
            ).fetchone()
            if not row:
                return {"status": "unmanaged_ignored"}
            if outgoing:
                known = await (
                    await c.execute(
                        """SELECT id FROM gsales.outreach_attempts WHERE lead_id=%s
                    AND telegram_account_id=%s AND provider_message_id=%s AND delivery_status='sent' """,
                        (row["lead_id"], account_id, message_id),
                    )
                ).fetchone()
                uncertain = await (
                    await c.execute(
                        "SELECT id FROM gsales.outreach_attempts WHERE lead_id=%s AND delivery_status IN ('sending','unknown')",
                        (row["lead_id"],),
                    )
                ).fetchone()
                if known:
                    return {"status": "ai_echo_ignored"}
                # Unmapped outgoing updates always stop AI, including a lost
                # send acknowledgement. They cannot be treated as new input.
                await c.execute(
                    "UPDATE gsales.dialog_controls SET human_takeover=true,ai_mode='manual',paused_reason=%s WHERE lead_id=%s",
                    ("outgoing_unconfirmed" if uncertain else "human_takeover", row["lead_id"]),
                )
                return {"status": "manual_takeover"}
            if text.strip().casefold() in OPT_OUT:
                await c.execute(
                    "UPDATE gsales.leads SET current_stage='opted_out',contact_eligibility='blocked' WHERE id=%s",
                    (row["lead_id"],),
                )
                await c.execute(
                    "UPDATE gsales.dialog_controls SET ai_mode='opted_out',paused_reason='recipient_opt_out' WHERE lead_id=%s",
                    (row["lead_id"],),
                )
                return {"status": "opted_out"}
            if row["current_stage"] in ("opted_out", "declined"):
                return {"status": "contact_blocked"}
            if text.strip() != CONSENT_COMMAND:
                if row["last_inbound_message_id"] and message_id <= row["last_inbound_message_id"]:
                    return {"status": "duplicate_ignored"}
                if (
                    row["human_takeover"]
                    or row["ai_mode"] != "auto_reply"
                    or row["global_paused"]
                    or row["status"] != "active"
                ):
                    return {"status": "paused"}
                if not self.db.settings.auto_reply_enabled or not row["auto_reply_enabled"]:
                    return {"status": "auto_reply_disabled"}
                await c.execute(
                    "UPDATE gsales.dialog_controls SET pending_inbound_message_id=%s,last_inbound_message_id=%s WHERE lead_id=%s",
                    (message_id, message_id, row["lead_id"]),
                )
        if text.strip() == CONSENT_COMMAND:
            await self.core.request(
                "PUT",
                f"/internal/v1/users/{row['lead_id']}/consent",
                json={
                    "chat_history": True,
                    "long_term_memory": False,
                    "age_band": "adult",
                    "age_status": "self_attested",
                    "age_method": "self_attested",
                    "jurisdiction": "GLOBAL",
                    "disclosure_accepted": True,
                    "disclosure_receipt_id": f"telegram:{peer_user_id}:{message_id}",
                },
            )
            await self.core.request("POST",f"/internal/v1/users/{row['lead_id']}/age-assurance/self-attest",json={"confirmed_18_plus":True})
            await self.w.sync_history(row["lead_id"])
            return {"status": "explicit_consent_received"}
        try:
            response = await self.core.respond(
                row["lead_id"], row["seller_id"], message_id, text, row["language"]
            )
        except GateError:
            async with self.db.transaction() as c:
                await c.execute(
                    "UPDATE gsales.dialog_controls SET pending_inbound_message_id=NULL WHERE lead_id=%s AND pending_inbound_message_id=%s",
                    (row["lead_id"], message_id),
                )
            return {"status": "consent_or_core_gate_closed"}
        if response.get("status") != "completed" or not response.get("text"):
            return {"status": "core_pending"}
        return await self.finalize(row, response, message_id, account_id, peer_user_id)

    async def finalize(self, row, response, message_id, account_id, peer_user_id):
        # The same source id always gives the same protected Core draft.
        request_id = f"reply:{row['lead_id']}:{message_id}"
        draft = await self.core.draft(str(row["seller_id"]), request_id, response["text"], [])
        async with self.db.transaction() as c:
            await c.execute("SELECT id FROM gsales.leads WHERE id=%s FOR UPDATE", (row["lead_id"],))
            current = await (
                await c.execute(
                    "SELECT * FROM gsales.dialog_controls WHERE lead_id=%s FOR UPDATE",
                    (row["lead_id"],),
                )
            ).fetchone()
            if (
                current["pending_inbound_message_id"] != message_id
                or current["human_takeover"]
                or current["ai_mode"] != "auto_reply"
                or (
                    current["last_inbound_message_id"]
                    and current["last_inbound_message_id"] > message_id
                )
            ):
                return {"status": "stale_or_paused"}
            await c.execute(
                "UPDATE gsales.leads SET current_stage='replied',agata_user_id=%s WHERE id=%s",
                (UUID(response["user_id"]), row["lead_id"]),
            )
            await c.execute(
                "UPDATE gsales.dialog_controls SET pending_inbound_message_id=NULL,last_inbound_message_id=%s,agata_relationship_id=%s WHERE lead_id=%s",
                (message_id, UUID(response["relationship_id"]), row["lead_id"]),
            )
            attempt_id = uuid4()
            await c.execute(
                """INSERT INTO gsales.outreach_attempts(id,tenant_id,lead_id,seller_id,telegram_account_id,
                attempt_type,message_reference,content_hash,telegram_random_id,idempotency_key,inbound_message_id)
                VALUES(%s,%s,%s,%s,%s,'reply',%s,%s,%s,%s,%s) ON CONFLICT(tenant_id,idempotency_key) DO NOTHING""",
                (
                    attempt_id,
                    self.db.settings.tenant_id,
                    row["lead_id"],
                    row["seller_id"],
                    account_id,
                    draft["message_reference"],
                    content_hash(response["text"]),
                    int(content_hash(request_id)[:15], 16),
                    request_id,
                    message_id,
                ),
            )
            attempt = await (
                await c.execute(
                    "SELECT id FROM gsales.outreach_attempts WHERE idempotency_key=%s",
                    (request_id,),
                )
            ).fetchone()
            bound = await self.w.locked(c, attempt["id"])
            # Explicit owner-enabled auto_reply is a bounded reply permission,
            # never a permission for a first unsolicited contact.
            await c.execute(
                """UPDATE gsales.outreach_attempts SET approval_status='approved',approved_by=%s,
                approved_at=now(),approved_content_hash=content_hash,approved_recipient_ref=%s,approval_binding_hash=%s WHERE id=%s""",
                (
                    self.db.settings.owner_telegram_id,
                    str(peer_user_id),
                    approval_binding(bound),
                    attempt["id"],
                ),
            )
        return await self.w.send(attempt["id"])

    async def drain_pending(self):
        await self.w.sync_history()
        async with self.db.transaction() as c:
            rows = await (
                await c.execute("""SELECT l.id AS lead_id,l.telegram_user_id,l.telegram_username,s.id AS seller_id,s.language,
                s.telegram_account_id,t.session_secret_ref,t.telegram_user_id AS account_user_id,d.pending_inbound_message_id
                FROM gsales.dialog_controls d JOIN gsales.leads l ON l.id=d.lead_id AND l.tenant_id=d.tenant_id
                JOIN gsales.sellers s ON s.id=d.seller_id AND s.tenant_id=d.tenant_id
                JOIN gsales.telegram_accounts t ON t.id=d.telegram_account_id AND t.tenant_id=d.tenant_id
                WHERE d.pending_inbound_message_id IS NOT NULL AND d.ai_mode='auto_reply' AND NOT d.human_takeover
                AND NOT s.global_paused AND s.status='active' AND s.auto_reply_enabled AND t.is_enabled LIMIT 100""")
            ).fetchall()
        if not self.db.settings.auto_reply_enabled:
            return
        # Recover a crash after the reply/approval commit but before its first
        # claim. Sending/unknown rows are deliberately excluded: never replay I/O.
        async with self.db.transaction() as c:
            ready = await (await c.execute("""SELECT id FROM gsales.outreach_attempts
                WHERE attempt_type='reply' AND delivery_status='draft'
                AND approval_status='approved' ORDER BY created_at LIMIT 100""")).fetchall()
        for attempt in ready:
            try:
                await self.w.send(attempt["id"])
            except GateError:
                # Final serialized guards still own pause/staleness/identity.
                continue
        for row in rows:
            mid = row["pending_inbound_message_id"]
            try:
                response = await self.core.poll_reply(row["lead_id"], row["seller_id"], mid)
            except GateError as exc:
                if str(exc) == "agata_contract_rejected_404":
                    text = await self.transport.read_inbound(row, mid)
                    if not text:
                        await self.w.pause(
                            lead_id=row["lead_id"], reason="inbound_recovery_unavailable"
                        )
                        continue
                    response = await self.core.respond(
                        row["lead_id"], row["seller_id"], mid, text, row["language"]
                    )
                else:
                    continue
            if response.get("status") == "completed" and response.get("text"):
                await self.finalize(
                    row, response, mid, row["telegram_account_id"], row["telegram_user_id"]
                )
