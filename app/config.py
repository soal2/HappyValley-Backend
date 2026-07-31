"""Typed application configuration loaded from environment variables."""

from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Global application settings loaded from environment / .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    DATABASE_URL: str = "postgresql+asyncpg://happyvalley:happyvalley@localhost:55432/happyvalley"
    SECRET_KEY: str = "change-me-in-production"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRATION_HOURS: int = 168
    MODE: str = "production"
    AGENT_BACKEND: Literal["http", "fake"] = "http"

    AI_API_KEY: str | None = None
    AI_MODEL: str | None = None
    AI_BASE_URL: str | None = None
    AI_NETWORK_MODE: Literal["auto", "direct", "proxy"] = "auto"
    AI_PROXY_URL: str | None = None
    IMAGE_API_KEY: str | None = None
    IMAGE_MODEL: str | None = None
    IMAGE_BASE_URL: str | None = None
    AI_TIMEOUT_SECONDS: float = 18.0
    IMAGE_TIMEOUT_SECONDS: float = 60.0
    TTS_PROVIDER: str = "elevenlabs"
    ELEVENLABS_API_KEY: str | None = None
    ELEVENLABS_TTS_MODEL: str = "eleven_multilingual_v2"
    ELEVENLABS_TTS_BASE_URL: str = "https://api.elevenlabs.io/v1"
    ELEVENLABS_TTS_TIMEOUT_SECONDS: float = 20.0
    ELEVENLABS_TTS_OUTPUT_FORMAT: str = "mp3_44100_128"
    ELEVENLABS_VOICE_FACILITATOR_ID: str | None = None
    ELEVENLABS_VOICE_YOU_ID: str | None = None
    ELEVENLABS_VOICE_ZHU_ID: str | None = None
    ELEVENLABS_VOICE_XIAOLI_ID: str | None = None
    ELEVENLABS_VOICE_ZHANG_ID: str | None = None
    ELEVENLABS_VOICE_LI_ID: str | None = None
    TTS_ALLOW_ANONYMOUS_DEMO: bool = False
    TTS_CACHE_MAX_ITEMS: int = 128
    TTS_CACHE_TTL_SECONDS: int = 600
    MAX_ACTIVE_ROOMS: int = 6
    RESERVATION_SECONDS: int = 60
    RECONNECT_SECONDS: int = 300
    CHARACTERS_CONFIG_PATH: str = "config/characters.yaml"
    ACTIVITY_CONFIG_PATH: str = "config/painting_activity.yaml"
    CRISIS_CONFIG_PATH: str = "config/crisis_keywords.yaml"
    MBTI_APPEARANCE_CONFIG_PATH: str = "config/mbti_appearance.yaml"
    CHARACTER_GENERATION_MODE: Literal["dynamic", "fixed"] = "dynamic"
    CHARACTER_GENERATION_TIMEOUT_SECONDS: float = 15.0
    CHARACTER_GENERATION_WAIT_SECONDS: float = 8.0
    CHARACTER_GENERATION_STALE_SECONDS: float = 30.0

    MEDIA_STORAGE_PATH: str = "./media"
    CORS_ORIGINS: str = "http://localhost:5173"
    CORS_ORIGIN_REGEX: str = r"^https://([a-z0-9-]+\.)*lingguangcontent\.com$"
    ADMIN_USERNAMES: str = ""
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    @model_validator(mode="after")
    def reject_anonymous_tts_in_production(self) -> "Settings":
        if self.MODE.lower() == "production" and self.TTS_ALLOW_ANONYMOUS_DEMO:
            raise ValueError("生产环境不能启用匿名 TTS")
        return self


def expand_cors_origins(value: str) -> list[str]:
    """Return configured origins plus the equivalent local development hostname."""
    origins: list[str] = []

    def add(origin: str) -> None:
        if origin and origin not in origins:
            origins.append(origin)

    for raw_origin in value.split(","):
        origin = raw_origin.strip()
        add(origin)
        parsed = urlsplit(origin)
        alias = {"localhost": "127.0.0.1", "127.0.0.1": "localhost"}.get(
            parsed.hostname or ""
        )
        if not alias:
            continue
        port = f":{parsed.port}" if parsed.port is not None else ""
        add(urlunsplit((parsed.scheme, f"{alias}{port}", "", "", "")))

    return origins


@lru_cache
def get_settings() -> Settings:
    """Return cached settings instance."""
    return Settings()
