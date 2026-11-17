from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[2] / ".env",
        extra="ignore",
        hide_input_in_errors=True,
    )

    keitaro_base_url: str = "https://your-tracker.example"
    keitaro_api_key: SecretStr = SecretStr("")
    keitaro_timeout_seconds: float = Field(default=20, gt=0, le=120)

    @field_validator("keitaro_base_url")
    @classmethod
    def normalize_base_url(cls, value: str) -> str:
        parts = urlsplit(value.strip())
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or parts.path.rstrip("/") not in ("", "/admin", "/admin_api", "/admin_api/v1")
        ):
            raise ValueError("Укажите HTTPS-адрес трекера без параметров ссылки")
        return urlunsplit((parts.scheme, parts.netloc, "", "", ""))

    def missing_configuration(self) -> list[str]:
        fields = {
            "KEITARO_API_KEY": bool(self.keitaro_api_key.get_secret_value()),
            "KEITARO_BASE_URL": self.keitaro_base_url != "https://your-tracker.example",
        }
        return [name for name, present in fields.items() if not present]
