"""Typed application configuration loaded from environment variables."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Global application settings loaded from environment / .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    DATABASE_URL: str = "postgresql+asyncpg://happyvalley:happyvalley@localhost:55432/happyvalley"
    SECRET_KEY: str = "change-me-in-production"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRATION_HOURS: int = 168
    MODE: str = "demo"

    AI_API_KEY: str | None = None
    AI_MODEL: str | None = None
    AI_BASE_URL: str | None = None
    IMAGE_API_KEY: str | None = None
    IMAGE_MODEL: str | None = None
    IMAGE_BASE_URL: str | None = None
    AI_TIMEOUT_SECONDS: float = 30.0
    IMAGE_TIMEOUT_SECONDS: float = 60.0
    MAX_ACTIVE_ROOMS: int = 6
    RESERVATION_SECONDS: int = 60
    RECONNECT_SECONDS: int = 300
    CHARACTERS_CONFIG_PATH: str = "config/characters.yaml"
    ACTIVITY_CONFIG_PATH: str = "config/painting_activity.yaml"
    CRISIS_CONFIG_PATH: str = "config/crisis_keywords.yaml"

    MEDIA_STORAGE_PATH: str = "./media"
    CORS_ORIGINS: str = "http://localhost:5173"
    HOST: str = "0.0.0.0"
    PORT: int = 8000


@lru_cache
def get_settings() -> Settings:
    """Return cached settings instance."""
    return Settings()
