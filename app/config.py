from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "smart-drying-rack"

    # RENDER/DB CHANGE:
    # Keep SQLite as the local fallback. On Render, set DATABASE_URL to the
    # PostgreSQL Internal Database URL in the Web Service environment variables.
    database_url: str = "sqlite:///./drying_rack.db"

    telegram_bot_token: str = ""
    telegram_mode: str = "polling"
    public_base_url: str = ""
    telegram_webhook_secret: str = ""

    device_secret_ttl_seconds: int = 900
    heartbeat_timeout_seconds: int = 90
    rain_action_timeout_seconds: int = 300

    # Loads local values from .env when developing on your PC. Render injects
    # the same settings through environment variables, so no secrets are hardcoded.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
