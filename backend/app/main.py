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
from .schemas import CampaignCreate, CampaignDeleteRead, CampaignRead, DraftInput, OfferRead, RevisionInput, StreamRead
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

    app = FastAPI(title="Keitaro Campaign Manager", version="0.3.0", lifespan=lifespan)

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

    @app.get("/api/health")
    async def health(request: Request):
        missing = settings.missing_configuration()
        async with request.app.state.sessions() as db:
            await db.execute(text("SELECT 1"))
        return {"status": "ok", "configured": not missing, "missing_configuration": missing}

    @app.get("/api/offers", response_model=list[OfferRead])
    async def offers(request: Request, query: str = Query(default="", max_length=255)):
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

    @app.get("/api/campaigns", response_model=list[CampaignRead])
    async def campaigns(request: Request):
        return await editor(request).list_campaigns()

    @app.post("/api/campaigns/sync", response_model=list[CampaignRead])
    async def sync_campaigns(request: Request):
        return await editor(request).sync_campaigns()

    @app.get("/api/campaigns/{campaign_id}/streams", response_model=list[StreamRead])
    async def streams(campaign_id: Annotated[int, Path(gt=0)], request: Request):
        return await editor(request).read(campaign_id)

    @app.post("/api/campaigns/{campaign_id}/streams/sync", response_model=list[StreamRead])
    async def sync_streams(campaign_id: Annotated[int, Path(gt=0)], request: Request):
        return await editor(request).sync_campaign(campaign_id)

    @app.patch("/api/campaigns/{campaign_id}/streams/{stream_id}/draft", response_model=StreamRead)
    async def edit_stream(campaign_id: Annotated[int, Path(gt=0)], stream_id: Annotated[int, Path(gt=0)],
                          payload: DraftInput, request: Request):
        return await editor(request).edit(campaign_id, stream_id, payload)

    @app.delete("/api/campaigns/{campaign_id}/streams/{stream_id}/draft", response_model=StreamRead)
    async def cancel_draft(campaign_id: Annotated[int, Path(gt=0)], stream_id: Annotated[int, Path(gt=0)],
                           payload: RevisionInput, request: Request):
        return await editor(request).cancel(campaign_id, stream_id, payload.expected_revision)

    @app.post("/api/campaigns/{campaign_id}/streams/{stream_id}/publish", response_model=StreamRead)
    async def publish_stream(campaign_id: Annotated[int, Path(gt=0)], stream_id: Annotated[int, Path(gt=0)],
                             payload: RevisionInput, request: Request):
        return await editor(request).publish(campaign_id, stream_id, payload.expected_revision)

    @app.post(
        "/api/campaigns", response_model=CampaignRead, status_code=201,
        summary="Create a campaign in the shared group",
        description="The first available domain and traffic source are selected through the Keitaro API. "
        "The test_markovskiy_group group is retrieved via API and created only if missing.",
    )
    async def create_campaign(
        payload: CampaignCreate,
        request: Request,
        response: Response,
        idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
    ):
        campaign, status = await CampaignService(request.app.state.keitaro, settings).create(payload, idempotency_key)
        try:
            await editor(request).sync_campaign(campaign.id)
        except (KeitaroError, SQLAlchemyError):
            error = KeitaroError("campaign_import_unconfirmed", "The campaign was created in Keitaro, but database persistence is unconfirmed. Refresh the campaign list.", uncertain=True)
            error.campaign_id = campaign.id
            raise error from None
        response.status_code = status
        return campaign

    @app.delete(
        "/api/campaigns/{campaign_id}", response_model=CampaignDeleteRead,
        summary="Archive a campaign",
        description="The campaign is archived via Keitaro API. Its group is retained even after all campaigns are archived.",
    )
    async def delete_campaign(campaign_id: Annotated[int, Path(gt=0)], request: Request):
        return await editor(request).archive(campaign_id)

    return app
