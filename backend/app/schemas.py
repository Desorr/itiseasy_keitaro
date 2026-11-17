from typing import Literal

import pycountry
from pydantic import BaseModel, ConfigDict, Field, field_validator


class CampaignCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=5, max_length=255)
    geo_code: str = Field(min_length=2, max_length=2, description="Коды стран через запятую, например DE, AU, BY")
    offer_id: int = Field(gt=0, le=9007199254740991, strict=True)

    @field_validator("name", mode="before")
    @classmethod
    def validate_name(cls, value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Введите название кампании")
        if len(value.strip()) < 5:
            raise ValueError("Название должно содержать не меньше 5 символов")
        if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
            raise ValueError("Название содержит недопустимые управляющие символы")
        if len(value.strip()) > 255:
            raise ValueError("Название должно содержать не больше 255 символов")
        return value.strip()

    @field_validator("geo_code", mode="before")
    @classmethod
    def validate_geo(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("Укажите один двухбуквенный GEO-код страны")
        code = value.strip().upper()
        if code and any(char < "A" or char > "Z" for char in code):
            raise ValueError("GEO-код поддерживает только буквы A–Z")
        if len(code) != 2 or pycountry.countries.get(alpha_2=code) is None:
            raise ValueError("Укажите один существующий двухбуквенный GEO-код страны")
        return code

    @property
    def geo_codes(self) -> list[str]:
        return [self.geo_code]


class OfferRead(BaseModel):
    id: int
    name: str


class CampaignRead(BaseModel):
    id: int
    name: str
    alias: str
    state: str
    domain_id: int | None = None
    group_id: int | None = None
    traffic_source_id: int | None = None
    geo_code: str | None = None
    offer_id: int | None = None
    google_stream_id: int | None = None
    offer_stream_id: int | None = None
    tracking_url: str | None = None
    admin_url: str


class CampaignDeleteRead(BaseModel):
    campaign_id: int
    status: Literal["archived"] = "archived"
    group_id: int | None = None
    group_deleted: bool = False
    message: str = "Кампания перенесена в архив Keitaro."
