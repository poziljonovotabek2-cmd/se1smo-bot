from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    bot_token: str
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/quakebot"
    public_base_url: str = ""
    polling: bool = True
    check_interval_seconds: int = 60
    cache_ttl_seconds: int = 60
    max_stale_cache_seconds: int = 600
    user_agent: str = "QuakeWatchTelegramBot/1.0"
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
