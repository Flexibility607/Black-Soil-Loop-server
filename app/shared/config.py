from __future__ import annotations

from functools import lru_cache

from pydantic import SecretStr, field_validator, model_validator
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
    voice_assistant_enabled: bool = False
    voice_public_enabled: bool = False
    assistant_public_db_quota_enabled: bool = False
    voice_stt_provider: str = "aliyun"
    aliyun_nls_access_key_id: SecretStr | None = None
    aliyun_nls_access_key_secret: SecretStr | None = None
    aliyun_nls_app_key: SecretStr | None = None
    aliyun_nls_endpoint: str = "https://nls-gateway-cn-shanghai.aliyuncs.com/stream/v1/asr"
    aliyun_nls_vocabulary_id: str | None = None
    voice_max_seconds: int = 30
    voice_max_bytes: int = 2 * 1024 * 1024
    voice_public_per_minute: int = 3
    voice_public_per_hour: int = 20
    voice_public_per_day: int = 50
    voice_public_text_per_minute: int = 10
    voice_public_text_per_day: int = 200
    voice_global_per_day: int = 500
    voice_max_concurrency: int = 2
    voice_lease_seconds: int = 60
    voice_rate_limit_hmac_secret: SecretStr | None = None
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
    dashboard_map_mode: str = "legacy"
    algorithm_showcase_enabled: bool = False
    public_information_enabled: bool = False
    showcase_dataset_enabled: bool = False
    dataset_role: str = "live"
    public_dashboard_dataset: str = "live"
    showcase_database_url: str | None = None
    showcase_b01_database_url: str | None = None
    showcase_b02_database_url: str | None = None
    showcase_worker_database_url: str | None = None
    showcase_upload_dir: str = "/var/lib/black-soil-loop/showcase-uploads"
    showcase_account_usernames: str = ""
    showcase_case_key: str = "changchun-fixed-showcase-v1"
    map_location_delayed_minutes: int = 10
    map_location_stale_minutes: int = 30

    @field_validator("jwt_secret")
    @classmethod
    def validate_secret(cls, value: str) -> str:
        if len(value) < 32:
            raise ValueError("JWT_SECRET 至少需要 32 个字符")
        return value

    @field_validator("voice_stt_provider")
    @classmethod
    def validate_voice_provider(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"aliyun", "openai"}:
            raise ValueError("VOICE_STT_PROVIDER 仅支持 aliyun 或 openai")
        return normalized

    @field_validator("dashboard_map_mode")
    @classmethod
    def validate_dashboard_map_mode(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"legacy", "changchun"}:
            raise ValueError("DASHBOARD_MAP_MODE 仅支持 legacy 或 changchun")
        return normalized

    @field_validator("public_dashboard_dataset")
    @classmethod
    def validate_public_dashboard_dataset(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"live", "showcase"}:
            raise ValueError("PUBLIC_DASHBOARD_DATASET must be live or showcase")
        return normalized

    @field_validator("dataset_role")
    @classmethod
    def validate_dataset_role(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"live", "showcase"}:
            raise ValueError("DATASET_ROLE must be live or showcase")
        return normalized

    @model_validator(mode="after")
    def validate_voice_settings(self):
        positive_fields = (
            "voice_max_seconds",
            "voice_max_bytes",
            "voice_public_per_minute",
            "voice_public_per_hour",
            "voice_public_per_day",
            "voice_public_text_per_minute",
            "voice_public_text_per_day",
            "voice_global_per_day",
            "voice_max_concurrency",
            "voice_lease_seconds",
        )
        if any(getattr(self, field) <= 0 for field in positive_fields):
            raise ValueError("语音助手限制配置必须为正整数")
        if self.voice_max_seconds > 30:
            raise ValueError("VOICE_MAX_SECONDS 不能超过 30")
        if self.voice_max_bytes > 2 * 1024 * 1024:
            raise ValueError("VOICE_MAX_BYTES 不能超过 2 MiB")
        if self.voice_lease_seconds < 60:
            raise ValueError("VOICE_LEASE_SECONDS 至少需要 60 秒以覆盖上游超时")
        if not self.aliyun_nls_endpoint.lower().startswith("https://"):
            raise ValueError("ALIYUN_NLS_ENDPOINT 必须使用 HTTPS")
        if self.voice_public_enabled and not self.voice_assistant_enabled:
            raise ValueError("VOICE_PUBLIC_ENABLED 需要同时启用 VOICE_ASSISTANT_ENABLED")
        if self.voice_public_enabled and not self.assistant_public_db_quota_enabled:
            raise ValueError("VOICE_PUBLIC_ENABLED 需要同时启用 ASSISTANT_PUBLIC_DB_QUOTA_ENABLED")
        if self.is_production and self.voice_public_enabled and self.voice_stt_provider != "aliyun":
            raise ValueError("生产匿名语音转写必须使用 aliyun provider")
        if self.map_location_delayed_minutes <= 0:
            raise ValueError("MAP_LOCATION_DELAYED_MINUTES 必须为正整数")
        if self.map_location_stale_minutes <= self.map_location_delayed_minutes:
            raise ValueError("MAP_LOCATION_STALE_MINUTES 必须大于延迟阈值")
        # Credentials are checked by the affected endpoint. A missing or
        # malformed secret must fail that request closed without preventing B01,
        # SSE, dashboards, or the deterministic text assistant from starting.
        return self

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

    @property
    def effective_showcase_database_url(self) -> str | None:
        by_role = {
            "b01": self.showcase_b01_database_url,
            "b02": self.showcase_b02_database_url,
            "worker": self.showcase_worker_database_url,
            "migration": self.showcase_worker_database_url,
        }
        return by_role.get(self.service_role) or self.showcase_database_url

    @property
    def showcase_account_username_set(self) -> set[str]:
        return {part.strip() for part in self.showcase_account_usernames.split(",") if part.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
