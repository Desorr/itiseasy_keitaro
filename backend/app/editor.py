from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .config import Settings
from .database import Campaign, Offer, Stream, StreamOffer
from .keitaro import KeitaroClient, KeitaroError
from .schemas import CampaignRead, DraftInput, StreamRead
from .services import CampaignService


def reject(code: str, message: str, status: int = 422):
    raise KeitaroError(code, message, http_status=status)


def remote_snapshot(raw: dict[str, Any]) -> dict[str, Any]:
    # Compare writable settings, not changing timestamps or generated binding IDs.
    result = {key: value for key, value in raw.items()
              if key not in {"updated_at", "created_at", "offers"}}
    result["offers"] = remote_offers(raw.get("offers") or [])
    return result


def remote_offers(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(({
        "offer_id": int(item["offer_id"]), "share": int(item["share"]),
        "state": item.get("state", "active"),
    } for item in items), key=lambda item: item["offer_id"])


def outgoing(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"offer_id": row["offer_id"], "share": row["share"], "state": row["state"]}
            for row in rows if not row["removed"]]


def distribute(rows: list[dict[str, Any]]) -> None:
    active = [row for row in rows if not row["removed"] and row["state"] == "active"]
    if not active:
        reject("last_offer", "The flow must have at least one active offer.")
    pinned = [row for row in active if row["pinned"]]
    free = [row for row in active if not row["pinned"]]
    remainder = 100 - sum(row["share"] for row in pinned)
    if remainder < 0 or (not free and remainder != 0) or (free and remainder < len(free)):
        reject("pinned_shares", "Pinned shares prevent allocation. Unpin one of the offers.")
    if free:
        share, extra = divmod(remainder, len(free))
        for index, row in enumerate(free):
            row["share"] = share + (index < extra)
    for row in rows:
        if row["removed"] or row["state"] != "active":
            row["share"] = 0
            row["pinned"] = False


class EditorService:
    def __init__(self, client: KeitaroClient, settings: Settings, sessions: async_sessionmaker[AsyncSession]):
        self.client, self.settings, self.sessions = client, settings, sessions
        self.campaigns = CampaignService(client, settings)

    async def _remote_campaign(self, campaign_id: int):
        self.campaigns._require_key()
        raw = await self.client.get_object(f"campaigns/{campaign_id}")
        if not self.campaigns._managed(raw) or raw.get("state") == "deleted":
            reject("campaign_not_found", "Campaign not found among the application campaigns.", 404)
        return raw

    async def _lock_campaign(self, db: AsyncSession, campaign_id: int, *, allow_deleted: bool = False) -> Campaign:
        campaign = await db.scalar(select(Campaign).where(Campaign.id == campaign_id).with_for_update())
        if campaign is None or (campaign.state == "deleted" and not allow_deleted) or not self.campaigns._managed(campaign.data):
            reject("campaign_not_found", "Campaign not found among the application campaigns.", 404)
        return campaign

    async def _lock_stream(self, db: AsyncSession, campaign_id: int, stream_id: int) -> Stream:
        await self._lock_campaign(db, campaign_id)
        stream = await db.scalar(select(Stream).where(
            Stream.id == stream_id, Stream.campaign_id == campaign_id).with_for_update())
        if stream is None:
            reject("stream_not_found", "Flow not found in the selected campaign.", 404)
        return stream

    @staticmethod
    def _revision(stream: Stream, expected: int):
        if stream.revision != expected:
            reject("revision_conflict", "The flow changed. Refresh the editor before continuing.", 409)
        if stream.publication != "idle":
            reject("publication_pending", "Publication is being verified. Fetch flows from Keitaro.", 409)

    async def _rows(self, db: AsyncSession, stream: Stream):
        return [dict(offer_id=row.offer_id, share=row.share, state=row.state,
                     pinned=row.pinned, removed=row.removed)
                for row in (await db.scalars(select(StreamOffer).where(
                    StreamOffer.stream_id == stream.id).order_by(StreamOffer.position, StreamOffer.id))).all()]

    async def _save_rows(self, db: AsyncSession, stream: Stream, rows: list[dict[str, Any]]):
        existing = {row.offer_id: row for row in (await db.scalars(
            select(StreamOffer).where(StreamOffer.stream_id == stream.id))).all()}
        for index, row in enumerate(rows):
            record = existing.get(row["offer_id"])
            if record is None:
                record = StreamOffer(stream_id=stream.id, offer_id=row["offer_id"])
                db.add(record)
            record.position = index
            for key in ("share", "state", "pinned", "removed"):
                setattr(record, key, row[key])
        await db.flush()

    async def _remember_offer(self, db: AsyncSession, offer_id: int, raw: dict[str, Any] | None = None):
        known = await db.get(Offer, offer_id)
        if raw is None and known is not None:
            return
        if raw is None:
            try:
                raw = await self.client.get_object(f"offers/{offer_id}")
            except KeitaroError as error:
                if error.code != "keitaro_not_found":
                    raise
                raw = {"name": f"Offer {offer_id} (unavailable)"}
        await db.execute(insert(Offer).values(id=offer_id, name=str(raw.get("name") or f"Offer {offer_id}")[:1024])
                         .on_conflict_do_update(index_elements=[Offer.id], set_={"name": str(raw.get("name") or f"Offer {offer_id}")[:1024]}))

    def _settled(self, stream: Stream) -> bool:
        return stream.publishing_at is None or (datetime.now(timezone.utc) - stream.publishing_at).total_seconds() > (
            self.settings.keitaro_timeout_seconds * 2 + 10)

    def _mark_missing(self, stream: Stream):
        changed = not stream.missing
        stream.missing = True
        stream.conflict = stream.draft is not None
        if stream.publication != "idle" and self._settled(stream):
            # The destination was archived externally; retain the draft but allow its cancellation.
            stream.publication = "idle"
            stream.pending = None
            stream.publishing_at = None
            changed = True
        if changed:
            stream.revision += 1

    async def _ingest(self, db: AsyncSession, stream: Stream, raw: dict[str, Any]):
        snapshot = remote_snapshot(raw)
        previous = await self._rows(db, stream)
        if stream.publication != "idle":
            # A live request owns this stream. A crashed/uncertain request is reconciled by reads only.
            if not self._settled(stream):
                return
            if stream.pending is not None and snapshot["offers"] == remote_offers(outgoing(stream.pending)):
                await self._save_rows(db, stream, stream.pending)
                stream.draft = None
                stream.conflict = False
                previous = await self._rows(db, stream)
            stream.publication = "idle"
            stream.pending = None
            stream.publishing_at = None
            stream.revision += 1
        if stream.draft is not None and snapshot != stream.confirmed:
            stream.conflict = True
        current = {item["offer_id"]: item for item in snapshot["offers"]}
        rows = []
        for old in previous:
            found = current.pop(old["offer_id"], None)
            rows.append({**old, "removed": found is None, "share": found["share"] if found else 0,
                         "state": found["state"] if found else old["state"],
                         "pinned": old["pinned"] if found and found["state"] == "active" else False})
        for item in current.values():
            await self._remember_offer(db, item["offer_id"])
            rows.append({**item, "removed": False, "pinned": False})
        await self._save_rows(db, stream, rows)
        if snapshot != stream.confirmed or stream.missing:
            stream.revision += 1
        stream.confirmed = snapshot
        stream.name = str(raw.get("name", ""))[:1024]
        stream.position = raw.get("position", 0)
        stream.schema = raw.get("schema", "")
        stream.missing = False

    async def _sync_one(self, db: AsyncSession, raw: dict[str, Any], domains: list[dict[str, Any]]):
        campaign_id = raw["id"]
        # The global import lock serializes first-time imports; the campaign lock serializes all later operations.
        data = dict(raw, admin_url=self.campaigns._admin_url(campaign_id))
        await db.execute(insert(Campaign).values(id=campaign_id, alias=raw["alias"], state=raw["state"], data=data)
                         .on_conflict_do_nothing(index_elements=[Campaign.id]))
        campaign = await self._lock_campaign(db, campaign_id, allow_deleted=True)
        remote = await self.client.get_list(f"campaigns/{campaign_id}/streams")
        # Re-read campaign metadata after locking, so a stale list cannot resurrect an archived campaign.
        raw = await self._remote_campaign(campaign_id)
        read = await self.campaigns._read(raw, streams=remote, domains=domains)
        campaign.alias, campaign.state, campaign.data = read.alias, read.state, read.model_dump()
        known = {stream.id: stream for stream in (await db.scalars(
            select(Stream).where(Stream.campaign_id == campaign_id).with_for_update())).all()}
        seen = set()
        for item in remote:
            if item.get("state") == "deleted":
                continue
            seen.add(item["id"])
            stream = known.get(item["id"])
            if stream is None:
                stream = Stream(id=item["id"], campaign_id=campaign_id, name=item.get("name", ""),
                                position=item.get("position", 0), schema=item.get("schema", ""), confirmed={},
                                revision=1, missing=False, conflict=False, publication="idle")
                db.add(stream)
                await db.flush()
            await self._ingest(db, stream, item)
        for stream in known.values():
            if stream.id not in seen:
                self._mark_missing(stream)
        return campaign

    async def _update_summary(self, db: AsyncSession, campaign_id: int):
        await db.flush()
        ids = list(dict.fromkeys((await db.scalars(select(StreamOffer.offer_id).join(Stream).where(
            Stream.campaign_id == campaign_id, Stream.missing.is_(False), StreamOffer.removed.is_(False),
            StreamOffer.state == "active").order_by(Stream.position, StreamOffer.position))).all()))
        campaign = await db.get(Campaign, campaign_id)
        campaign.data = {**campaign.data, "offer_ids": ids, "offer_id": ids[0] if len(ids) == 1 else None}

    async def list_campaigns(self):
        async with self.sessions() as db:
            campaigns = (await db.scalars(select(Campaign).where(Campaign.state != "deleted").order_by(Campaign.id.desc()))).all()
            return [CampaignRead.model_validate(item.data) for item in campaigns]

    async def sync_campaigns(self):
        self.campaigns._require_key()
        async with self.sessions.begin() as db:
            await db.execute(text("SELECT pg_advisory_xact_lock(71120401)"))
            available = await self.client.get_list("campaigns")
            managed = [raw for raw in available if self.campaigns._managed(raw) and raw.get("state") != "deleted"]
            domains = await self.client.get_list("domains") if managed else []
            for raw in sorted(managed, key=lambda item: item["id"]):
                await self._sync_one(db, raw, domains)
            ids = {item["id"] for item in managed}
            existing = (await db.scalars(select(Campaign).with_for_update())).all()
            for item in existing:
                if item.id not in ids:
                    item.state = "deleted"
                    item.data = {**item.data, "state": "deleted"}
        return await self.list_campaigns()

    async def sync_campaign(self, campaign_id: int):
        async with self.sessions.begin() as db:
            await db.execute(text("SELECT pg_advisory_xact_lock(71120401)"))
            raw = await self._remote_campaign(campaign_id)
            await self._sync_one(db, raw, await self.client.get_list("domains"))
        return await self.read(campaign_id)

    async def archive(self, campaign_id: int):
        async with self.sessions.begin() as db:
            # Import is serialized with archive, including a first visit before initial import.
            await db.execute(text("SELECT pg_advisory_xact_lock(71120401)"))
            existing = await db.scalar(select(Campaign).where(Campaign.id == campaign_id).with_for_update())
            if existing:
                streams = (await db.scalars(select(Stream).where(Stream.campaign_id == campaign_id))).all()
                if any(stream.publication != "idle" for stream in streams):
                    reject("publication_pending", "Check the flow publication result first.", 409)
            result = await self.campaigns.delete(campaign_id)
            if existing:
                existing.state = "deleted"
                existing.data = {**existing.data, "state": "deleted"}
            return result

    async def _render(self, db: AsyncSession, stream: Stream) -> StreamRead:
        rows = stream.draft if stream.draft is not None else await self._rows(db, stream)
        allocation_error = None
        if stream.draft is not None:
            try:
                distribute(deepcopy(rows))
            except KeitaroError as error:
                allocation_error = error.message
        names = {offer.id: offer.name for offer in (await db.scalars(
            select(Offer).where(Offer.id.in_([row["offer_id"] for row in rows])))).all()} if rows else {}
        return StreamRead(id=stream.id, name=stream.name, position=stream.position, revision=stream.revision,
                          editable=bool(rows) and stream.schema == "landings" and not stream.missing,
                          missing=stream.missing, dirty=stream.draft is not None, conflict=stream.conflict,
                          publication=stream.publication,
                          allocation_error=allocation_error,
                          offers=[{**row, "name": names.get(row["offer_id"], f"Offer {row['offer_id']}")} for row in rows])

    async def read(self, campaign_id: int):
        async with self.sessions.begin() as db:
            await self._lock_campaign(db, campaign_id)
            streams = (await db.scalars(select(Stream).where(Stream.campaign_id == campaign_id)
                                       .order_by(Stream.position, Stream.id))).all()
            for stream in streams:
                if stream.publication != "idle" and self._settled(stream):
                    await self._remote_campaign(campaign_id)
                    try:
                        raw = await self.client.get_object(f"streams/{stream.id}")
                    except KeitaroError as error:
                        if error.code != "keitaro_not_found":
                            raise
                        self._mark_missing(stream)
                        continue
                    if raw.get("state") == "deleted":
                        self._mark_missing(stream)
                        continue
                    if raw.get("campaign_id") != campaign_id:
                        reject("stream_not_found", "The flow no longer belongs to this campaign.", 409)
                    await self._ingest(db, stream, raw)
            await self._update_summary(db, campaign_id)
            return [await self._render(db, stream) for stream in streams if not stream.missing or stream.draft is not None]

    async def edit(self, campaign_id: int, stream_id: int, payload: DraftInput):
        async with self.sessions.begin() as db:
            stream = await self._lock_stream(db, campaign_id, stream_id)
            self._revision(stream, payload.expected_revision)
            if stream.conflict or stream.missing:
                reject("stream_conflict", "The flow changed in Keitaro. Cancel the draft and fetch flows again.", 409)
            rows = deepcopy(stream.draft if stream.draft is not None else await self._rows(db, stream))
            if stream.schema != "landings" or not rows:
                reject("stream_not_editable", "This flow has no offers and cannot be edited.")
            row = next((row for row in rows if row["offer_id"] == payload.offer_id), None)
            if payload.action in {"add", "restore"}:
                if row and not row["removed"]:
                    reject("duplicate_offer", "This offer is already in the flow.")
                if payload.action == "restore" and row is None:
                    reject("offer_not_found", "Offer not found in the flow archive.")
                if payload.action == "add" and row:
                    reject("offer_archived", "The offer is already archived. Use Bring back.")
                raw = await self.client.get_object(f"offers/{payload.offer_id}")
                if raw.get("state") != "active":
                    reject("offer_inactive", "The selected offer is inactive.")
                await self._remember_offer(db, payload.offer_id, raw)
                if row is None:
                    row = dict(offer_id=payload.offer_id, share=0, state="active", pinned=False, removed=False)
                    rows.append(row)
                row.update(removed=False, pinned=False, state="active")
            elif row is None or row["removed"]:
                reject("offer_not_found", "Active offer not found in the flow.")
            elif payload.action == "remove":
                row.update(removed=True, pinned=False, share=0)
            else:
                if row["state"] != "active" or row["share"] <= 0:
                    reject("offer_inactive", "Only an active offer share can be pinned.")
                row["pinned"] = not row["pinned"]
                if stream.draft is None:
                    # Pinning alone is application metadata; it does not publish traffic changes.
                    await self._save_rows(db, stream, rows)
                    stream.revision += 1
                    return await self._render(db, stream)
            distribute(rows)
            baseline = await self._rows(db, stream)
            stream.draft = rows if rows != baseline else None
            stream.revision += 1
            return await self._render(db, stream)

    async def cancel(self, campaign_id: int, stream_id: int, expected: int):
        async with self.sessions.begin() as db:
            stream = await self._lock_stream(db, campaign_id, stream_id)
            self._revision(stream, expected)
            stream.draft = None
            stream.conflict = False
            stream.revision += 1
            return await self._render(db, stream)

    async def publish(self, campaign_id: int, stream_id: int, expected: int):
        # Commit the intended write BEFORE sending it; a process crash must not lose its identity.
        error = None
        async with self.sessions.begin() as db:
            stream = await self._lock_stream(db, campaign_id, stream_id)
            self._revision(stream, expected)
            if stream.draft is None:
                reject("no_draft", "No changes to publish.")
            if stream.conflict or stream.missing:
                reject("stream_conflict", "The flow changed in Keitaro. Cancel the draft and fetch flows again.", 409)
            await self._remote_campaign(campaign_id)
            raw = await self.client.get_object(f"streams/{stream_id}")
            if raw.get("campaign_id") != campaign_id:
                reject("stream_not_found", "The flow no longer belongs to this campaign.", 409)
            if remote_snapshot(raw) != stream.confirmed:
                await self._ingest(db, stream, raw)
                error = KeitaroError("stream_conflict", "The flow changed in Keitaro. Cancel the draft and fetch flows again.", http_status=409)
            else:
                rows = stream.draft
                distribute(deepcopy(rows))
                active = [row for row in rows if not row["removed"] and row["state"] == "active"]
                if not active or sum(row["share"] for row in active) != 100:
                    reject("invalid_shares", "Active offer shares must total 100%.")
                old_ids = {item["offer_id"] for item in stream.confirmed["offers"]}
                for row in rows:
                    if not row["removed"] and row["offer_id"] not in old_ids:
                        offer = await self.client.get_object(f"offers/{row['offer_id']}")
                        if offer.get("state") != "active":
                            reject("offer_inactive", f"Offer {row['offer_id']} is inactive.")
                stream.pending = deepcopy(rows)
                stream.publication = "publishing"
                stream.publishing_at = datetime.now(timezone.utc)
                stream.revision += 1
        if error:
            raise error
        async with self.sessions.begin() as db:
            stream = await self._lock_stream(db, campaign_id, stream_id)
            try:
                await self.client.request("PUT", f"streams/{stream_id}", json={"offers": outgoing(stream.pending)})
            except KeitaroError as caught:
                error = caught
            if error is None or error.uncertain:
                try:
                    actual = await self.client.get_object(f"streams/{stream_id}")
                    if (actual.get("campaign_id") == campaign_id
                            and remote_offers(actual.get("offers") or []) == remote_offers(outgoing(stream.pending))):
                        await self._save_rows(db, stream, stream.pending)
                        stream.draft = None
                        stream.confirmed = remote_snapshot(actual)
                        stream.conflict = False
                        stream.publication = "idle"
                        stream.pending = None
                        stream.publishing_at = None
                        await self._update_summary(db, campaign_id)
                        error = None
                    else:
                        error = KeitaroError("publication_unconfirmed", "Keitaro did not confirm publication. Fetch flows to check the result.", uncertain=True)
                except KeitaroError as caught:
                    error = caught
                    error.uncertain = True
            if error:
                stream.publication = "uncertain" if error.uncertain else "idle"
                if not error.uncertain:
                    stream.pending = None
                    stream.publishing_at = None
                error.campaign_id = campaign_id
            stream.revision += 1
            result = await self._render(db, stream)
        if error:
            raise error
        return result
