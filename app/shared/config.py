from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: str = "development"
    service_role: str = "all"
    database_url: str = "sqlite+pysqlite:///./black_soil_loop.sqlite3"
    b01_database_url: str | None = None
    b02_database_url: str | None = None
    worker_database_url: str | None = None
    jwt_secret: str = "development-secret-change-before-production-2026"
    access_token_minutes: int = 15
    refresh_token_days: int = 7
    idle_timeout_minutes: int = 30
    cors_origins: str = "http://localhost:8787,http://127.0.0.1:8787"
    public_base_url: str = "http://127.0.0.1:8101"
    openai_api_key: str | None = None
    openai_model: str | None = None
    openai_transcription_model: str = "gpt-4o-mini-transcribe"
    wechat_app_id: str | None = None
    wechat_app_secret: str | None = None
    device_api_key: str = "development-device-key-change-before-production"
    cookie_domain: str | None = None
    upload_dir: str = "/var/lib/black-soil-loop/uploads"
    upload_max_bytes: int = 10 * 1024 * 1024
    rate_limit_per_minute: int = 120
    auth_rate_limit_per_minute: int = 20
    device_rate_limit_per_minute: int = 600
    demo_seed_password: str = "Demo-Change-Me-2026"

    @field_validator("jwt_secret")
    @classmethod
    def validate_secret(cls, value: str) -> str:
        if len(value) < 32:
            raise ValueError("JWT_SECRET 至少需要 32 个字符")
        return value

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"

    @property
    def cors_origin_list(self) -> list[str]:
        return [part.strip() for part in self.cors_origins.split(",") if part.strip()]

    @property
    def effective_database_url(self) -> str:
        by_role = {
            "b01": self.b01_database_url,
            "b02": self.b02_database_url,
            "worker": self.worker_database_url,
            "migration": self.worker_database_url,
        }
        return by_role.get(self.service_role) or self.database_url


@lru_cache
def get_settings() -> Settings:
    return Settings()
