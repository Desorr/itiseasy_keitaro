from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

import httpx
from fastapi import FastAPI, Header, Path, Query, Request, Response
from fastapi.responses import JSONResponse

from .config import Settings
from .keitaro import KeitaroClient, KeitaroError
from .schemas import CampaignCreate, CampaignDeleteRead, CampaignRead, OfferRead
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
        try:
            yield
        finally:
            await app.state.keitaro.aclose()

    app = FastAPI(title="Keitaro Campaign Manager", version="0.2.0", lifespan=lifespan)

    @app.exception_handler(KeitaroError)
    async def keitaro_error_handler(request: Request, error: KeitaroError):
        return JSONResponse(status_code=error.http_status, content={"detail": {
            "code": error.code, "message": error.message,
            "uncertain": error.uncertain, "campaign_id": error.campaign_id,
        }})

    @app.get("/api/health")
    async def health():
        missing = settings.missing_configuration()
        return {"status": "ok", "configured": not missing, "missing_configuration": missing}

    @app.get("/api/offers", response_model=list[OfferRead])
    async def offers(request: Request, query: str = Query(default="", max_length=255)):
        if not settings.keitaro_api_key.get_secret_value():
            raise KeitaroError("configuration_missing", "Укажите API-ключ в .env.", http_status=503)
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
        return await CampaignService(request.app.state.keitaro, settings).list_campaigns()

    @app.post(
        "/api/campaigns", response_model=CampaignRead, status_code=201,
        summary="Создать кампанию в общей группе",
        description="Первый доступный домен и источник выбираются через Keitaro API. "
        "Группа test_markovskiy_group получается через API и создаётся только при отсутствии.",
    )
    async def create_campaign(
        payload: CampaignCreate,
        request: Request,
        response: Response,
        idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
    ):
        campaign, status = await CampaignService(request.app.state.keitaro, settings).create(payload, idempotency_key)
        response.status_code = status
        return campaign

    @app.delete(
        "/api/campaigns/{campaign_id}", response_model=CampaignDeleteRead,
        summary="Перенести кампанию в архив",
        description="Кампания архивируется через Keitaro API. Группа сохраняется даже после удаления всех кампаний.",
    )
    async def delete_campaign(campaign_id: Annotated[int, Path(gt=0)], request: Request):
        return await CampaignService(request.app.state.keitaro, settings).delete(campaign_id)

    return app
