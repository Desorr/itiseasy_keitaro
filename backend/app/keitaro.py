import logging
from typing import Any

import httpx

from .config import Settings

logger = logging.getLogger(__name__)


class KeitaroError(Exception):
    def __init__(self, code: str, message: str, *, http_status: int = 502, uncertain: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.uncertain = uncertain
        self.campaign_id: int | None = None


class KeitaroClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.client = httpx.AsyncClient(
            base_url=f"{settings.keitaro_base_url}/admin_api/v1/",
            headers={
                "Api-Key": settings.keitaro_api_key.get_secret_value(),
                "Accept": "application/json",
                "User-Agent": "KeitaroCampaignManager/0.1",
            },
            timeout=httpx.Timeout(settings.keitaro_timeout_seconds, connect=10, pool=5),
            follow_redirects=False,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self.client.aclose()

    def _validation_message(self, data: Any, *, group: bool = False) -> str | None:
        if not isinstance(data, dict):
            return None
        fields = data.get("errors", data)
        if not isinstance(fields, dict):
            return None
        labels = {
            "name": "Group name" if group else "Campaign name", "alias": "Campaign alias",
            "domain_id": "Domain", "group_id": "Group", "traffic_source_id": "Traffic source",
            "type": "Group type" if group else "Campaign type", "state": "State", "campaign_id": "Campaign",
            "offer_id": "Offer", "filters": "Filters", "offers": "Offers",
        }
        messages = []
        key = self.client.headers.get("Api-Key", "")
        entity = "Group" if group else "Campaign"
        for field, label in labels.items():
            errors = fields.get(field)
            errors = [errors] if isinstance(errors, str) else errors
            if not isinstance(errors, list):
                continue
            for error in errors[:3]:
                if not isinstance(error, str):
                    continue
                error = error.replace(key, "[redacted]") if key else error
                text = " ".join(error.split())[:300]
                if field == "name" and "already been used in the archive" in text.casefold():
                    return f"A {entity} with this name already exists in the Keitaro archive. Use another name."
                if field == "name" and any(
                    phrase in text.casefold() for phrase in ("already been used", "already exists", "already been taken")
                ):
                    return f"A {entity} with this name already exists in Keitaro. Use another name."
                messages.append(f"{label}: {text}")
        if not messages:
            return None
        message = "Keitaro rejected the data. " + " ".join(messages)
        return (message.replace(key, "[redacted]") if key else message)[:1200]

    async def request(self, method: str, path: str, *, allow_empty: bool = False, **kwargs: Any) -> Any:
        mutating = method != "GET"
        try:
            response = await self.client.request(method, path.lstrip("/"), **kwargs)
        except httpx.TimeoutException:
            logger.warning("Keitaro timeout: %s %s", method, path.split("?")[0])
            raise KeitaroError(
                "keitaro_timeout",
                "Keitaro timed out.",
                http_status=504,
                uncertain=mutating,
            ) from None
        except httpx.RequestError:
            logger.warning("Keitaro connection error: %s %s", method, path.split("?")[0])
            raise KeitaroError(
                "keitaro_unavailable",
                "Failed to connect to Keitaro.",
                uncertain=mutating,
            ) from None

        try:
            data = response.json()
        except ValueError:
            data = None

        if not response.is_success:
            logger.warning("Keitaro HTTP %s: %s %s", response.status_code, method, path.split("?")[0])
            if isinstance(data, dict) and data.get("error_code") == 1010:
                raise KeitaroError("cloudflare_blocked", "Cloudflare is blocking access to the Keitaro API.")
            errors = {
                400: ("keitaro_validation", "Keitaro rejected the request parameters."),
                401: ("keitaro_unauthorized", "Keitaro rejected the API key."),
                402: ("keitaro_api_unavailable", "Admin API is unavailable with the current Keitaro license."),
                403: ("keitaro_forbidden", "Insufficient permissions for this Keitaro request."),
                404: ("keitaro_not_found", "Object not found in Keitaro."),
                406: ("keitaro_validation", "Keitaro rejected the object parameters."),
                422: ("keitaro_validation", "Keitaro rejected the data. Check the supplied fields."),
                429: ("keitaro_rate_limit", "Too many Keitaro requests. Try again later."),
            }
            if response.status_code >= 500:
                fallback = (
                    "keitaro_unavailable",
                    f"Keitaro is temporarily unavailable (HTTP {response.status_code}). Try again later.",
                )
            else:
                fallback = ("keitaro_error", f"Keitaro rejected the request (HTTP {response.status_code}).")
            code, message = errors.get(response.status_code, fallback)
            if response.status_code in (400, 406, 422):
                message = self._validation_message(data, group=path.strip("/") == "groups") or message
            raise KeitaroError(
                code, message, http_status=422 if response.status_code == 422 else 502,
                uncertain=mutating and response.status_code >= 500,
            )
        if allow_empty and not response.content.strip():
            return None
        if data is None:
            raise KeitaroError(
                "keitaro_invalid_response",
                "Keitaro returned an invalid response.",
                uncertain=mutating,
            )
        return data

    async def archive_campaign(self, campaign_id: int) -> None:
        await self.request("DELETE", f"campaigns/{campaign_id}", allow_empty=True)

    async def delete_group(self, group_id: int) -> None:
        await self.request("DELETE", f"groups/{group_id}/delete", allow_empty=True)

    async def get_list(self, path: str, **kwargs: Any) -> list[dict[str, Any]]:
        data = await self.request("GET", path, **kwargs)
        if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
            raise KeitaroError("keitaro_invalid_response", "Unexpected Keitaro list format.")
        return data

    async def get_object(self, path: str) -> dict[str, Any]:
        data = await self.request("GET", path)
        if not isinstance(data, dict):
            raise KeitaroError("keitaro_invalid_response", "Unexpected Keitaro object format.")
        return data

    async def create(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = await self.request("POST", path, json=payload)
        if not isinstance(data, dict) or not isinstance(data.get("id"), int):
            raise KeitaroError(
                "keitaro_invalid_response",
                "The Keitaro response does not contain an object ID.",
                uncertain=True,
            )
        return data
