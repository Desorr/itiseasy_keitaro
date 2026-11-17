import asyncio
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

import httpx
from fastapi import FastAPI, Header, Path, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from .config import Settings
from .database import connect_database
from .editor import EditorService
from .keitaro import KeitaroClient, KeitaroError
from .schemas import CampaignCreate, CampaignDeleteRead, CampaignSummary, DraftInput, ErrorRead, HealthRead, OfferRead, PreparationRead, RevisionInput, StreamRead
from .services import CampaignService


def create_app(
    settings: Settings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.keitaro = KeitaroClient(settings, transport)
        app.state.engine, app.state.sessions = connect_database(settings)
        try:
            yield
        finally:
            await app.state.keitaro.aclose()
            await app.state.engine.dispose()

    app = FastAPI(
        title="Keitaro Campaign Manager", version="0.3.0", lifespan=lifespan,
        description=(
            "Application API for managed Keitaro campaigns and offer bindings. "
            "Campaign lists return only id, name, alias and state. Keitaro credentials stay on the backend. "
            "Writes with an uncertain result must be reconciled by reading state; do not automatically replay them."
        ),
        openapi_tags=[
            {"name": "System", "description": "Configuration and database readiness."},
            {"name": "Offers", "description": "Fresh active offers read from Keitaro."},
            {"name": "Campaigns", "description": "Campaign preparation, listing, creation and archival."},
            {"name": "Flows", "description": "Import flows, edit saved drafts and publish offer bindings."},
        ],
        responses={
            404: {"model": ErrorRead, "description": "Managed campaign or flow not found."},
            409: {"model": ErrorRead, "description": "Stale revision, external conflict or pending publication."},
            422: {"model": ErrorRead, "description": "Invalid request fields or rejected editor operation."},
            502: {"model": ErrorRead, "description": "Keitaro rejected the request or its result could not be confirmed."},
            503: {"model": ErrorRead, "description": "Database unavailable or required configuration missing."},
            504: {"model": ErrorRead, "description": "Keitaro request timed out; inspect uncertain and campaign_id."},
        },
    )

    def editor(request: Request) -> EditorService:
        return EditorService(request.app.state.keitaro, settings, request.app.state.sessions)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, error: RequestValidationError):
        messages = {
            "expected_revision": "Enter a positive integer flow revision.",
            "offer_id": "Enter a positive integer offer ID.",
            "action": "Select a valid offer action.",
            "campaign_id": "Enter a positive integer campaign ID.",
            "stream_id": "Enter a positive integer flow ID.",
        }
        details = []
        for item in error.errors():
            field = str(item["loc"][-1]) if item["loc"] else ""
            message = messages.get(field, str(item["msg"]).removeprefix("Value error, "))
            if item["type"] == "extra_forbidden":
                message = "Unknown field supplied."
            elif item["type"] == "json_invalid":
                message = "Invalid JSON request."
            details.append({"loc": item["loc"], "msg": message, "type": item["type"]})
        return JSONResponse(status_code=422, content={"detail": details})

    @app.exception_handler(SQLAlchemyError)
    async def database_error_handler(request: Request, error: SQLAlchemyError):
        return JSONResponse(status_code=503, content={"detail": {
            "code": "database_unavailable", "message": "Database unavailable. Refresh the state before making changes.",
            "uncertain": request.method not in {"GET", "HEAD"},
            "campaign_id": request.path_params.get("campaign_id"),
        }})

    @app.exception_handler(KeitaroError)
    async def keitaro_error_handler(request: Request, error: KeitaroError):
        return JSONResponse(status_code=error.http_status, content={"detail": {
            "code": error.code, "message": error.message,
            "uncertain": error.uncertain, "campaign_id": error.campaign_id,
        }})

    @app.get("/api/health", response_model=HealthRead, tags=["System"],
             summary="Check configuration and database readiness",
             description="Executes SELECT 1 and reports missing settings. Does not verify Keitaro connectivity or API permissions.")
    async def health(request: Request):
        missing = settings.missing_configuration()
        async with request.app.state.sessions() as db:
            await db.execute(text("SELECT 1"))
        return {"status": "ok", "configured": not missing, "missing_configuration": missing}

    @app.get("/api/offers", response_model=list[OfferRead], tags=["Offers"],
             summary="Load active offers from Keitaro",
             description="Reads Keitaro on every request, without an offer cache or database fallback. An empty list means no active offers were returned for this API access.")
    async def offers(request: Request, query: str = Query(default="", max_length=255, description="Optional case-insensitive ID/name substring. The frontend searches the loaded list locally.")):
        if not settings.keitaro_api_key.get_secret_value():
            raise KeitaroError("configuration_missing", "Set the API key in .env.", http_status=503)
        needle = query.strip().casefold()
        available = await request.app.state.keitaro.get_list("offers")
        matches = [
            {"id": offer["id"], "name": offer["name"]}
            for offer in available
            if offer.get("state") == "active"
            and (needle in str(offer.get("id", "")) or needle in str(offer.get("name", "")).casefold())
        ]
        return sorted(matches, key=lambda item: (str(item["id"]) != needle, item["name"].casefold()))

    @app.get("/api/campaigns/preparation", response_model=PreparationRead, tags=["Campaigns"],
             summary="Preload campaign reference data",
             description="Loads domains, traffic sources and groups in parallel with a 60-second backend cache. Does not load offers or create objects. Creation reads groups fresh.")
    async def prepare_campaign(request: Request):
        if not settings.keitaro_api_key.get_secret_value():
            raise KeitaroError("configuration_missing", "Set the API key in .env.", http_status=503)
        client = request.app.state.keitaro
        await asyncio.gather(
            client.preparation_list("domains"),
            client.preparation_list("traffic_sources"),
            client.preparation_list("groups", params={"type": "campaigns"}),
        )
        return {"ready": True}

    @app.get("/api/campaigns", response_model=list[CampaignSummary], tags=["Campaigns"],
             summary="List saved campaigns", description="Reads non-archived campaigns from PostgreSQL without contacting Keitaro.")
    async def campaigns(request: Request):
        return await editor(request).list_campaigns()

    @app.post("/api/campaigns/sync", response_model=list[CampaignSummary], tags=["Campaigns"],
              summary="Sync managed campaigns from Keitaro", description="Imports campaigns with tm_<16 hex> aliases and their flows into PostgreSQL. Preserves drafts and archived bindings. Returns public campaign summaries.")
    async def sync_campaigns(request: Request):
        return await editor(request).sync_campaigns()

    @app.get("/api/campaigns/{campaign_id}/streams", response_model=list[StreamRead], tags=["Flows"],
             summary="Read saved flow editor state", description="Reads saved flows and drafts. A settled interrupted publication may trigger read-only Keitaro reconciliation after 2*timeout+10 seconds. Does not replay writes.")
    async def streams(campaign_id: Annotated[int, Path(gt=0)], request: Request):
        return await editor(request).read(campaign_id)

    @app.post("/api/campaigns/{campaign_id}/streams/sync", response_model=list[StreamRead], tags=["Flows"],
              summary="Fetch flows from Keitaro", description="Imports actual shares without redistribution or automatic drafts. Missing known bindings remain archived. External changes with an existing draft cause a conflict.")
    async def sync_streams(campaign_id: Annotated[int, Path(gt=0)], request: Request):
        return await editor(request).sync_campaign(campaign_id)

    @app.patch("/api/campaigns/{campaign_id}/streams/{stream_id}/draft", response_model=StreamRead, tags=["Flows"],
               summary="Edit offer bindings or pin a share", description="Saves add/remove/restore as a draft and redistributes unpinned shares. The last offer may be removed. Pinning without a draft updates only database metadata. Does not publish to Keitaro.")
    async def edit_stream(campaign_id: Annotated[int, Path(gt=0)], stream_id: Annotated[int, Path(gt=0)],
                          payload: DraftInput, request: Request):
        return await editor(request).edit(campaign_id, stream_id, payload)

    @app.delete("/api/campaigns/{campaign_id}/streams/{stream_id}/draft", response_model=StreamRead, tags=["Flows"],
                summary="Cancel a flow draft", description="Requires expected_revision in the JSON body. Discards the draft and accepts the latest confirmed state; pending publication must be reconciled first.")
    async def cancel_draft(campaign_id: Annotated[int, Path(gt=0)], stream_id: Annotated[int, Path(gt=0)],
                           payload: RevisionInput, request: Request):
        return await editor(request).cancel(campaign_id, stream_id, payload.expected_revision)

    @app.post("/api/campaigns/{campaign_id}/streams/{stream_id}/publish", response_model=StreamRead, tags=["Flows"],
              summary="Publish one flow to Keitaro", description="Checks ownership, revision and external changes, saves intent before PUT, writes only offers, then confirms by GET. Empty bindings are allowed. After timeout, read or sync to reconcile instead of repeating publication. Other flow settings are preserved.")
    async def publish_stream(campaign_id: Annotated[int, Path(gt=0)], stream_id: Annotated[int, Path(gt=0)],
                             payload: RevisionInput, request: Request):
        return await editor(request).publish(campaign_id, stream_id, payload.expected_revision)

    @app.post(
        "/api/campaigns", response_model=CampaignSummary, status_code=201,
        tags=["Campaigns"], summary="Create and verify a campaign with two flows",
        responses={200: {"model": CampaignSummary, "description": "An uncertain write was reconciled and the active campaign was verified."}},
        description="The first available domain and traffic source are selected through the Keitaro API. "
        "Groups are read from the API: FORTESTS is preferred, then test_markovskiy_group; only the latter is created if both are missing. "
        "Domain/source reference data may be cached for 60 seconds. No extra offer lookup is made. "
        "Creates a disabled campaign, Flow 1 (GEO to Google) and Flow 2 (selected offer at 100%) sequentially, "
        "verifies settings, enables the campaign, verifies again and saves the confirmed snapshot in PostgreSQL.",
    )
    async def create_campaign(
        payload: CampaignCreate,
        request: Request,
        response: Response,
        idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key", description="Required UUID used to derive the tracking alias for recovery. Does not make replaying POST safe.")],
    ):
        created, status = await CampaignService(request.app.state.keitaro, settings).create(payload, idempotency_key)
        campaign = created.campaign
        try:
            await editor(request).save_created(created)
        except (KeitaroError, SQLAlchemyError):
            error = KeitaroError("campaign_import_unconfirmed", "The campaign was created in Keitaro, but database persistence is unconfirmed. Refresh the campaign list.", uncertain=True)
            error.campaign_id = campaign.id
            raise error from None
        response.status_code = status
        return campaign

    @app.delete(
        "/api/campaigns/{campaign_id}", response_model=CampaignDeleteRead,
        tags=["Campaigns"], summary="Archive a campaign",
        description="The campaign is archived via Keitaro API. Ownership and archival are verified. The database history, group and offer objects are retained; this does not clean the tracker archive.",
    )
    async def delete_campaign(campaign_id: Annotated[int, Path(gt=0)], request: Request):
        return await editor(request).archive(campaign_id)

    return app
