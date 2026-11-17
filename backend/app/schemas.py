from typing import Literal

import pycountry
from pydantic import BaseModel, ConfigDict, Field, field_validator


class HealthRead(BaseModel):
    status: Literal["ok"]
    configured: bool = Field(description="Required settings are present; Keitaro connectivity is not checked.")
    missing_configuration: list[str]


class PreparationRead(BaseModel):
    ready: Literal[True] = Field(description="Domain, source and group reference lists have been loaded.")


class ErrorDetail(BaseModel):
    code: str
    message: str
    uncertain: bool = Field(description="The operation may have taken effect. Read the current state before retrying a write.")
    campaign_id: int | str | None = None


class ValidationIssue(BaseModel):
    loc: list[str | int]
    msg: str
    type: str


class ErrorRead(BaseModel):
    detail: ErrorDetail | list[ValidationIssue]


class CampaignCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=5, max_length=255, description="Trimmed campaign name; control characters are rejected.", examples=["test_markovskiy_example"])
    geo_code: str = Field(min_length=2, max_length=2, description="One existing two-letter country code; normalized to uppercase.", examples=["DE"])
    offer_id: int = Field(gt=0, le=9007199254740991, strict=True)
    offer_name: str | None = Field(default=None, max_length=1024, description="Optional display name from the selected offer. Metadata only; no extra offer GET is made during creation.")

    @field_validator("name", mode="before")
    @classmethod
    def validate_name(cls, value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Enter a campaign name")
        if len(value.strip()) < 5:
            raise ValueError("The name must contain at least 5 characters")
        if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
            raise ValueError("The name contains invalid control characters")
        if len(value.strip()) > 255:
            raise ValueError("The name must contain no more than 255 characters")
        return value.strip()

    @field_validator("geo_code", mode="before")
    @classmethod
    def validate_geo(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("Enter one two-letter country GEO code")
        code = value.strip().upper()
        if code and any(char < "A" or char > "Z" for char in code):
            raise ValueError("The GEO code supports only letters A–Z")
        if len(code) != 2 or pycountry.countries.get(alpha_2=code) is None:
            raise ValueError("Enter one valid two-letter country GEO code")
        return code

    @property
    def geo_codes(self) -> list[str]:
        return [self.geo_code]


class OfferRead(BaseModel):
    id: int
    name: str


class CampaignSummary(BaseModel):
    """Public campaign response; internal settings and tracker URLs are omitted."""

    id: int
    name: str
    alias: str = Field(description="Public tracking alias tm_<16 hex>. Used for creation recovery; not a secret or an API key.")
    state: str


class CampaignRead(CampaignSummary):
    domain_id: int | None = None
    group_id: int | None = None
    traffic_source_id: int | None = None
    geo_code: str | None = None
    offer_id: int | None = None
    offer_ids: list[int] = Field(default_factory=list)
    google_stream_id: int | None = None
    offer_stream_id: int | None = None
    tracking_url: str | None = None
    admin_url: str


class CampaignDeleteRead(BaseModel):
    campaign_id: int
    status: Literal["archived"] = "archived"
    group_id: int | None = None
    group_deleted: bool = False
    message: str = "The campaign was archived in Keitaro."


class RevisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(gt=0, strict=True, description="Current revision from StreamRead. Stale revisions return HTTP 409.", examples=[2])


class DraftInput(RevisionInput):
    action: Literal["add", "remove", "restore", "toggle_pin"] = Field(description="Edit the draft or pin metadata. Removing the last offer is allowed.")
    offer_id: int = Field(gt=0, le=9007199254740991, strict=True)


class StreamOfferRead(BaseModel):
    offer_id: int
    name: str
    share: int
    state: str
    pinned: bool
    removed: bool


class StreamRead(BaseModel):
    id: int
    name: str
    position: int
    revision: int
    editable: bool
    missing: bool
    dirty: bool = Field(description="A saved unpublished draft exists.")
    conflict: bool
    allocation_error: str | None = Field(default=None, description="Share allocation problem that blocks publication.")
    publication: Literal["idle", "publishing", "uncertain"] = Field(description="Pending or uncertain writes must be reconciled by reads, not replayed.")
    offers: list[StreamOfferRead]
