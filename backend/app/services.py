import re
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit
from uuid import UUID

from .config import Settings
from .keitaro import KeitaroClient, KeitaroError
from .schemas import CampaignCreate, CampaignDeleteRead, CampaignRead

GOOGLE_URL = "https://google.com"
GOOGLE_STREAM_NAME = "GEO: Google"
OFFER_STREAM_NAME = "Offer"
UNIQUENESS_METHOD = "ip_ua"
CAMPAIGN_GROUP_NAME = "test_markovskiy_group"


class CampaignService:
    def __init__(self, client: KeitaroClient, settings: Settings):
        self.client = client
        self.settings = settings

    def _require_key(self) -> None:
        if not self.settings.keitaro_api_key.get_secret_value():
            raise KeitaroError("configuration_missing", "Укажите API-ключ в .env.", http_status=503)

    @staticmethod
    def _managed(campaign: dict[str, Any]) -> bool:
        return re.fullmatch(r"tm_[0-9a-f]{16}", str(campaign.get("alias", ""))) is not None

    async def list_campaigns(self) -> list[CampaignRead]:
        self._require_key()
        available = await self.client.get_list("campaigns")
        campaigns = [
            campaign for campaign in available
            if self._managed(campaign) and campaign.get("state") != "deleted"
        ]
        if not campaigns:
            return []
        domains = await self.client.get_list("domains")
        return [
            await self._read(
                campaign, domains=domains,
                streams=await self.client.get_list(f"campaigns/{campaign['id']}/streams"),
            )
            for campaign in sorted(campaigns, key=lambda item: item["id"], reverse=True)
        ]

    async def delete(self, campaign_id: int) -> CampaignDeleteRead:
        self._require_key()
        archived = None
        campaign = await self._campaign_or_none(campaign_id)
        if campaign is None:
            # The tracker may hide archived campaigns from GET campaigns/{id}.
            # Recover archived campaigns when the regular endpoint hides them.
            archived = await self.client.get_list("campaigns/deleted")
            campaign = next((item for item in archived if item.get("id") == campaign_id), None)
        if campaign is None:
            return CampaignDeleteRead(campaign_id=campaign_id, message="Кампания уже отсутствует в Keitaro.")
        if not self._managed(campaign):
            raise KeitaroError("campaign_not_found", "Кампания не найдена среди кампаний приложения.", http_status=404)
        if campaign.get("state") != "deleted":
            try:
                await self.client.archive_campaign(campaign_id)
            except KeitaroError as error:
                if not error.uncertain:
                    raise
                try:
                    remaining = await self._campaign_or_none(campaign_id)
                except KeitaroError:
                    raise error from None
                if remaining is not None and remaining.get("state") != "deleted":
                    raise error
            else:
                remaining = await self._campaign_or_none(campaign_id)
                if remaining is not None and remaining.get("state") != "deleted":
                    raise KeitaroError(
                        "campaign_delete_unconfirmed", "Keitaro не подтвердил удаление кампании.", uncertain=True,
                    )

        # Keep the shared group even when its last campaign is archived.
        return CampaignDeleteRead(campaign_id=campaign_id, group_id=campaign.get("group_id"))

    async def _campaign_or_none(self, campaign_id: int) -> dict[str, Any] | None:
        try:
            return await self.client.get_object(f"campaigns/{campaign_id}")
        except KeitaroError as error:
            if error.code == "keitaro_not_found":
                return None
            raise

    async def _find_alias(self, alias: str) -> dict[str, Any] | None:
        campaigns = await self.client.get_list("campaigns")
        return next((item for item in campaigns if item.get("alias") == alias), None)

    async def create(self, payload: CampaignCreate, request_id: UUID) -> tuple[CampaignRead, int]:
        self._require_key()
        alias = f"tm_{request_id.hex[:16]}"
        domain, source = await self._preflight(payload)
        parameters = self._source_parameters(source)
        group = await self._campaign_group()
        binding = {"domain_id": domain["id"], "group_id": group["id"], "traffic_source_id": source["id"]}
        campaign_id = None
        try:
            campaign = await self.client.create("campaigns", {
                "name": payload.name, "alias": alias, "type": "position", "state": "disabled",
                "uniqueness_method": UNIQUENESS_METHOD,
                "cost_currency": "USD", "cost_auto": True,
                **binding, "group_id": str(group["id"]),
                "parameters": parameters,
            })
            campaign_id = campaign["id"]
            await self.client.create("streams", {
                "campaign_id": campaign_id, "name": GOOGLE_STREAM_NAME, "type": "regular", "position": 1,
                "schema": "redirect", "action_type": "http", "action_payload": GOOGLE_URL,
                "state": "active", "collect_clicks": True, "filter_or": False,
                "filters": [{"name": "country", "mode": "accept", "payload": payload.geo_codes}],
            })
            await self.client.create("streams", {
                "campaign_id": campaign_id, "name": OFFER_STREAM_NAME, "type": "regular", "position": 2,
                "schema": "landings", "action_type": "http", "state": "active", "collect_clicks": True,
                "filters": [], "landings": [],
                "offers": [{"offer_id": payload.offer_id, "share": 100, "state": "active"}],
            })
            await self._verify(campaign_id, alias, payload, expected_state="disabled", binding=binding, domains=[domain])
            await self.client.request("POST", f"campaigns/{campaign_id}/enable")
            result = await self._verify(campaign_id, alias, payload, expected_state="active", binding=binding, domains=[domain])
            return result, 201
        except KeitaroError as error:
            # Reconcile uncertain writes using Keitaro reads; never replay a write.
            if error.uncertain:
                try:
                    found = await self._find_alias(alias)
                    if found is not None:
                        campaign_id = found["id"]
                        if self._managed(found) and found.get("state") == "active":
                            return await self._verify(
                                campaign_id, alias, payload, expected_state="active", binding=binding, domains=[domain]
                            ), 200
                except KeitaroError:
                    pass
            error.campaign_id = campaign_id
            if campaign_id is not None:
                error.message += f" Кампания ID {campaign_id} могла быть создана частично. Проверьте её в Keitaro."
            elif error.uncertain:
                error.message += " Результат неизвестен. Обновите список кампаний перед новым созданием."
            raise

    async def _read(
        self, campaign: dict[str, Any], *, streams: list[dict[str, Any]],
        domains: list[dict[str, Any]] | None = None,
    ) -> CampaignRead:
        domains = domains if domains is not None else await self.client.get_list("domains")
        domain = next((item for item in domains if item.get("id") == campaign.get("domain_id")), None)
        tracking_url = None
        if domain is not None:
            tracking_url = f"{self._domain_url(domain)}/{campaign['alias']}"
            query = {
                parameter.get("alias") or key: parameter["placeholder"]
                for key, parameter in self._source_parameters(campaign).items()
                if parameter.get("placeholder")
            }
            if query:
                tracking_url += "?" + urlencode(query, safe="{}")
        google = next((item for item in streams if item.get("name") == GOOGLE_STREAM_NAME), {})
        offer = next((item for item in streams if item.get("name") == OFFER_STREAM_NAME), {})
        countries = next((item.get("payload") for item in (google.get("filters") or [])
                          if item.get("name") == "country" and item.get("mode") == "accept"), None)
        offers = offer.get("offers") or []
        return CampaignRead(
            id=campaign["id"], name=campaign["name"], alias=campaign["alias"], state=campaign["state"],
            geo_code=", ".join(countries) if isinstance(countries, list) and countries else None,
            offer_id=offers[0].get("offer_id") if len(offers) == 1 else None,
            google_stream_id=google.get("id"), offer_stream_id=offer.get("id"),
            domain_id=campaign.get("domain_id"), group_id=campaign.get("group_id"),
            traffic_source_id=campaign.get("traffic_source_id"),
            tracking_url=tracking_url, admin_url=self._admin_url(campaign["id"]),
        )

    async def _preflight(self, payload: CampaignCreate) -> tuple[dict[str, Any], dict[str, Any]]:
        self._require_key()
        offer = await self.client.get_object(f"offers/{payload.offer_id}")
        if offer.get("state") != "active":
            raise KeitaroError("offer_inactive", "Выбранный оффер неактивен.", http_status=422)

        def first_available(items: list[dict[str, Any]], label: str) -> dict[str, Any]:
            item = next((item for item in items if item.get("state", "active") == "active"), None)
            if item is None:
                raise KeitaroError(
                    "configuration_missing", f"{label}: нет доступных объектов в Keitaro.", http_status=422
                )
            if not isinstance(item.get("id"), int) or item["id"] <= 0:
                raise KeitaroError("keitaro_invalid_response", f"{label}: Keitaro вернул некорректный ID.")
            return item

        domain = first_available(await self.client.get_list("domains"), "Домен")
        source = first_available(await self.client.get_list("traffic_sources"), "Источник трафика")
        self._domain_url(domain)
        return domain, source

    async def _campaign_group(self) -> dict[str, Any]:
        async def find() -> dict[str, Any] | None:
            groups = await self.client.get_list("groups", params={"type": "campaigns"})
            return next((group for group in groups
                         if group.get("name") == CAMPAIGN_GROUP_NAME and group.get("type") == "campaigns"
                         and group.get("state") != "deleted"), None)

        group = await find()
        if group is not None:
            return group
        try:
            return await self.client.create("groups", {"name": CAMPAIGN_GROUP_NAME, "type": "campaigns"})
        except KeitaroError as error:
            # A concurrent request or uncertain write may have created this group.
            # Read once to reconcile; do not repeat POST groups automatically.
            if error.uncertain or error.code == "keitaro_validation":
                try:
                    group = await find()
                    if group is not None:
                        return group
                except KeitaroError:
                    pass
            error.message = "Не удалось создать группу кампании. " + error.message
            raise

    @staticmethod
    def _source_parameters(source: dict[str, Any]) -> dict[str, Any]:
        parameters = source.get("parameters") or {}
        if not isinstance(parameters, dict):
            raise KeitaroError("keitaro_invalid_response", "Некорректные параметры источника.")
        return {
            key: {field: value[field] for field in ("name", "placeholder", "alias") if field in value}
            for key, value in parameters.items()
            if isinstance(value, dict)
        }

    async def _verify(
        self, campaign_id: int, alias: str, payload: CampaignCreate, *, expected_state: str, binding: dict[str, int],
        domains: list[dict[str, Any]] | None = None,
    ) -> CampaignRead:
        campaign = await self.client.get_object(f"campaigns/{campaign_id}")
        streams = await self.client.get_list(f"campaigns/{campaign_id}/streams")
        streams = sorted(streams, key=lambda item: item.get("position", 0))
        valid = (
            campaign.get("name") == payload.name
            and campaign.get("alias") == alias
            and campaign.get("type") == "position"
            and campaign.get("uniqueness_method") == UNIQUENESS_METHOD
            and campaign.get("cost_currency") == "USD"
            and campaign.get("cost_auto") is True
            and campaign.get("state") == expected_state
            and all(str(campaign.get(field)) == str(value) for field, value in binding.items())
            and len(streams) == 2
        )
        if valid:
            google, offer = streams
            filters = google.get("filters") or []
            offers = offer.get("offers") or []
            valid = (
                google.get("position") == 1
                and google.get("schema") == "redirect"
                and google.get("action_type") == "http"
                and str(google.get("action_payload", "")).rstrip("/") == GOOGLE_URL
                and len(filters) == 1
                and filters[0].get("name") == "country"
                and filters[0].get("mode") == "accept"
                and sorted(filters[0].get("payload") or []) == sorted(payload.geo_codes)
                and offer.get("position") == 2
                and offer.get("schema") == "landings"
                and not offer.get("filters")
                and not offer.get("landings")
                and len(offers) == 1
                and offers[0].get("offer_id") == payload.offer_id
                and offers[0].get("share") == 100
                and offers[0].get("state") == "active"
                and all(
                    stream.get("type") == "regular"
                    and stream.get("state") == "active"
                    and stream.get("collect_clicks") is True
                    for stream in streams
                )
            )
        if not valid:
            raise KeitaroError(
                "configuration_mismatch", "Сохранённая конфигурация Keitaro не соответствует запросу."
            )

        return await self._read(campaign, streams=streams, domains=domains)

    @staticmethod
    def _domain_url(domain: dict[str, Any]) -> str:
        name = str(domain.get("name", "")).strip()
        if "://" not in name:
            name = f"{'https' if domain.get('is_ssl') else 'http'}://{name}"
        parts = urlsplit(name)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
            raise KeitaroError(
                "configuration_invalid", "Некорректный адрес домена кампании.", http_status=503
            )
        return urlunsplit((parts.scheme, parts.netloc, "", "", ""))

    def _admin_url(self, campaign_id: int) -> str:
        return f"{self.settings.keitaro_base_url}/admin/#!/campaigns/{campaign_id}"
