"""
Centralised, typed configuration for the scraper service.
All values load from environment variables (.env in dev, real env vars in prod).
Never hardcode credentials here — see SECURITY notes in the project brief.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    forebet_base_url: str = "https://www.forebet.com"
    forebet_football_url: str = "https://www.forebet.com/en/football-predictions"
    # Forebet is statistical/algorithmic — lower scrape frequency is fine.
    # Override via FOREBET_SCRAPE_INTERVAL_HOURS in .env if needed.
    forebet_scrape_interval_hours: int = 6

    freesupertips_base_url: str = "https://www.freesupertips.com/free-football-betting-tips/"
    freesupertips_scrape_interval_hours: int = 4

    soccervista_base_url: str = "https://www.soccervista.com/"
    soccervista_scrape_interval_hours: int = 6

    olbg_base_url: str = "https://www.olbg.com"

    proxy_enabled: bool = False

    proxy_host: str | None = None
    proxy_port: int | None = None
    proxy_username: str | None = None
    proxy_password: str | None = None
    proxy_session_prefix: str = "acca-scraper"

    redis_url: str = "redis://localhost:6379/0"
    redis_picks_queue: str = "queue:picks:raw"
    redis_failed_queue: str = "queue:picks:failed"

    database_url: str = "postgresql://postgres:postgres@localhost:5432/tippster"
    db_pool_min_size: int = 2
    db_pool_max_size: int = 10
    processor_max_retries: int = 3

    scrape_jitter_min_seconds: float = 1.5
    scrape_jitter_max_seconds: float = 4.0
    scrape_headless: bool = True
    scrape_max_retries: int = 3
    scrape_timeout_ms: int = 30_000

    log_level: str = "INFO"


settings = Settings()  # type: ignore[call-arg]  # values populated from env at import time
